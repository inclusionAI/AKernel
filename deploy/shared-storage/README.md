# JuiceFS and AFS checkpoint storage

AKernel uses ADX's `distributed_fs` interface for shared checkpoint storage.
This directory includes the JuiceFS deployment stack: Rook, Ceph RGW and its
bucket, PostgreSQL, a format Job, and
per-host FUSE clients. AFS and an existing JuiceFS deployment can use the same
ADX configuration through an existing mount or RWX claim.

## Storage layout and ownership

```text
Shared JuiceFS / AFS namespace
  akernel/checkpoints/artifacts/<id>/   Immutable checkpoint artifacts
  shares/                              Optional application HostPath data

Node-local storage
  /home/akernel/filestore/              Private writable layers
  /home/akernel/sandboxd/               Runtime state and sockets

adxlet -> sandboxd checkpoint_dir -> shared checkpoint artifact
Coordinator -> shared storage -> artifact reclamation
JuiceFS client -> PostgreSQL metadata + Ceph RGW data blocks
```

In the default direct filesystem mode, adxlet allocates each shared artifact
directory and passes it to sandboxd through `CheckpointRequest.checkpoint_dir`.
The optional local staging mode captures on the native local filesystem,
publishes complete artifacts to the shared volume, and materializes verified
restore inputs in a protected node-local cache. It requires a qualifying ADX
image with `distributed_fs.local_cache` support; the baseline release archive
does not include that extension. See [the HA deployment guide](production.md).
Sandboxd's `filestore_dir` configures
private runtime writes. Keep the same container-local filestore path on every
node; each path is backed by that node's local disk. Runtime state, private
writable layers and checkpoints have separate paths. Restore reconstructs the
private writable layer locally; account for that copy in performance results.

Helm and standalone enable runsc background restore and checkpoint output synchronization. Checkpoint success waits for regular files and directories to be synced; restore input files remain protected while the execution uses them. Firecracker uses the buffered `Async` writable-disk engine with virtio-fs disabled by default. This profile uses EROFS roots; directory-backed roots and host directory mounts require explicitly enabling a compatible virtio-fs profile. Any explicit Firecracker `storage_dir` must also stay outside the shared checkpoint mount.

Anonymous recovery points follow their Environment's retention, replacement
and deletion. ADX protects files used by running executions. Coordinator
reclaims shared artifacts; sandboxd removes private runtime files. JuiceFS/AFS
owns physical block reclamation and COW sharing. Deleting a reusable Snapshot
that is in use returns its Sandbox consumers.

JuiceFS defaults to `trashDays: 0`, so deleted checkpoint files have no extra
filesystem trash retention. Environment and Snapshot references still control
when Coordinator may delete them. Existing volumes require an explicit JuiceFS
configuration update; the format Job preserves existing volume settings.

## Deploy through Terraform

Both `deploy/terraform/aliyun` and `deploy/terraform/huaweicloud` expose the same
`juicefs` and `checkpoint_storage` objects. The stack is disabled by default.
To deploy PostgreSQL, Rook/Ceph RGW and JuiceFS, add this to the selected
profile's `terraform.tfvars`:

```hcl
juicefs = {
  enabled                = true
  ceph_storage_class     = "YOUR_RAW_BLOCK_STORAGE_CLASS"
  postgres_storage_class = "YOUR_FILESYSTEM_STORAGE_CLASS"
  cache_mib              = 4096
  cache_large_write      = true
  # Optional node-local disk directory; empty uses a namespace/release path.
  cache_host_path        = ""
  gateway_tcp_nodelay     = true
}
```

This also selects `distributed_fs` for ADX, with the managed host mount at
`/srv/akernel-shared`. Terraform deploys the storage namespace, pinned Rook
operator and local JuiceFS chart. It waits for the format Job and mount clients
before installing AKernel. A bounded post-install/upgrade readiness Job
checks that the current OnDelete client template is ready on every selected
host, including a nonempty host set. Helm's built-in wait alone does not wait
for OnDelete DaemonSets. API failures, missing mounts or outdated clients fail
the release instead of allowing the dependent AKernel release to start.
Use the main deployment guide for cloud credentials,
image selection and TLS/API-key setup. Run `make plan ENV=YOUR_PROFILE`, review
the resulting resources, and then deploy the approved plan.

