# Shared checkpoints with native gVisor reflink

The production layout separates control, business and storage pools. Keep at
least two business hosts for cross-node restore and three storage hosts in
separate availability zones. Coordinator remains single-active; its Pod can
restart on another control host, with a control-service interruption.

## Image and filesystem contract

The local-cache extension must be present in the selected ADX image before
enabling this profile. The default AKernel archive pins continue to identify
their original release. `builder/scripts/prepare-adx-overlay.py` prepares an
explicit integration build from a qualified immutable AKernel base image and
a verified clean ADX package. It preserves the base runtime dependencies and
Ubuntu/tini EROFS layout, replaces Execd, and records separate overlay
provenance. Build and push that context through your normal image pipeline;
deploy its resulting digest. This is not an upstream ADX release publication.

Capture and restore materialization must share the native local filesystem
with gVisor's filestore. A loop filesystem inside the filestore directory
cannot satisfy that condition. Mount a local XFS volume with `reflink=1` at
`/home/akernel`, and use these core chart values:

```yaml
adx:
  checkpointStorage:
    kind: distributed_fs
    alias: akernel-checkpoints
    hostPath: /srv/akernel-shared
    prefix: akernel/checkpoints-v1
    nodeSelector: {akernel.io/checkpoint-client: "true"}
    localCache:
      enabled: true
      root: /home/akernel/adx/checkpoint-cache-v1
      budgetBytes: 53687091200
      maxEntries: 500
node:
  config:
    sandboxd:
      nativeFilestore: true
      runscCheckpointMode: reflink
```

Use a fresh shared prefix for this storage format, retaining the old prefix
and state service for rollback. Coordinator receives the same alias/prefix
but no node-local cache. Running executions pin restore inputs independently
of the 50 GiB/500-unused-entry cache limits. The init container verifies actual
FICLONE support before starting a worker. Custom sandboxd TOML must retain the
`AKERNEL_NATIVE_FILESTORE` and `AKERNEL_RUNSC_CHECKPOINT_MODE` markers; provider
templates include them. Terraform forwards `checkpoint_storage.local_cache`,
`native_filestore` and `runsc_checkpoint_mode`.

Native reflink accelerates local writable-filestore capture and private
restore clones. Cross-node cold restore still transfers the artifact. The
adapter checks size and SHA256, preserves sparse files, synchronizes files and
directories, and commits the manifest last. Mount loss or publication failure
must not expose partial shared artifacts as successful recovery points.

## State and storage services

Install pinned Rook, CloudNativePG and Opstree Redis operators before their
custom resources. With Rook 1.21.0, disabling `csi.installCsiOperator` and
`csi.createCsiOperatorResources` avoids deploying CSI drivers, but the Rook
filesystem controller still watches the `ClientProfile` API. Render the
`CustomResourceDefinition` objects from the bundled `ceph-csi-operator`
subchart in the verified Rook archive and apply those definitions separately
with a dedicated field manager. Retain the source archive checksum and rendered
definitions with the deployment profile, and update them with Rook. Do not apply
the subchart's controllers or drivers for an RGW-only deployment. Check operator
readiness after the informer startup interval before creating the Ceph cluster.

The `charts/state-services` chart creates:

- Three CloudNativePG instances, one per zone, 50 GiB independent cloud PVCs,
  synchronous acknowledgement from one standby, and managed disruption budgets.
- Three Redis instances with 20 GiB independent cloud PVCs and three Sentinels
  across control zones; AOF always, no eviction and a minimum of one replica
  within five seconds for writes.
- Two HAProxy replicas in separate zones, checking authentication and the
  Redis master role; demotion closes existing backend sessions. The write
  Service is the endpoint for core's existing external Redis Secret interface.

Precreate PostgreSQL's application Secret (`username: juicefs`, `password`) and
Redis's password Secret. Pin all operand images by digest. CloudNativePG also
requires the PostgreSQL version tag, for example `postgresql:17.11@sha256:...`,
to detect major upgrades. The state chart supports `postgres.enabled=false`
when initializing Redis before the database operator is ready. Provide an explicit
independent cloud-disk StorageClass for state PVCs. Restrict network access to
the consuming applications, operators and monitoring/backup clients. Redis
Sentinel uses asynchronous replication: the replica write constraint and role
check do not establish zero RPO or a distributed fencing guarantee. Validate
ADX reconnection and state reconciliation through a real primary failover.

Configure `charts/juicefs` with `postgres.mode: external`, the CloudNativePG
read-write service hostname, the application Secret, and its CA Secret:

