# Node-local application directories

Operators can prepare dedicated directories on every eligible AKernel execution node through the existing `core.node.initContainers` extension. A sandbox can then bind an approved source, for example `/home/akernel/shared/app-logs`, at an application-specific guest target such as `/app/logs`. These paths are independent; neither is a required AKernel default.

This is deployment-time preparation for trusted management clients. It does not grant access to a source, provide tenant isolation, or resolve the server-side authorization requirement in [#110](https://github.com/inclusionAI/AKernel/issues/110). Do not let untrusted sandbox requests configure the initializer. The SDK never prepares node directories: `Mount(host_path=...)` sends `create_if_missing=false`.

## Configure the node initializer

The core chart packages `scripts/prepare-node-directories.py` as the `prepare-node-directories.py` key of `akernel-node-config`. The [supported values overlay](../akernel/examples/node-local-directories.yaml) mounts this script and the existing `home-disk` into an unprivileged init container with only the capabilities needed for directory ownership and permission setup. It uses Python 3 from the existing all-in-one image; no new runtime image is required.

1. Choose the backing storage with `core.node.hostDiskPath` or `core.node.homeDisk.useEphemeralPVC` and its StorageClass/size. These settings select the whole existing node `home-disk`; they do not add a separate application disk. Additional volumes require a separate chart extension.
2. Copy the overlay and set its initializer image to the same tested image/digest as the node workload. `node.initContainers` is literal Kubernetes configuration, not a Helm `tpl`: image template expressions in values are not evaluated. Keep any required image pull secrets in the deployment's existing values.
3. Set `--directories-json` to an array of objects with exactly `path`, `uid`, `gid`, and `mode`. Paths are relative to the mounted `--root`. Use a dedicated application subtree, outside AKernel's `adx`, `sandboxd`, `images`, and `filestore` state. Each declaration needs an explicit numeric UID/GID and a four-digit octal mode string, such as `0770` or `2770`.
4. Review the complete rendered node Pod and schedule a maintenance window for existing nodes. Changing init containers changes the Pod template and rolls node Pods; an initializer cannot run retroactively inside an existing Pod. Helm merges replace lists: combine any existing `node.initContainers` with this entry instead of silently dropping them. The built-in `migrate-adx-state` remains first, followed by configured initializers.

For the umbrella chart, merge the overlay with your protected deployment values and render before applying:

```bash
helm template akernel deploy/akernel -n akernel \
  -f /path/to/deployment-values.yaml \
  -f /path/to/reviewed-node-directories.yaml
```

For `deploy/akernel/charts/core` alone, remove the outer `core:` key from the overlay. Without an initializer entry, the chart does not create application directories or add an application initializer. The helper's ConfigMap key is inert until explicitly mounted and invoked.

Kubernetes completes all init containers before starting the node container. A missing/unavailable PVC prevents volume setup; a missing `home-disk` volumeMount is rejected by the helper because `--root` must be an actual mountpoint in Linux `/proc/self/mountinfo`. This check establishes a container volume boundary, not the identity of a physical host disk. In hostPath mode, `DirectoryOrCreate` can create the source on the host's root filesystem if an expected data disk is absent. Operators must verify the host disk mount and provision it before starting node Pods; the initializer does not mount disks or fall back to alternate storage.

## Ownership, retries, and failure

The helper validates the entire declaration list before changing the filesystem. Absolute paths, traversal, control characters, duplicate/overlapping declarations, unknown fields, invalid UID/GID or modes, and symlinks are rejected. The root and intermediate directories must belong to the initializer's UID and must not be group/other writable. The example runs as UID 0. Directory-relative file descriptors and no-follow opens prevent following a substituted symlink.

New intermediate directories are owned by the initializer and have mode `0755`; new leaf directories receive only their declared UID/GID/mode. Existing leaf directories must match exactly. The helper never recursively changes ownership, permissions, or application files, and it never removes directories. A successful repeat reports `exists` and preserves directory identity and contents. A mismatch fails with expected and actual metadata in the init container log. Use `kubectl logs <node-pod> -c prepare-node-directories` to diagnose it.

Preparation is not transactional across directories. A later error, interrupted initializer, or failed ownership operation may leave already-created directories or a partially initialized leaf. Inspect the failure and explicitly reconcile the operator-owned directory before retrying; the helper does not guess whether changing an existing directory is safe. An init failure blocks that new node container from starting; it does not promise immediate removal of an older registration from the scheduler's capacity ledger.

The directory's UID/GID and mode determine access to it. New files are owned by the actual writer and follow the writer's umask; setting a directory UID does not switch a root process to that user. Mode `2770` can preserve a shared group for new files, but applications still need suitable group membership and file modes. Validate the real application UID/GID and the collector's read access. Do not use `0777` as a substitute for this contract.

## Mount and retention contract

For an operator-approved runsc node, public SDK 0.2.0rc3 or newer can create an explicit writable bind:

```python
from akernel_sdk import Mount, Sandbox

with Sandbox(
    node_id="<prepared-node-id>",
    mounts=[Mount(target="/app/logs", host_path="/home/akernel/shared/app-logs", read_only=False)],
) as sandbox:
    print(sandbox.commands.run("ls -ld /app/logs").stdout)
```

HostPath defaults to read-only. The source must be visible inside the selected node Pod's filesystem namespace, not merely exist on the SDK client, controller, or ECS host. Separate workers' directories contain separate data; `node_id` chooses a node and does not replicate files. Sharing a source among sandboxes shares its contents and requires application coordination. Directory preparation does not allocate a per-sandbox log segment, enforce retention, or constrain external data with `storage_mb`. Budget disk space and retention separately from AKernel's private writable filestore.

| Event | Host-backed `home-disk` | Generic ephemeral PVC `home-disk` |
| --- | --- | --- |
| Sandbox deletion | Operator directory and contents remain | Contents remain while the node Pod/volume remains |
| Node container restart in the same Pod | Mounted data remains; init does not normally rerun | Mounted data remains; init does not normally rerun |
| Node Pod replacement | Initializer checks the same host directory when that node and backing disk remain | PVC is owned by the old Pod and is garbage-collected with it; the replacement normally gets a new claim/volume and new empty directories |
| Underlying node/volume loss | No automatic replication, migration, or recovery | No durability guarantee; PV/disk deletion ultimately follows the StorageClass reclaim policy |

An ephemeral PV retained by its reclaim policy is not automatically reattached to the replacement Pod. Use an explicitly managed persistent-storage design when application data must outlive node Pods. This example does not implement one.

A separately managed collector needs its own authorized view of the same backing storage. In hostPath mode that can be the selected host source. An ephemeral PVC's `/home/akernel` path is inside the node Pod; an unrelated hostPath collector does not automatically see it. Collector installation, PVC attachment/sidecar design, offsets, rotation handling, delivery guarantees, and vendor ingestion are outside this feature. Sandbox deletion never owns application-data cleanup; operators retain and remove those files according to their chosen policy and checkpoint dependencies.

## Checkpoint and restore validation

Treat writable external directories as live caller-owned state, separate from the sandbox's private writable root. A checkpoint does not copy or roll back their contents. Retain the original source directories and referenced files, with the same mount layout and access modes, for as long as restores may need them. An empty replacement volume is not equivalent to the original source. For logs, `O_APPEND` avoids overwriting later host writes with a restored saved offset; replayed execution may still create duplicate records. Restore does not automatically create a new log segment or provide exactly-once delivery.

Validate only runtimes supported by the deployed stack. Runsc has local C/R support; Kata and runc do not. The pinned sandboxd also implements directory binds and local C/R for Firecracker with virtio-fs enabled, which requires a matching VMM/kernel/initrd/virtiofsd stack. Existing AKernel images may carry a different sandboxd pin; do not infer support from a runtime name or a chart update. Start with runsc for the example and qualify additional runtimes separately.

The pinned sandboxd `test/e2e/host-mount-rw.sh` exercises mixed RO/RW binds, rotation of an open append FD, checkpoint/delete/restore, retained read offsets and process memory, post-checkpoint host changes, and source retention. Reuse it for runtime qualification. An SDK-level acceptance should additionally bind the prepared source, create a checkpoint using the deployed internal workload checkpoint mechanism, reload the same logical sandbox, verify continued writes and unchanged external data, and clean up only its own instances/files. The checkpoint mechanism is not a new public SDK API.

The maintained [SDK acceptance](../akernel/tests/test_node_directories_live.py) exercises the example's UID/GID 1000, file mode `0640`, RO rejection, retention after sandbox deletion, and optionally runsc checkpoint/reload with a live append FD. Run it with an independently installed SDK, a dedicated disposable prepared source, trusted credentials, and the configured TLS policy:

```bash
AKERNEL_TEST_NODE_DIRECTORIES=1 \
AKERNEL_TEST_HOSTPATH_SOURCE=/home/akernel/shared/app-logs \
AKERNEL_TEST_NODE_ID='<prepared-node-id>' \
AKERNEL_TEST_HOSTPATH_CHECKPOINT=1 \
python -I deploy/akernel/tests/test_node_directories_live.py -v
```

The test defaults to `python:3.12-slim`; set `AKERNEL_TEST_IMAGE` to a tested immutable image fixture when recording acceptance. Omit the checkpoint opt-in when only checking the basic directory lifecycle; that produces an explicit checkpoint skip. Each run prints its instance IDs and unique `directory-<UUID>` file prefix. It deletes its sandboxes, leaves those files for independent node-side inspection, and never removes host data. Confirm physical sandbox/mount cleanup before removing only the run's files. Repeat against each eligible worker and record any skips as untested coverage.

## Validation checklist

- Render the core and umbrella examples with hostPath and ephemeral PVC backing; verify script bytes, volume mounts, initializer order, and unchanged unconfigured behavior.
- Run `python deploy/akernel/charts/core/tests/test_node_directories.py -v` and the existing chart tests. They exercise real temporary directories without requiring a mounted volume; runtime mountpoint verification is tested separately.
- On a permitted test cluster, check every eligible worker's prepared directory, actual UID/GID/mode, writable access as the intended application user, RO rejection, and node-visible data after deleting the sandbox.
- Replace a node Pod while empty, confirm the initializer reruns, and check the documented host-backed retention. Use an independent small ephemeral PVC test to verify old-Pod claim cleanup and replacement isolation; do not switch an existing node's storage mode merely to test retention.
- Exercise permission mismatch and unavailable backing storage using isolated test Pods. Do not deliberately break the running node DaemonSet.
- Run supported C/R acceptance separately and record the actual image/runtime pin. Capture failures and remove only owned test files/volumes after all sandbox mounts are released.
