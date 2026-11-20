# Managed image qualification

`qualify.py` runs candidate Coordinator and API Server binaries with a dedicated Redis Unix socket and a new loopback-only Distribution container. It publishes owned OCI manifest/index fixtures with shared and unique blobs, calls the public SDK ownership API, settles a fenced partial publication and exercises dry-run and resumable manifest deletion. It stops writers, runs Registry GC dry-run and GC, measures allocated bytes and verifies surviving manifests and blobs.

Prepare the candidate ADX binaries, Redis and short-lived mTLS certificates using the ADX repository's documented build/CI tools. Install this checkout's SDK and its dependencies. Select the Registry image explicitly; keep the resulting image ID and binary source versions with the qualification receipt.

```sh
.venv/bin/python deploy/tests/managed_images/qualify.py \
  --coordinator /path/to/adx-coordinator \
  --apiserver /path/to/adx-apiserver \
  --redis /path/to/redis-server \
  --tls-dir /path/to/isolated-test-certificates \
  --registry-image registry:3 \
  --output /path/to/new-owned-evidence-directory
```

The process stops/removes only its own named Registry container and terminates its own child services. Its output directory, including Redis AOF, Registry data, configurations and private test key, is retained for inspection. Do not commit the directory. If interrupted outside normal cleanup, inspect the unique container and child identities before resuming or removing anything. Native qualification additionally needs an owned managed-profile backend with the candidate runtime tools: build → run → capture → release publisher → delete source → restore and read image data → delete all consumers → observe grace → manifest deletion → no-writer GC. This storage fixture does not substitute for that test.

`native.py` performs that native qualification with ordinary Nydus roots and multi-service managed Compose. It creates a fresh authenticated Registry, Nydus BuildKit, all-in-one node, Docker network and loop-backed XFS filesystem. It accepts no existing service endpoint. Select complete matching node/tools images explicitly; the node must include the supporting control plane and matching static Execd/bootstrap rootfs. Local candidates and formally released artifacts must be identified separately in the receipt.

The native driver requires Linux root privileges, Docker, loop/mount/XFS tools, `/dev/fuse`, `/dev/net/tun`, and a previously verified dedicated data disk. Install the SDK and the integration-only requirements; ordinary SDK and adapter tests do not require bcrypt. All selected images must already exist in the local Docker daemon and match the native architecture.

```sh
uv pip install --python .venv/bin/python -r deploy/tests/managed_images/requirements-native.txt
.venv/bin/python deploy/tests/managed_images/native.py \
  --node-image <complete-matching-all-in-one-image> \
  --tool-image <current-managed-tools-image> \
  --input-image <native-python-fixture-image> \
  --registry-image registry:3 \
  --buildkit-image <nydus-enabled-buildkit-image> \
  --disk-uuid <verified-dedicated-disk-uuid> \
  --data-root /path/on/that/disk/to/new-profile \
  --output /path/to/new-evidence-directory
```

For both workload forms it captures a Snapshot, mutates the source afterward, releases publisher ownership and deletes the source. It verifies retained dependencies across restore, including omitted Compose publication metadata and a control-plane restart. The original process must retain its nonce/PID/one launch and advance its counter after the first restored sample; source writes made after capture must be absent. Only after deleting all consumers does the driver observe grace, invoke the operator CLI against exact publication manifests, stop writers, run GC dry-run/GC, verify surviving blob hashes and start a fresh retained-image consumer.

The driver refuses nonempty data and existing backing files. Cleanup verifies container labels and loop backing identities, removes only owned services/network/mounts, and retains backing files plus protected credentials/evidence. Use fresh paths for every attempt; an error or failed cleanup makes the run fail. The Registry uses private HTTP only on the newly created network; this is not production TLS, cross-node migration or E2B qualification. Production activation still requires its reviewed release/configuration and storage-domain maintenance policy.