```yaml
postgres:
  mode: external
  existingSecret: juicefs-metadata-app
  external:
    host: juicefs-metadata-rw
    caSecret: juicefs-metadata-ca
    sslMode: verify-full
ceph:
  storageClass: YOUR_INDEPENDENT_BLOCK_STORAGE_CLASS
  nodeSelector: {node-role: akernel-storage}
  tolerations:
    - {key: dedicated, operator: Equal, value: akernel-storage, effect: NoSchedule}
  zones: [YOUR_ZONE_B, YOUR_ZONE_C, YOUR_ZONE_D]
  failureDomain: zone
  osdCount: 3
  osdSize: 1Ti
  monSize: 20Gi
  mgrCount: 2
  replicas: 3
  gatewayInstances: 2
juicefs:
  nodeSelector: {akernel.io/checkpoint-client: "true"}
  cacheMiB: 4096
  writeback: false
```

The database is external to the JuiceFS chart but can remain in the same
Kubernetes namespace. Format, wait-for-format and mount containers all use
the CA and verify the service certificate. Managed single-instance PostgreSQL
remains available for evaluation. Ceph's zone layout creates one independent
raw Block OSD PVC in each selected zone. Check the cloud provider's minimum
ESSD sizes; no existing host disk or image-cache PVC is selected or formatted.
Three 1 TiB OSDs with three replicas provide approximately 1 TiB logical
capacity; alert at 80% usage and retain headroom for recovery.

For a new RGW-only Ceph 20.2.4 cluster, verify every entry in
`ceph auth dump-keys --format=json` uses `aes256k` before setting
`ceph.allowedCiphers: [aes256k]`. This resolves the insecure-key compatibility
warnings without muting them. Existing kernel RBD/CephFS clients may require
older keys; follow [Rook's CephX guidance](https://rook.io/docs/rook/v1.21/Storage-Configuration/Advanced/cephx-key-rotation/)
before restricting an existing cluster.

Label only consuming control/business hosts for JuiceFS clients. Storage-pool
taints exclude sandbox workers. Keep the existing `OnDelete` client update
strategy: drain consumers before explicitly replacing clients and then
restart consumers to pin the new mount identity.

Use `node.nodeSelector` for worker generation/canary placement separately from
the shared client selector, which also applies to Coordinator. Conflicting
worker/client selectors fail rendering. On ACK, check each pool's Terway mode:
exclusive ENI does not support NetworkPolicy, including matching those clients
as sources of a policy protecting shared-ENI state Pods. This mixed layout must
set `networkPolicy.enabled=false` for the state chart and use an appropriate
VPC/security-group boundary plus service authentication. This does not provide
namespace-level network isolation. Use supported shared ENI pools when that
isolation is required; do not claim an untested NetworkPolicy is effective.

## Change and acceptance gates

For an existing ACK cluster, `deploy/terraform/aliyun-node-pools` owns only
new storage pools. Supply the verified cluster ID, existing zone/vSwitch map,
security groups and OS image. Review the saved plan before applying it. Keep
state, kubeconfigs, credentials and private deployment profiles outside Git.

Save old core values, manifests, image digest and Redis data. Quiesce ingress,
drain the old execution state, then select a fresh Redis namespace and shared
checkpoint prefix. Start one worker, qualify it, and expand to the remaining
business workers. Retain old state PVCs/configuration for at least seven days.
Rollback stops new consumers before restoring the prior core and state entry.

Render and validate the storage and core charts, then validate native behavior:

- Capture on A; restore on uncached B and C while A is unavailable. Check
  process memory, files and application results, not only the RPC response.
- Validate leave-running capture, repeated restore, private writes and actual
  shared extents; protect background restore inputs during cache eviction.
- Exercise SDK create/exec/pause/resume/Snapshot/auto-checkpoint/close/delete
  and reference reclamation, then component and storage failure scenarios.
- Measure control recovery against five minutes, freeze time, durable publish
  time, cold/hot restore, cache hit rate, free space and Ceph health.
- Publish daily recoverable metadata/state/artifact backup sets to independent
  private object storage with seven-day retention; perform isolated recovery
  and at least 24 hours of observation before signing off the deployment.

Manifest rendering is a configuration gate, not evidence that these operational
acceptance scenarios have passed.

Enable `prometheusServer.config.sharedStorage.enabled` in the monitor chart to
discover all CloudNativePG and Redis/Sentinel exporters in the configured
namespace and scrape the active Ceph manager. It loads Ceph health/capacity,
PostgreSQL replication and exporter-availability alert rules. Include the state
namespace in PVC metrics discovery as well. Verify actual targets and samples
after applying the configuration; notification routing remains the monitoring
deployment's responsibility. The optional [backup chart](backup/README.md)
requires its own authentication and recovery qualification before scheduling.

`deploy/tests/native_restore/shared_checkpoint_probe.py` provides capture,
cross-node restore and concurrent isolation phases. The operator owns source
outages and mount/extent inspection. The capture phase intentionally retains
the source for an outage test; record its ID and explicitly delete it afterward.
Other phases explicitly delete their detached sandboxes with
`kill(delete_remote=True)`. A detached handle's ordinary `kill()` only closes
local transports and must not be used as proof of remote deletion or reference
reclamation.
