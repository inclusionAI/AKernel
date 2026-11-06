# Pin the guest runtime rootfs during platform upgrades

Native runsc snapshots refer to the read-only EROFS runtime mounted at `/__adx`. Keeping the same path or Execd version is insufficient: rebuilding an EROFS image can change its UUID, build time and superblock. Restore must receive the original filesystem bytes. Runsc's superblock verification remains enabled.

Use `node.runtimeRootfsPin` to upgrade control-plane or worker executables while retaining a qualified guest runtime. Harbor configuration does not change. This setting applies to the Execd-only Kubernetes profile; E2B is rejected because its additional envd compatibility requirements are not qualified here.

Before enabling the pin, qualify a digest-pinned source image containing Python 3, `/opt/akernel/runtime/akernel-runtime-rootfs.img` and `/opt/adx/current/runtime/adx-execd`. Record the runtime file's SHA256 and the static Execd SHA256 from that image, and confirm that the source runtime contains that executable. The previously deployed, qualified all-in-one image can supply these artifacts. By default the new platform must contain the same static Execd bytes.

When a published platform bundle changes its packaged Execd, an operator may explicitly set `node.runtimeRootfsPin.currentExecdSha256` to the separately verified carrier executable's SHA256 after qualifying that exact new platform with the old guest runtime and pre-upgrade snapshots. The source initializer still verifies `execdSha256`; only the new-platform initializer uses `currentExecdSha256`. Empty values preserve the original equal-hash check. This does not replace the guest executable, alter the cached rootfs or bypass runsc filesystem checks. Different hashes alone are not evidence of protocol compatibility: retain an explicit compatibility record and the original artifacts.

The rootfs pin preserves filesystem identity only. It does not provide cross-version runsc checkpoint compatibility: runsc requires its checkpoint version to match the restoring binary. Retain the original complete runsc bundle and guest-rootfs tuple for old snapshots, or explicitly drain that snapshot generation before upgrading the active runtime. Do not bypass the runtime version check.

The enabled worker pool must use the same architecture and qualified runtime tuple. This single pin does not configure heterogeneous runtime generations or automatically select an older runtime for a legacy snapshot.

```bash
helm upgrade akernel deploy/akernel/charts/core \
  -f /path/to/operator-values.yaml \
  --set node.runtimeRootfsPin.enabled=true \
  --set-string node.runtimeRootfsPin.sourceImage="$RUNTIME_SOURCE_IMAGE" \
  --set-string node.runtimeRootfsPin.sha256="$RUNTIME_ROOTFS_SHA256" \
  --set-string node.runtimeRootfsPin.execdSha256="$RUNTIME_EXECD_SHA256"
```

`RUNTIME_SOURCE_IMAGE` must include an immutable `@sha256:` reference. Preserve this tuple in operator deployment values through subsequent control-plane upgrades. Prepare and review the rendered change, drain execution according to the deployment's shutdown procedure, and qualify a snapshot captured before the upgrade against the new workers before resuming submissions.

The `stage-runtime-rootfs` initializer runs only the checked-in installer in the source image; it does not start old AKernel services. It verifies the source Execd and runtime image, then installs the image once under the existing node data disk at `runtime-rootfs/<sha256>.img`. Publication is atomic and durable; an existing corrupted file is rejected rather than replaced. The `verify-runtime-rootfs` initializer uses the new worker image and checks its Execd and the cached runtime. The main worker mounts that file read-only at the existing runtime path. Snapshot restore therefore sees the same EROFS identity without copying the immutable runtime into every checkpoint.

Cache files survive worker Pod replacement and remain separate from the image-manager directory. They are not automatically garbage-collected. Retain the source image and cached files while running environments or snapshots depend on them. A newly created node must be able to pull the source image.

Changing guest runtime contents, the Execd executable, or this pin requires a separate compatibility and retention plan. This option does not migrate arbitrary old snapshots, reconstruct missing original images, make writable host mounts rollbackable, or qualify cross-node checkpoint transfer. Keep older required runtime artifacts available; do not bypass filesystem verification to load a snapshot with different bytes.

The chart feature is disabled by default, preserving existing deployment behavior. Operators using snapshots across platform upgrades should explicitly maintain a qualified runtime pin rather than relying on an incidental Docker build-cache hit.
