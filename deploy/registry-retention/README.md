# Temporary image retention

The `harbor-ephemeral` SDK profile provides a configurable publication lifetime
for single-Dockerfile jobs. The AKernel retention service owns publication and
sandbox-use records in a small SQLite database on its own persistent volume.
The SDK calls the existing ADX instance APIs. Deploy the service and the updated
SDK and managed tools together.

```mermaid
flowchart LR
    H["Harbor data synthesis<br/>Temporary profile"]
    SDK["AKernel SDK<br/>build_image with TTL<br/>Reserve image use before create"]
    R["AKernel retention service<br/>Persist digest, expiry and holds"]
    DX["Agent DX<br/>Existing instance API"]
    BK["Shared BuildKit"]
    W["Worker / Dragonfly"]
    A["Registry A + PVC A<br/>Read-only during GC"]
    B["Registry B + PVC B<br/>Accepts new builds"]
    O["Operator or capacity controller<br/>Volume measurements and rotation"]

    H --> SDK
    SDK -->|"Register publication and use<br/>Report confirmed stop"| R
    SDK --> BK
    SDK -->|"Manage sandbox lifecycle"| DX
    R -->|"Verify instance absence"| DX
    R -->|"Delete eligible manifests"| B
    BK -->|"Push new images"| B
    DX --> W
    W -->|"Pull retained digests"| A
    W -->|"Pull new digests"| B
    O -.->|"Switch new builds to B"| R
    O -.->|"Fence writers, then run GC"| A

    classDef sdk fill:#dbeafe,stroke:#2563eb,color:#172554,stroke-width:2px;
    classDef retention fill:#ffedd5,stroke:#ea580c,color:#431407,stroke-width:2px;
    classDef storage fill:#ede9fe,stroke:#7c3aed,color:#2e1065,stroke-width:2px;
    class SDK sdk;
    class R retention;
    class A,B storage;
```

Blue marks the SDK changes, orange marks the retention service, and purple
marks the two independent storage endpoints. The diagram shows B active during
A's GC window. The service can sweep eligible manifests on A before A is sealed.
During GC, workers keep pulling retained digests from A's original endpoint.

## Configure

- Provision two independent private Registry endpoints with separate storage.
  Configure an existing Sandboxd managed-runtime profile for each endpoint.
  Use the corresponding names `harbor-ephemeral-a` and `harbor-ephemeral-b`,
  and set their repository prefix to `akernel-ephemeral`. The SDK alias selects
  the current writable endpoint. A publication retains its Registry authority.
- Copy [the example configuration](config.example.json). The default TTL is
  86400 seconds and the default maximum is 604800 seconds. New publications use
  the policy at admission, and their expiry starts when the Registry digest is
  verified. The service keeps completed ledger entries for
  `terminal_receipt_seconds` (604800 seconds by default), measured from
  cancellation, manifest deletion, or confirmed cleanup of an abandoned push.
  It prunes eligible entries in batches and SQLite reuses the space. Live
  holds, pending writes and unconfirmed cleanups stay in the ledger.
  Leave `auto_delete` false to inspect candidates during rollout.
- Run one service replica with `retention.py`, `registry.py`, `capacity.py` and
  `checkpoint_archive.py` on a private
  HTTPS endpoint. Mount the SQLite directory on an independent persistent
  volume. Mount separate credentials for the trusted Harbor runner, the ADX
  instance observer, and the Registry token issuers. The ADX observer must be
  able to query instances created by that runner. Grant Registry manifest-delete
  access only to the retention identity and the dedicated temporary prefix.
- Configure Harbor's `managed_runtime_profile` as `harbor-ephemeral` for
  single-Dockerfile data synthesis. Set `AKERNEL_IMAGE_RETENTION_URL` and
  `AKERNEL_IMAGE_RETENTION_TOKEN_FILE` in the runner's environment with the
  trusted CA installed. Keep temporary references within that runner's
  AKernel SDK admission path. Use the ordinary durable profile for Compose,
  independent image consumers, and reusable snapshots on deployments without
  the checkpoint archive.

```sh
python3 retention.py --config /etc/retention/config.json \
  --database /var/lib/retention/state.sqlite serve --listen 0.0.0.0 \
  --tls-cert /etc/retention/tls/tls.crt --tls-key /etc/retention/tls/tls.key
```

The service protects busy, paused and pending instances after TTL. An owned
SDK sandbox reports a confirmed DELETE when Harbor stops the environment.
The service then verifies that ADX has removed that instance before releasing
the image hold. ADX can retire a lost worker's directory entry without
contacting it, so a missing entry alone keeps the hold in place.

