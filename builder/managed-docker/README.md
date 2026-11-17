# Managed Docker runtime

This opt-in runtime profile delegates Dockerfile/Compose semantics to Docker's official Buildx remote driver and Compose/Bake frontend. It connects to an operator-provided BuildKit service without changing that service's deployment. `compose.py` changes publication targets and generates a separate image-reference override; it does not rewrite submitted task files or interpret Dockerfile instructions.

The runtime tool image contains Docker, Compose, Buildx and the AKernel snapshotter/registration helper. Session endpoints and credentials are injected at launch, outside the image. Buildx pushes native Nydus OCI artifacts directly to an internal CNCF Distribution Registry and returns digest references. No OCI archive passes through the Harbor client and no task artifact is published to ACR.

Build targets are matched to services through native Compose service labels,
preserving case and punctuation. Image-only projects need no native build
targets; their images still pass through the native format-conversion exporter.
Publication names are unique within each invocation, and `outputs.json` retains
the service-to-target/reference mapping used to read Buildx results. Image
references are opaque handles; consumers must not derive them by lowercasing a
service name. A missing or ambiguous build target is an error, even when the
service also declares an image.

`image_provider.py` runs on the runtime node. Its session capability accepts only immutable references in an operator-authorized Registry/repository prefix. It validates OCI metadata, calls sandboxd's private image-management Unix socket, and exports existing ROFS through a read-only collection. Dragonfly and the node's existing image manager own content distribution/caching; this adapter does not maintain a second layer cache. `akernel-image-bridge` imports checked metadata and committed roots into inner containerd; unknown extraction roots still fail closed. Private service uppers remain inside the outer sandbox's checkpoint boundary.

The Registry requires private routes from build workers for push and from runtime nodes for pull. Harbor clients do not require direct Registry access or Kubernetes credentials. Registry credentials are separate from AKernel API keys. Use TLS with a trust chain available to build workers, runtime nodes and the configured distribution path. Private HTTP requires an explicit isolated-network policy and compatible Dragonfly configuration. Standard scoped Bearer authentication can retain access control without applying a password hash to each manifest/blob request. `registry.insecure` is an explicit operator option, not a default.

BuildKit owns cache validity. Worker-local caches reuse steps on the same worker. Cache persistence and request affinity depend on the BuildKit deployment and are not guaranteed by this adapter. Cross-worker registry cache import/export is a separate native Buildx policy; runtime Nydus artifacts must not be represented as BuildKit cache records. ROFS and Dragonfly cache final content independently from build-step caches.

The runtime retains streamed Buildx diagnostics and allows at most three attempts
of the same native Bake command when Buildx definitively exits1 with the terminal
`failed to get compression blob "nydus": not found` export error, including its
`merge nydus layers: get compression blob` variant. Any native
`no-cache` target limits the operation to one attempt. Build failures, access
errors, process/transport exceptions, and signal exits propagate directly. This
is bounded recovery for that specific exporter/cache failure; it does not
repair the BuildKit implementation or establish an exactly-once contract for
external side effects in Dockerfile instructions. Final metadata and runtime
image registration are used only after a successful Bake result.

`ComposeProject.bind_runtime(sandbox, descriptor)` binds a newly prepared runtime and permits initial peer-restricted tunnel creation. `ComposeProject.attach()` remains the restore binding: it must not start daemons, build, run Compose up or create replacement tunnels. `network_mode: service:...` is resolved using Docker's native namespace-owner inspection, preserving the task's network configuration.

Single-container `image-build` returns verified artifact/process metadata through the authorized `/describe` operation without mounting or registering the task rootfs on its temporary build helper's node. The final single-container sandbox mounts that image through the ordinary runtime path. Compose `/prepare` still registers the ROFS lower required by inner containerd. Both operations retain digest, platform and repository-prefix validation and use the same session capability.