The guided profile generator accepts `JUICEFS_ENABLED=true` and
`JUICEFS_STORAGE_CLASS=YOUR_STORAGE_CLASS` through `make config`; the class must
support both filesystem and raw Block PVCs. Advanced settings remain available
in the generated `terraform.tfvars`. `JUICEFS_CACHE_MIB` (default `4096`),
`JUICEFS_CACHE_LARGE_WRITE` (default `true`) and `JUICEFS_CACHE_HOST_PATH`
(default empty) also pass through `make config`. For external storage, use
`CHECKPOINT_STORAGE_HOST_PATH=/srv/akernel-shared` or
`CHECKPOINT_STORAGE_EXISTING_CLAIM=YOUR_RWX_CLAIM`. These choices are exclusive
with the managed JuiceFS option. Existing profiles are preserved unless their
replacement is explicitly selected.

The default Ceph layout uses three OSD PVCs of 100 GiB, three monitor PVCs of
10 GiB, three data replicas and a 10 GiB PostgreSQL PVC. It requires at least
three eligible storage hosts. The default PostgreSQL instance is single-node;
this is not a complete HA database deployment. Ceph's StorageClass must support
raw Block volumes; PostgreSQL and monitors need filesystem volumes. No existing
host disk is automatically selected or formatted. The Ceph image in this
baseline is for Linux/amd64; select compatible images and placement explicitly
for another architecture.

A small, explicitly non-HA evaluation layout is:

```hcl
juicefs = {
  enabled                = true
  ceph_storage_class     = "YOUR_RAW_BLOCK_STORAGE_CLASS"
  postgres_storage_class = "YOUR_FILESYSTEM_STORAGE_CLASS"
  osd_count              = 1
  osd_size               = "40Gi"
  mon_count              = 1
  replicas               = 1
  storage_node_selector  = { "kubernetes.io/hostname" = "YOUR_STORAGE_HOST" }
}
```

`client_node_selector` limits mount clients and also constrains execution node
and Coordinator placement. It defaults to all Linux hosts; existing execution
node affinity still applies. If `values_files` adds JuiceFS `nodeSelector`
labels, the effective merged selector is passed to both consumer roles too.
The selected hosts must satisfy both storage and workload placement requirements.
`values_files` customizes the local storage chart, and
`rook_values_files` customizes the Rook operator, for example registry mirrors
and image-pull Secrets. These files must exist on the machine running Terraform.
The host mount remains tied to `juicefs.host_mount_path` so ADX and clients agree.
To reuse an existing Rook operator and namespace, set `install_rook=false` and
`create_namespace=false`; its CRDs and bucket provisioner must already be ready.

For an existing AFS/JuiceFS mount, keep `juicefs.enabled=false` and configure:

```hcl
checkpoint_storage = {
  kind      = "distributed_fs"
  alias     = "akernel-checkpoints"
  host_path = "/srv/akernel-shared"
  prefix    = "akernel/checkpoints"
}
```

For an existing JuiceFS/AFS RWX claim in the AKernel namespace, replace
`host_path` with `existing_claim = "YOUR_RWX_CLAIM"`. Exactly one source is
required. The namespace, alias and prefix must identify the same storage for
nodes and Coordinator. Shared aliases accept only ASCII letters, digits and
hyphens; `local` is reserved. This validation also applies when managed JuiceFS
implicitly selects shared storage. Defaults retain node-local checkpoints.

## Deploy the storage charts directly

Use Helm 3 and Kubernetes with privileged FUSE support and `/dev/fuse`.
Prepare cloud StorageClasses before installing. The optional stack uses Rook
**1.21.0**, Ceph **20.2.4**, PostgreSQL **17**, and JuiceFS **1.4.1**, with pinned
image digests in chart values. Rook installs cluster-scoped CRDs and RBAC; use
an existing operator when your cluster already has one.