Inspect `status` for unresolved holds. Use `cancel-hold --confirmed-absent`
for a create known never to have started. An operator who verifies physical
teardown of an existing sandbox can use `release-hold --confirmed-stopped`.
The latter also checks that the instance is absent from ADX.

## Harbor single-Dockerfile checkpoints

Existing Harbor jobs can keep `managed_runtime_profile: harbor-v1`. Upgrade the
centrally managed runner SDK and set `AKERNEL_HARBOR_V1_TEMPORARY_IMAGES=1`
alongside its existing retention origin and dedicated runner token file. The
SDK selects the current writable A/B profile for single-Dockerfile
`build_image` calls. The shared BuildKit endpoint and tools image stay the
same. The service defaults to a 24-hour TTL, and operators can set
`default_ttl_seconds` and `max_ttl_seconds`. SDK callers can supply
`ttl_seconds` for individual builds. Capacity LRU continues to follow the
temporary Registry policy below.

Compose uses its managed runtime profile directly and keeps its existing
publication path. A running Compose collection uses node-local immutable
image roots and checkpoint pins. Cross-worker recovery of those collections
is outside this archive. Unmodified runners and `harbor-v2` continue to use
their configured persistent path. This rollout only covers checkpoints
created with the archive enabled. Historical images and checkpoints require
an independent cleanup decision.

Before a new checkpoint can become Ready, sandboxd coordinates an archive
copy while the temporary publication is pinned. The service preserves
original OCI index, manifest, config and blob bytes, verifies every object's
size and SHA-256, and checks each Nydus bootstrap blob table against the
archived descriptors with `nydus-image inspect --request blobs`. It writes
`akernel-image-archive.json` into the staged checkpoint on JuiceFS. Agent DX
seals and publishes that directory through the existing snapshot API.
Restoration uses the receipt and a worker-scoped runtime lease to read the
verified image through the private read-only Registry API, including Nydus
byte ranges. It can use the archive after both temporary Registry endpoints
and worker caches lose the original image.

```mermaid
flowchart LR
    H["Harbor runner<br/>harbor-v1"]
    B["Shared BuildKit"]
    T["Temporary Registry A/B<br/>TTL and LRU"]
    W["sandboxd<br/>capture and restore"]
    R["Checkpoint image archive<br/>verified OCI and Nydus objects"]
    J["JuiceFS<br/>OCI objects and checkpoint receipt"]
    D["Agent DX<br/>checkpoint Ready"]
    H --> B --> T
    H --> W
    T -->|"Original digest, pinned during copy"| R
    W -->|"Request archive before capture returns"| R
    R --> J
    J -->|"Seal checkpoint directory"| D
    W -->|"Cold restore by digest"| R
    classDef archive fill:#ffedd5,stroke:#ea580c,color:#431407
    classDef entry fill:#dbeafe,stroke:#2563eb,color:#172554
    class R,J archive
    class H,W entry
```

Configure this optional block in the retention service JSON.

```json
{
  "checkpoint_archive": {
    "mount": "/mnt/akernel-shared",
    "checkpoint_artifacts": "/mnt/akernel-shared/akernel/checkpoints/artifacts",
    "root": "/mnt/akernel-shared/akernel/checkpoint-images",
    "volume_identity": "hk-checkpoint-archive-01",
    "registry_origin": "https://image-retention.private.example",
    "worker_token_file": "/etc/retention/worker-token",
    "registry_token_file": "/etc/retention/archive-pull-token",
    "nydus_image": "/usr/local/bin/nydus-image",
    "auto_delete": false
  }
}
```

- Mount the same JuiceFS dataset on the service, worker and Coordinator.
  The service verifies the `fuse.juicefs` mount and the value in
  `.akernel-checkpoint-archive-volume` at its root. Create this marker on the
  mounted dataset. The service needs permission to read and write the ADX
  checkpoint artifact directories. Use a Nydus inspector compatible with
  the BuildKit exporter and allow private HTTPS from workers to this origin.
  Trust its CA in sandboxd and the Nydus client. Populate the exact archive
  Registry authority in each worker's existing `registry_auths.json`. Use
  username `akernel-checkpoint` and the password from `registry_token_file`,
  with the Docker `auth` field set to base64 of username and password joined
  with a colon. Keep the pull credential distinct from the worker control
  token, the runner token and ADX credentials. Archive pulls use the private
  origin directly, with TLS verification and the exact authority's
  credentials. Normal Registries keep their separate proxy policy.
- Run a single writable archive service replica on the existing SQLite PVC.
  Mount and initialize the native distributed checkpoint store before
  starting the archive service. Back up the archive ledger and JuiceFS
  dataset together. The ledger binds to the native checkpoint store scope
  and the archive volume identity. Keep the local sandboxd lease directory
  beside its store on the worker's persistent disk so it survives pod
  replacement. An unreachable worker or missing disk requires confirmed
  decommissioning before an unresolved archive lease can be removed.