Versioned managed collections use Sandboxd's node-local live/checkpoint leases. The broker holds the collection's `flock` during each operation and sends its explicit owner to the private image-management API. The last live/in-flight/retained-checkpoint reference releases exported roots and management ownership while preserving ordinary rootfs consumers; retained managed checkpoints intentionally keep dependencies alive and count toward the node's ROFS budget. Missing/copied checkpoint markers fail closed. Legacy collections remain pinned for an operator drain because their history cannot be inferred safely. Deploy the broker and Sandboxd together: the new management API rejects anonymous ownership, and old Sandboxd rejects the new owner field. See Sandboxd's checkpoint/restore contract for local-filesystem and recovery requirements. Cross-node reconstruction and Registry artifact GC remain separate work.

Run adapter regressions with `python3 -m unittest discover -s builder/managed-docker -p 'test_*.py'`. SDK changes additionally require `make sdk-check`; helper changes require `go test ./...` in `builder/image-bridge`. Use an owned namespace/data directory and delete sandbox/snapshot records before unmounting collections or deleting Registry storage.

To also validate plans with official Compose and Buildx, supply executable paths
from the selected runtime toolchain:

```sh
AKERNEL_TEST_DOCKER_COMPOSE=/path/to/docker-compose \
AKERNEL_TEST_BUILDX=/path/to/docker-buildx \
python3 -m unittest discover -s builder/managed-docker -p 'test_*.py'
```

These additional tests use an empty client configuration and an unavailable
Docker socket. They exercise native parsing and `bake --print`, without a
daemon, builds, Registry traffic or cluster changes. Real publication and
checkpoint/restore require separate runtime integration tests.

## Platform-owned profile

Native builds publish to a fresh invocation tag and return immutable digests.
If Buildx exits with the specific post-publication `unknown blob ... in history`
error, the runtime inspects every planned output with the official Docker CLI
and verifies its raw manifest digest, size and platform. It adopts the published
outputs only after all checks pass, records the original error in a publication
receipt, and does not replay the build. A separate exact internal-deadline case
requires completed native manifest-push acknowledgements for every target and
matching Registry digests; explicit attestations are ineligible for this recovery.
Buildx exit 100 alone does not establish publication. Metadata-file provenance
defaults to Docker's supported `disabled` mode; an explicit operator setting is
preserved. This skips the optional client history lookup, without claiming to
repair server-side history or storage failures. Missing or partial publication, ordinary
build errors, authentication failures and unknown command outcomes still fail.

Managed builds export the original native Docker/Buildx targets directly. They do not append an application interpreter, synthetic harness layer or PATH entries. Whole-sandbox checkpoint/restore is performed externally by Sandboxd/runsc and requires no Python or process-tree stop/continue in task images. Runtime control tools may use Python in the platform's own tools image; this is separate from application image requirements.

`registry_auth_file` accepts Sandboxd's flat credential map or a standard Docker `auths` configuration for the platform-owned build client. Configure the image manager with a separate pull-only credential; do not give its account push permissions. With standard Registry token authentication, Buildx, the image manager and Nydus exchange credentials for short-lived, repository-scoped Bearer tokens. The private Registry and issuer require authenticated access; private networking is not an authorization policy.

The runtime broker delegates bounded, digest-addressed manifest/config reads to Sandboxd's private `/registry_metadata` Unix interface. Sandboxd reuses `go-containerregistry`'s standard challenge, scoped token reuse and refresh implementation. Neither the SDK nor the Python broker parses credentials or implements a second Bearer protocol. Metadata size/digest validation and collection repository-prefix checks remain mandatory; blob data continues through the existing Dragonfly/ROFS path. The Unix interface is platform-only and is never mounted in a sandbox.

See [the operator-only Sandboxd configuration](profile.example.toml).

Size the control plane separately from task sandboxes. The
[core-chart capacity overlay](control-capacity.example.yaml) provides an example
Coordinator budget for environment catalogs and retained snapshot tombstones. It changes no task resource limits and is not a concurrency
guarantee. Apply it as an operator Helm values file, nested under `core` when
using the umbrella chart. Track Coordinator memory, restarts and request latency;
catalog filtering fixes do not eliminate the need for a suitable control budget.