```sh
helm upgrade --install akernel-storage-namespace \
  deploy/shared-storage/charts/namespace --namespace default \
  --set name=akernel-storage

helm upgrade --install akernel-rook rook-ceph \
  --repo https://charts.rook.io/release --version v1.21.0 \
  --namespace akernel-storage -f deploy/shared-storage/rook-values.yaml --wait

helm upgrade --install akernel-juicefs deploy/shared-storage/charts/juicefs \
  --namespace akernel-storage \
  --set ceph.storageClass=YOUR_RAW_BLOCK_STORAGE_CLASS \
  --set postgres.storageClass=YOUR_FILESYSTEM_STORAGE_CLASS \
  --wait --wait-for-jobs --timeout 60m
```

The chart provisions a retained RGW bucket through ObjectBucketClaim and reads
its generated Secret/ConfigMap. PostgreSQL credentials are generated once and
reused on upgrade, or supplied through `postgres.existingSecret` with a
`password` key. Metadata uses `fsync=on`, `synchronous_commit=on`, and
`full_page_writes=on`. The included metadata connection uses cluster-internal
PostgreSQL without TLS; apply your production database and network policies.

The format Job runs `juicefs format --no-update`. It waits for PostgreSQL and
RGW and preserves an existing formatted volume. Client Pods wait for formatted
metadata, mount the same volume and verify a real JuiceFS FUSE mount before
becoming ready. The readiness hook uses a pinned Python image and a read-only
ServiceAccount: it lists node labels and client Pods and reads this DaemonSet;
it cannot change workloads or read Secrets. `readiness.timeoutSeconds` defaults
to 900 seconds and must fit within the Helm release timeout. A failed hook is
retained for diagnostics; inspect its Job/Pod logs before retrying the upgrade.
AK/SK and database passwords stay in Kubernetes Secrets and
are not put in ADX configuration. Export secrets separately for backup.

The default uses 1 MiB blocks, 4 MiB readahead and a 4096 MiB node-local block
cache with `cache-large-write=true`. `writeback=false` is explicit and enforced:
checkpoint completion still waits for the existing shared-storage durability
barrier. Cached checkpoint writes can serve same-node restores without another
RGW GET; another node can still read the durable blocks from RGW.

The cache is a separate hostPath at `/var/cache/<namespace>/<release>/juicefs`
by default, mounted at `/cache` in the client. JuiceFS adds the volume UUID below
it. Use `juicefs.cacheHostPath` for a dedicated local XFS, ext4 or Btrfs disk.
The startup check rejects shared/FUSE, tmpfs and container overlay filesystems;
the chart rejects paths overlapping the shared mount or runtime/Ceph state.
Budget 4 GiB per client plus filesystem overhead and disk headroom. Cached blocks
are disposable and JuiceFS evicts them; they are not authoritative artifacts.

