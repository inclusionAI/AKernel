# Managed image qualification

`qualify.py` runs candidate Coordinator and API Server binaries with a dedicated Redis Unix socket and a new loopback-only Distribution container. It publishes owned OCI manifest/index fixtures with shared and unique blobs, calls the public SDK ownership API, exercises dry-run and manifest deletion, stops writers, runs Registry GC dry-run and GC, measures allocated bytes, then verifies surviving manifests and blobs. It never targets an existing Registry or AKernel deployment. Its fixture is a Registry storage/metadata test, not a bootable Nydus rootfs or a native checkpoint/restore test.

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