- Set `adx.checkpointStorage.kind=distributed_fs` and enable
  `adx.checkpointImageArchive` with its private `origin` and a worker-only
  `existingSecret` containing the configured `tokenKey`. Use runsc full
  checkpoint mode for JuiceFS. The chart's worker init container verifies that
  the pinned sandboxd binary supports the archive. Grant A/B build profiles
  to the approved runner tenant. Deploy the archive service and update the
  complete eligible worker pool before enabling temporary routing on the
  runner.
- Canary the runner using its existing `harbor-v1` job configuration and a
  short TTL in an isolated deployment. Verify a post-TTL read, Ready checkpoint
  creation, actual temporary Registry GC, and cold-worker restoration. After
  proving reference cleanup, set archive `auto_delete` to true. An archive
  object is unlinked only after ADX removes the referenced checkpoint artifacts
  and sandboxd confirms deletion of its last runtime users. Shared objects
  remain until all referring digests are gone. JuiceFS trash retention and
  chunk cleanup determine when those unlinks return backend storage bytes.
- To revert publication, remove the runner flag or roll back the runner.
  Newly submitted `harbor-v1` jobs use the original persistent profile again.
  Keep the worker integration, same private origin and JuiceFS/SQLite storage
  available for archive-backed checkpoints and running restores until their
  references have been released. Existing archive-capable SDKs can still
  capture or restore these checkpoints while new builds use durable images.
  Keep A/B available while in-flight builds and original temporary image
  uses drain.

## Capacity management

For bursty builds, enable the optional `capacity` configuration and run one
maintenance process alongside the service. Both processes use the same
configuration and SQLite volume. Set `auto_delete` to enable rotation and
manifest deletion. The capacity process measures both Registry volumes even
while a GC command is running. Start it before sending builds to the profile.

```json
"capacity": {
  "driver": "/etc/retention/registry-maintenance",
  "storage_ids": {"a": "registry-a-volume-uid", "b": "registry-b-volume-uid"},
  "high_watermark_percent": 80,
  "low_watermark_percent": 65,
  "max_in_flight_builds": 16,
  "prepare_seconds": 600,
  "poll_seconds": 10,
  "max_sample_age_seconds": 60,
  "rotation_interval_seconds": 21600,
  "drain_seconds": 1800,
  "driver_timeout_seconds": 3600,
  "lru_idle_seconds": null
}
```

```sh
python3 retention.py --config /etc/retention/config.json \
  --database /var/lib/retention/state.sqlite maintain
```

Admission stops at the high watermark and resumes at the low watermark.
Each build reserves a place before it starts a preparation sandbox, and the
reservation counts towards the per-writer limit through publication. The
SDK releases unused reservations after preparation failures. Set
`prepare_seconds` for the time allowed to schedule the sandbox, upload the
context and register its publication plan. An expired reservation cannot
start a push. Admission and planning both require a fresh volume measurement.
A capacity rejection returns HTTP 503 with code
`REGISTRY_CAPACITY_UNAVAILABLE`. Size the headroom and concurrency limit for the
largest concurrent builds because the registry learns blob sizes during upload.

The controller rotates at the high watermark or on the configured interval.
It also rotates when the build limit stays occupied entirely by stale plans, so an
interrupted publication can be reconciled after fencing. Set
`rotation_interval_seconds` to zero to disable periodic rotation.
Rotation starts new builds on a healthy standby and gives planned builds on the
retiring slot up to `drain_seconds` to finish before fencing. Set that window
for the build time you want to allow during rotation. When both volumes are
blocked, the controller first tries to reclaim the standby. If it comes back
without enough headroom, the controller pauses new build admission and drains
the former writer for its own maintenance. Each restore must pass the write
check and the capacity threshold before it can reopen admission. If both
volumes remain blocked, maintenance retries alternate after a five-minute
cooldown. Existing sandboxes keep reading their original digest and endpoint.
If a write check fails after GC, the controller records a pending restore and
keeps that slot sealed. A blocked writer can then drain and collect its own
volume. Future attempts resume at restore and wait when a fresh measurement
shows zero available bytes. Inspect `status` for measurements, queued
maintenance, pending restores, publication states,
and the per-slot count of abandoned repositories awaiting cleanup.

Set `lru_idle_seconds` to a positive idle grace to allow early eviction of
temporary images published with that policy. The receipt exposes
`pressure_evictable`. Pressure maintenance first sweeps expired publications,
then considers opted-in publications in order of their last confirmed sandbox
stop. Never-used images keep their TTL. A hold still protects any live or
uncertain sandbox, including an instance occasionally reading a Nydus blob.
Scheduled rotation uses the TTL policy. Each sweep considers up to 256
publications, with space usage remeasured after physical GC.