Set `juicefs.cacheMiB: 0` and `juicefs.cacheLargeWrite: false` to reproduce the
no-block-cache baseline. Linux page cache still exists. Managed Ceph sets the
Beast frontend to `tcp_nodelay=1` through `ceph.gatewayTcpNoDelay`; changing it
restarts RGW. Record cache/readahead settings in benchmarks. The original
[akernel-fs](https://gitcode.com/robbluo/akernel-fs) workload used 32 MiB
readahead; use that value when reproducing its baseline.

## Connect AKernel through Helm

After all mount clients are ready, use [juicefs-values.yaml](juicefs-values.yaml):

```sh
helm template akernel deploy/akernel/charts/core --namespace akernel \
  -f deploy/shared-storage/juicefs-values.yaml \
  --set image.repository=YOUR_REGISTRY/akernel \
  --set image.tag=YOUR_TESTED_TAG > /tmp/akernel-juicefs.yaml
```

Review the rendered configuration and install it with `helm upgrade --install`
using the same arguments. For the umbrella chart, nest these values under
`core:`. AFS uses the same values with `hostPath` pointing at its actual mount.
Host mounts must exist before AKernel Pods start. For restricted client placement,
set `adx.checkpointStorage.nodeSelector` to the same effective selector on direct
Helm installs; the chart applies it to execution nodes and Coordinator. Terraform
sets it automatically for managed JuiceFS. A CSI claim can instead be
selected by clearing `hostPath` and setting `existingClaim`.

Only nodes and Coordinator mount checkpoint storage. Coordinator receives a
matching `checkpoint_reclaimers` entry. Ingress/API Server has no shared mount.
Node initialization rejects local-directory substitutes, read-only, covered or
ambiguous mounts, and runtime paths or symlinks into the checkpoint mount.
Custom sandboxd templates must explicitly set `rootDir`, `storeDir`, and
`filestore_dir`. Initialization preserves the original sandboxd configuration
and its local-disk Firecracker I/O policy.

Helm and standalone enable Execd-to-adxlet runtime control-stream notifications.
Checkpoint requests enter the Environment's serialized queue when pushed;
reconnection reports pending state again. Helm advertises the node Pod IP on
`adx.runtimeControl.port` (19003). Standalone advertises the default sandbox
bridge gateway `10.88.0.1`; update its advertised address for a custom bridge.
Keep `/home/akernel/adx/runtime-control-key` on local persistent storage. Network
rules must allow sandboxes to reach this node endpoint.

## Validate and operate

Save evidence from the deployed system:

| Scenario | Required evidence |
| --- | --- |
| Storage readiness | Ceph health, RGW bucket, PostgreSQL readiness, successful format Job, and a real JuiceFS mount on every required host |
| Basic Sandbox | Public SDK create, commands, file operations, published-port access and deletion |
| Checkpoint and same-node restore | Shared artifact paths, process/state continuity, resume return, first service response, cache hit and RGW GET deltas |
| Cross-node restore | Placement on the second physical host and reads from the shared recovery point |
| Concurrent wakeup | Multiple Ingress requests succeed through one physical restore |
| References and reclamation | Running COW inputs remain readable; Coordinator reclaims anonymous artifacts after Environment deletion |

The public AKernel SDK retains `Sandbox`, `failover`, `reload()` and HostPath.
Use matching ADX administrative tools for complete lifecycle/fault probes; see
[the ADX runbook](https://github.com/openJiuwen-ai/agent-dx/blob/master/docs/testing/shared-filesystem-runbook.md).
Configuration tests and API return are separate from real storage acceptance
and full application-state verification.

Drain consumers before unmounting, replacing a client, or changing storage
identity. Changes to cache arguments also require this drain/remount sequence.
Clients use `OnDelete` updates so a chart upgrade does not immediately
replace live FUSE mounts. An upgrade that changes the client Pod template waits
at the readiness hook until the clients are explicitly replaced. Drain all
consumers first, then replace clients on each affected host while the upgrade
waits (or retry after its timeout). Wait for the new clients' mount probes and
the readiness hook to pass. Recreate ADX Pods after remounting to pin the new
mount identity; only then admit workloads. Local runtime files remain
sandboxd-owned. JuiceFS trash and block GC,
and AFS's COW block GC, run separately from Coordinator artifact deletion.

PostgreSQL credentials/PVC, Ceph cluster/pools, the bucket and storage namespace
are retained on chart removal. Back up metadata, data, credentials and manifests
before explicit storage teardown. Reinstalling a retained stack requires
reusing its resources and credentials; do not format an unrelated existing
volume. Firecracker checkpoints retain their saved disk I/O engine, and old
runsc checkpoints retain their original rootfs annotations. Drain and recreate
recovery points before an incompatible runtime or path migration.

References: [JuiceFS format semantics](https://juicefs.com/docs/community/command_reference/),
[Rook bucket claims](https://rook.io/docs/rook/latest/Storage-Configuration/Object-Storage-RGW/ceph-object-bucket-claim/).