Harbor's maintained managed path sends `managed_runtime_profile="harbor-v1"`.
`DockerRuntime(profile=...)` forwards only that capability in `extra_config`.
Sandboxd authorizes the trusted ADX tenant label against the operator allowlist,
chooses the pinned tool image and creates a distinct collection for each fresh
environment, including independent verifiers. It injects readonly configuration,
credentials and collection mounts; client-selected reserved mounts are rejected.

Package `provider_service.py` and `image_provider.py` on the runtime node. The
all-in-one image includes the opt-in `akernel-managed-images.service`; operators
supply `/etc/akernel/managed-runtime.env` containing `MANAGED_RUNTIME_ROOT`,
`MANAGED_IMAGE_SOCKET`, `MANAGED_IMAGE_PORT`, configure matching Sandboxd
`[plugin.managed_runtime]` / `[plugin.managed_runtime.profiles.harbor-v1]`, then
run `systemctl enable --now akernel-managed-images.service`. No unit/profile is enabled by default. Keep the private broker
and Registry on reachable internal routes. Never expose the image Unix socket.

The Sandboxd profile owns `image` (digest pinned), `registry`, `repository_prefix`,
`registry_auth_file`, `registry_insecure` (explicit private-HTTP configuration only),
`buildkit_endpoint`, `provider_url`, `bootstrap_root`, `allowed_tenants`, `writable_mb`, and `minimum_root_mb` (defaults to 4096 MiB). The total writable-storage floor is the image upper plus this root-layer reservation. Before starting each worker, run `python3 builder/managed-docker/profile_metadata.py /path/to/sandboxd.toml` and merge the resulting labels into its ADXlet service `config.labels`. Labels contain only profile names and minimum MiB; regenerate them with every storage-geometry change. The SDK queries this authenticated resource metadata using its own connection before allocating a managed sandbox, reserves the default floor, and adds a hard placement condition for compatible workers. Explicit requests or limits below the floor fail before creation. Sandboxd checks the actual total quota again before any collection filesystem changes. Ordinary image sandboxes do not perform this preflight.

Managed Compose inputs are frozen and uploaded unchanged. The runtime's official `docker compose config` resolves services after startup, including native `!reset` and `!override` tags; the SDK does not parse or merge managed YAML. Call `start_runtime()` before obtaining service handles.

The snapshot compatibility fingerprint excludes `allowed_tenants`. Restore still checks the tenant against the current allowlist first: expanding or reordering authorization preserves compatibility; revocation denies restore. Tool, bootstrap, storage and other captured runtime configuration changes remain incompatible. An incompatible fingerprint/schema change requires draining the affected snapshot generation before a profile upgrade. Node-local lease recovery does not imply cross-node portability or a qualified arbitrary runtime upgrade.
The operator, rather than the Harbor user, builds and publishes the tool image
from this directory and supplies the matching image-bridge binary in its context.

Checkpoint output includes `managed-runtime.json` with an opaque collection ID,
tenant and profile fingerprint, without credentials or node paths. Restore checks
this server-created metadata and reuses the retained collection. Missing or changed
profile/collection fails closed. It must not rebuild, restart Docker or substitute
an empty collection. Versioned checkpoints additionally retain their local collection
lease marker; collection leases and checkpoint output must share a filesystem.
Restore is same-node only. Per-tenant Registry credential issuance, cross-node collection reconstruction
and automatic Registry artifact GC are not provided by this profile.

## Packaging and compatibility

Build the all-in-one image with the repository's maintained build helper. The
Sandboxd gitlink and runtime manifests select the runtime implementation; the
Agent DX source reference and platform/Execd URL/checksum pairs must identify the
same published source. Verify complete package manifests, architecture and
matching platform/component Execd bytes before packaging. Downloaded optional
AFS/envd payloads do not enable those services.

For control-only upgrades, retain the deployment's qualified guest-rootfs pin
and complete compatible runsc bundle while snapshots depend on them. Changing
the guest runtime, provider identity or captured profile requires an explicit
compatibility and retention plan. See
[runtime-rootfs pinning](../../deploy/docs/runtime-rootfs-pinning.md).

Record tested sources, image digests, workload results and cleanup in release
qualification evidence. Keep run-specific measurements, PR status and rollout
history outside this operator guide.
