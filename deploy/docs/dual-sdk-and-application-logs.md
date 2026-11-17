# One native and E2B deployment with persistent application logs

Use one Coordinator, API/Ingress deployment and runsc worker pool. Set
`adx.e2b.enabled=true` only with the matching envd-enabled all-in-one image.
Record the platform/Execd commit, both archive hashes, envd version/binary hash,
runtime-rootfs hash and full image digest. See [packaging](../../builder/README.md).

`adx.e2b` adds the protocol configuration, trusted envd metadata on the same
API/worker RuntimeProfile, and E2B management routes on the existing Ingress.
ADX removes envd settings for native environments and enables them only for
specifications containing an envd service. This is not proof of compatibility:
qualify both SDKs together, including authentication, PTY, published ports,
pause/connect, snapshots and cross-node restore. The existing native routes,
Service, credentials, Redis and checkpoint storage remain in use.

Require exact observed Relay source IPv4 addresses in `envdIngressSources`.
The usual bridge address is `10.88.0.1`; never substitute a broad CIDR or a guest
SNAT egress address. Qualify actual same-node and cross-node guest attempts to
reach envd TCP49983. E2B is experimental: do not advertise Code Interpreter,
Desktop, arbitrary caller-supplied HostPath, private-registry credentials in
Template requests, or unsupported template build steps as implemented.
Existing node registry credentials remain usable by the shared image backend;
qualify the actual private image rather than assuming it from public-image tests.

## Persistent log configuration

Enable `node.applicationLogs` on a host-backed node with an existing XFS data
mount. The default backing store is `/home/akernel/shared/app-logs`. The formal
host, Node Pod and sandbox entrypoint is `/app/logs`. Logs are independent of
`node.hostDiskPath`, checkpoint namespaces/caches, runtime image and SDK.
They are node-local external application data, not checkpoint contents or a
replicated log filesystem. Keep the original referenced files available during
restore; cross-node recovery does not copy external logs or provide exactly-once
append behavior.

The initializer validates the host mount table and the actual device seen by
its data volume before writing. It refuses a missing mount, wrong filesystem,
unsafe path/symlink or unexpected existing ownership. The data hostPath uses
`Directory`, not `DirectoryOrCreate`. Only the initializer sees the host root
and proc mount: it installs a checked bind and host boot service. The Node Pod
uses the data disk subPath, and the OCI baseline exposes the same fixed log
mount to native and E2B sandboxes. Explicit SDK mounts replace the baseline at
the same destination instead of producing conflicting duplicates. Test explicit
native read-only mounts as well as writable mounts against the exact runtime;
JSON rendering alone is insufficient.

New directory ownership defaults to root:1000 and mode2770. Existing file
owners, modes and xattrs are preserved; no recursive chmod/chown or world
permissions are applied. Qualify the actual business UID/GID and E2B default
`user`. These shared HostPaths are for a trusted business deployment and do not
provide per-tenant filesystem isolation; server-side HostPath authorization is
tracked independently. Mounts are not part of `storage_mb` limits.

```python
from akernel_sdk import Mount, Sandbox

with Sandbox(image=BUSINESS_IMAGE, mounts=[
    Mount(host_path="/app/logs", target="/app/logs", read_only=False),
]) as sandbox:
    print(sandbox.commands.run("printf 'hello\\n' >> /app/logs/business.log").stdout)
```

Read-only is the SDK default. When restoring a native Snapshot captured with an explicit mount, pass the identical `mounts` argument to `Sandbox.from_snapshot(...)`; a changed access mode is not a compatible restore. E2B gets this one deployment-owned path; it does not acquire
an arbitrary HostPath creation parameter.

## Migration and rollback

Inspect the live host source, active sandboxes/open writers, file inventory,
permissions and physical disk. Stop admission and drain all writers first.
Invoke the packaged `prepare-application-logs.py` with an explicit
`--migrate-from <old-relative-directory>` only for a reviewed one-time migration.
It renames the old directory on the same data disk, retains file inodes and
metadata, changes only the declared directory-root ownership/mode and leaves
the legacy host name as a symlink to `/app/logs`. A protected receipt outside
the exposed directory records the original root metadata. No second writable
copy or silent fallback is created. An interrupted migration requires operator
inspection of that receipt before proceeding; normal Pod startup never migrates.

