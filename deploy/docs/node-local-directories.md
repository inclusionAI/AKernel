# Node-local application directories

Operators can prepare dedicated directories through the existing `node.initContainers` extension, then bind an approved source such as `/home/akernel/shared/app-logs` at a sandbox target such as `/app/logs`. Directory preparation belongs to trusted deployment configuration. Source authorization remains a separate server-side responsibility ([#110](https://github.com/inclusionAI/AKernel/issues/110)).

## Configuration

Use the complete [values overlay](../akernel/examples/node-local-directories.yaml). It enables `node.directoryHelper.enabled` and mounts the packaged helper and `home-disk` into an init container using Python from the existing node image. The example runs as root with limited capabilities; `FSETID` preserves shared-group modes such as `2770` when the target GID is outside the initializer's groups.

1. Select the existing node storage with `node.hostDiskPath` or `node.homeDisk.useEphemeralPVC` and its StorageClass/size. These settings select the entire `home-disk` volume. Verify the real host disk before starting host-backed Pods: Kubernetes `DirectoryOrCreate` can otherwise create a source on the host root filesystem. The helper verifies that `--root` is a container mountpoint, not the identity of its physical disk.
2. Set the initializer image to the same tested image/digest as the node workload. `node.initContainers` contains literal Kubernetes configuration; Helm expressions in its values are not evaluated. Merge the example into any existing initializer list, since Helm replaces lists. The built-in `migrate-adx-state` runs first.
3. Configure `--directories-json` with explicit `path`, `uid`, `gid`, and `mode` for each directory. Paths are relative to the mounted `--root`; modes are four-digit octal strings, for example `0770` or `2770`. Use a dedicated application subtree outside AKernel's `adx`, `sandboxd`, `images`, and `filestore` state.

The overlay uses the umbrella chart's `core:` wrapper. Remove that wrapper for the core chart alone. Render the complete deployment values before applying:

```bash
helm template akernel deploy/akernel -n akernel \
  -f /path/to/deployment-values.yaml \
  -f /path/to/reviewed-node-directories.yaml
```

The helper is disabled by default, preserving the existing node ConfigMap and rollout checksum. Enabling it packages the script; the initializer is still required to invoke it. Keep the flag enabled while the initializer mounts the script. Initializer or enabled-helper changes roll node Pods, so schedule deployment changes around active workloads.

## Ownership and failure behavior

The helper validates declaration syntax before writing. It rejects traversal, control characters, duplicate or overlapping paths, unknown fields, and invalid UID/GID/modes. Directory-relative descriptors and no-follow opens reject symlinks. The volume root and intermediate directories must belong to the initializer's UID and must not be group/other writable.

New intermediate directories use mode `0755`; new leaves receive their declared UID/GID/mode. Existing leaves must match exactly. Repeated preparation preserves their identity and contents; the helper never recursively changes permissions or deletes data. Permission mismatches report expected and actual metadata in `kubectl logs <node-pod> -c prepare-node-directories`.

An init failure blocks that new node container from starting. Preparation is not transactional: interruption or an ownership error can leave partial directories. Inspect and explicitly reconcile them before retrying. A missing PVC prevents volume setup; an unmounted `--root` is rejected before directory creation. Node startup failure does not guarantee immediate removal of an older scheduler registration.

Directory ownership controls access, not the UID of the application process. Files follow the writer's UID/GID and umask. Mode `2770` preserves a shared directory group; applications and collectors still need suitable membership and file permissions.

## Mounts and retention

Use the SDK's [HostPath interface](../../sdk/python/README.md#node-hostpath-bind-mounts) to bind the prepared source. The selected node Pod must see that source; a path on the SDK client or ECS host alone is insufficient. HostPath is read-only by default, and writable access requires `read_only=False`. Separate workers have separate data; node selection does not replicate it.

| Event | Host-backed `home-disk` | Generic ephemeral PVC `home-disk` |
| --- | --- | --- |
| Sandbox deletion | Directory and data remain | Data remains while the node Pod/volume remains |
| Container restart in the same node Pod | Data remains; init does not normally rerun | Data remains; init does not normally rerun |
| Node Pod replacement | Init checks the same directory if node and disk remain | The old Pod owns its PVC; the replacement normally gets a new claim and empty directories |
| Node or volume loss | No automatic replication or recovery | No durability guarantee; disk deletion follows the PV reclaim policy |

A retained ephemeral PV is not automatically reattached to a replacement Pod. Operators own retention, cleanup, disk capacity, and application coordination; external data is outside the sandbox's `storage_mb` quota. A collector needs authorized access to the same storage: an ephemeral PVC inside the node Pod is not exposed through an unrelated hostPath. Collector attachment, offsets, rotation, and delivery guarantees belong to the collector integration.

## Checkpoint and restore

External directories remain live caller-owned state. Checkpoints do not copy or roll back their contents. Retain the original directories and referenced files with the same mount layout and access modes while restores may need them. `O_APPEND` prevents a restored saved offset from overwriting later appends, but replayed execution can duplicate records. Restore provides neither new log segments nor exactly-once delivery.

Qualify mounts and checkpoint support against the deployed [runtime stack](../../sdk/python/README.md#sandbox-runtimes). The SDK acceptance below targets runsc and uses the deployed internal workload checkpoint mechanism.

## Validation

Run offline lifecycle and core/umbrella rendering tests:

```bash
python -m unittest discover -s deploy/akernel/charts/core/tests -p 'test_*.py' -v
```

The restricted-capabilities test requires Linux root and `setpriv`; CI executes that case explicitly as root. Other tests run without privileges.

Run the [SDK acceptance](../akernel/tests/test_node_directories_live.py) with an independently installed SDK, trusted credentials and TLS configuration, and a disposable source prepared for UID/GID 1000:

```bash
AKERNEL_TEST_NODE_DIRECTORIES=1 \
AKERNEL_TEST_HOSTPATH_SOURCE='<prepared-absolute-disposable-source>' \
AKERNEL_TEST_NODE_ID='<prepared-node-id>' \
AKERNEL_TEST_HOSTPATH_CHECKPOINT=1 \
python -I deploy/akernel/tests/test_node_directories_live.py -v
```

It checks UID/GID, file mode `0640`, RO rejection, retention after sandbox deletion, and runsc checkpoint/reload with an open append FD. Set `AKERNEL_TEST_IMAGE` to an immutable fixture when recording acceptance. Omitting the checkpoint opt-in produces a checkpoint skip. The test deletes its sandboxes and prints instance IDs and its unique file prefix; independently confirm physical cleanup before removing only those files.

For deployment qualification, repeat on each eligible worker, replace an empty node Pod to verify host-backed retention, and use isolated Pods for permission/storage failures and two ephemeral-PVC generations. Record the tested source/image/runtime, claim identities and reclaim policy, skips, results, and cleanup. Keep existing worker storage unchanged during isolated PVC tests.