### Maintenance driver

Supply a deployment-owned executable at `capacity.driver`. The controller runs
it directly with three arguments, `<action> <slot> <storage-id>`. Bind each slot
to its existing endpoint and independent filesystem or PVC, and verify the
provided storage identity on every action. Use the Registry volume's measured
filesystem capacity. All actions must be safe to retry after a timeout or a
restart.

| Action | Required behavior |
| --- | --- |
| `measure` | Write a JSON object to stdout with `storage_id`, `total_bytes`, `available_bytes` and `writable` (boolean). Verify the mounted volume, reachable Registry and effective write mode. This action may run during another action. |
| `fence` | Drain or stop outstanding writers for this slot, keep the original read endpoint, put the Registry in read-only mode and disable the upload purger. Keep this fence in effect throughout GC, including across driver restarts. Ensure stopped builds cannot resume an upload when the endpoint is writable again. The controller calls this action before GC even if the Registry already reports read-only mode. |
| `gc` | Recheck the identity and write fence. Apply the abandoned-repository plan on stdin when present, then dry-run and run Distribution GC with the same Registry version and configuration. Serialize this work on the volume and report success only after GC completes. |
| `restore` | Verify that GC has stopped, restore the writable Registry and pass a push/read smoke check on the same endpoint before reporting success. |

Run the driver with the permissions scoped to these two Registries. Its
`measure` loss or a failed step keeps the affected slot out of new build
selection. The controller keeps the maintenance job in SQLite and retries it.
When providing the driver for Kubernetes, validate it against the dedicated
namespace and the actual PVC identities before enabling it for that deployment.

After fencing, the controller reconciles each exact planned tag. A tag that
resolves to a digest is recorded as a publication. For plans whose tag is
absent, the `gc` driver receives a JSON object on stdin with an
`abandoned_repositories` array of up to 256 repository paths. It receives empty
stdin when that batch is empty. Scope cleanup to the listed, private
per-build repositories. Keep tag-bearing repositories and report them as
deferred by leaving them out of the receipt.

For the filesystem backend, run
`python3 cleanup_abandoned.py --rootdirectory <rootdirectory> --writers-fenced`
with the received JSON. Ship `registry.py` alongside the helper. Use the
effective filesystem rootdirectory from the fenced Registry configuration,
mounted for the helper at a verified absolute path without nested mounts.
The helper rechecks for current tags and removes the repository metadata and
staged upload files only within the listed paths. Distribution's default GC
then reclaims unreferenced shared blob data. Keep the fence through both steps.
After GC completes, forward the helper's JSON `cleaned_repositories` receipt
to stdout. Send operational output to stderr when returning a receipt. A missing
path counts as already cleaned so the driver can retry after an interruption.
Any unconfirmed paths stay pending for a later fenced maintenance pass.

## Reclaim and rotate

Use the same configuration and database when running operator commands.
`switch`, `seal` and `ready` can be used for manual rotation when the
maintenance process is stopped and all queued maintenance and restores are complete.
`sweep` lists expired and unreferenced manifest candidates. With `auto_delete`
enabled, the service periodically applies those candidates. It verifies the
original tag and manifest graph and records the deletion worklist before
deleting. Interrupted deletions resume that exact worklist.

1. Verify the standby endpoint can publish and serve an owned image, then run
   `switch <standby>`. New builds go to the standby. Already planned
   publications can finish on the previous target.
2. Drain accepted writes on the previous Registry and fence its BuildKit
   writers. Let manifest reclamation run for eligible images before switching
   that Registry to read-only. Disable its upload purger for the maintenance
   window. Then run `seal <old> --writers-fenced` to reconcile interrupted
   publications against the Registry.
3. Run Distribution's GC dry run and then GC against only the fenced storage
   with its exact Registry version and configuration. Keep its read path
   available. Use the default mark-and-sweep options, which retain manifests
   addressable only by digest. Record actual bytes freed and verify retained
   digests before resuming writes. The capacity controller handles the
   scoped cleanup of abandoned pushes during automatic maintenance.
4. Restore the retired Registry's writable configuration. After it passes
   write and read smoke checks, run `ready <old>` to make it eligible for a
   later switch.

For the first cleanup of a legacy storage domain, review its retained-reference
inventory and verify recoverability on a clone. Historic repositories are
outside the automatic manifest policy. The Registry sweep covers its whole
filesystem and requires a no-writer window for that filesystem.
[Distribution 3.1.2 maintenance](https://github.com/distribution/distribution/blob/v3.1.2/docs/content/about/garbage-collection.md)