`legacyMountPath` can temporarily expose the same store at an old Node-Pod
source. External native callers must change their HostPath source to `/app/logs`.
Remove the alias only after callers have cut over and the agreed rollback
window has passed; verify it references the canonical data before unlinking it.
Remove only the old log-directory declaration from any custom `prepare-node-directories` init container before enabling the new alias: its symlink guard must not try to recreate the old directory. Preserve unrelated directory declarations. Runtime rollback retains the canonical log binding and initializer. If old
checkpoints predate the OCI log mount, set `mountInSandbox=false` and retain
the original OCI baseline while native callers mount `/app/logs` explicitly. Restore
old runtime/profile/checkpoint identity, not the old version-dependent data layout.
A reversal of the physical migration requires another fully drained window.

## Registry credentials and acceptance

Keep the existing ACR helper, ServiceAccount and admission-injected pull Secrets.
`node.secret.preserveExisting=true` uses live Secret data during a real Helm
upgrade; it prevents stale values from overwriting registry credentials rotated
outside Helm. Offline rendering cannot perform that lookup: hydrate protected
review values from the live Secret and compare fingerprints at deployment.
Set the flag false only for an intentional rotation. Never mount registry
credentials into a business sandbox or include them in logs/Git.

Acceptance includes actual private business image startup, root/default-user
log writes, explicit native RO/RW, sandbox deletion, drained Node Pod replacement
and fresh-node preparation. Keep old checkpoints/artifacts and test compatibility:
identical runsc strings do not prove that an updated EROFS bootstrap can restore
an old checkpoint. Use a reviewed state transition if incompatible.


## Existing IP entry and command lifetime

The default remains TLS for E2B API, envd and application ports. An existing
certificate-matching IP and trusted CA can serve both SDKs without another
Ingress or wildcard DNS. Native SDK callers retain their existing bare
`AKERNEL_SERVER_ADDRESS` and token; native public application URLs may use HTTP.

An ADX release implementing `ADX_DATA_PLANE_INGRESS_E2B_ALLOW_HTTP` can explicitly
opt in with `adx.e2b.enabled=true` and `adx.e2b.allowHttp=true`. E2B callers then
set both `api_url` and `sandbox_url` to the existing `http://IP` origin and use
the existing API key. No CA is used for those requests. This accepts unencrypted
credentials on that network; choose the transport explicitly. HTTP redirects,
forwarded headers and the presence of two API routes do not establish compatibility.
The API key, tenant ownership, per-sandbox envd token, traffic-token/private-port
policy, current route generation, forbidden `/init` and Execd restrictions
remain enforced. Native control/admin and Agent traffic still require TLS.

Use the fixed entry with `E2b-Sandbox-Id` and `E2b-Sandbox-Port` headers for an
application port. A `get_host()` string needs separate wildcard DNS (and a
matching certificate when using TLS); configuring an IP origin does not provision it.
For a transport-only change, use the [control image build](../../builder/README.md#apiingress-only-maintenance-image)
and `adx.ingressApi.image` override. Per-role ADX config checksums avoid unrelated
worker/Coordinator rollouts. For the first migration from the old whole-ConfigMap checksum, explicitly set
`adx.configChecksumMigration.<role>.configSha256` to the SHA256 of that role YAML
and `legacyChecksum` to its existing Pod annotation. Changed role YAML fails
rendering until the pin is removed for a planned rollout. This does not infer
active Pod configuration from a potentially partially updated live ConfigMap.
Compare server-rendered/effective Pod templates and image digests before
maintenance, then verify live Pod UIDs. Remove each migration pin during that
role's next planned, drained upgrade.

For E2B processes that must survive long snapshots, explicitly use
`commands.run(..., background=True, timeout=0)`. The published SDK's default
command timeout is independent of sandbox expiry and checkpoint state. Restore
an unchanged, already-running workload and verify its original memory rather
than launching a replacement process.

Provision the data disk before a new worker joins, with its stable XFS mount
at `applicationLogs.dataDiskRoot`. Keep the steady-state worker selector tied
to the node-pool role, not a one-off runtime-generation label absent from the
pool template. The log initializer refuses an unmounted disk; it does not
format a device or silently create a system-disk directory. Qualify empty-disk
initialization separately from a drained Pod replacement and actual host reboot.
