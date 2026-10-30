# AGENTS.md

This is the tool-neutral project instruction entry point for coding agents.
Tool-specific compatibility files should point here rather than duplicate
project guidance.

## Project Overview

AKernel provides standalone and cluster-backed remote sandbox environments for agents and
developer workflows. The current public user-facing surface is the Python
`akernel-sdk`, including the `akernel_sdk.Sandbox` API and the `ak` CLI.
The default sandbox runtime is gVisor runsc. Runtime identifiers and generic
JSON-compatible runtime configuration are forwarded to the selected backend,
which owns availability and compatibility checks. The bundled deployment also
advertises Kata Containers and Firecracker on KVM-capable nodes. The native
Linux runc payload is build-time optional and must be explicitly included and
enabled by an operator.
SDK service certificate verification is configured with `AKERNEL_VERIFY_TLS`
(default off); private trust uses `SSL_CERT_FILE`. The `ak` CLI supports global
`--verify-tls` / `--no-verify-tls` and `--ca-file` options. Keep SDK connection,
PTY, inventory and resource-query TLS policy consistent through `_tls.py`.
Trust-source loading follows the underlying transport: HTTPX explicitly loads
`SSL_CERT_FILE` ahead of `SSL_CERT_DIR`, while Python SSL/OpenSSL paths use
default trust paths and can validate through a CA directory despite an invalid
file. `--ca-file` sets the trust-file environment variable rather than selecting
an exclusive trust store. Preserve this documented distinction and its real
TLS regression coverage; the SDK README owns the detailed contract.
Creation-time network policies and atomic runtime replacement support
unrestricted networking, blocking new flows except the control-plane and
published sandbox-port routes, or denying exact and leading-wildcard DNS names.
Experimental whole-device NVIDIA GPU requests require runsc. Configurable
writable-storage requests are supported by runsc and Firecracker.

Use AKernel when a task needs an isolated remote environment with command
execution, file operations, interactive PTYs, port forwarding, or reverse
tunnels. The project overview and deployment quick start are in
[`README.md`](./README.md), detailed SDK documentation is in
[`sdk/python/README.md`](./sdk/python/README.md), and runnable examples are in
[`sdk/python/examples/`](./sdk/python/examples/).

## Source Layout

- `sdk/python/` - AKernel Python SDK and CLI.
- `sdk/python/akernel_sdk/` - SDK implementation for `Sandbox`, commands,
  filesystem, PTY support, instance plumbing, and CLI helpers.
- `sdk/python/akernel_sdk/_dockerfile_launch.py` - lightweight public
  Dockerfile direct-launch configuration, independent of the parser and backend.
- `sdk/python/examples/` - maintained AKernel SDK examples.
- `sdk/python/tests/` - maintained AKernel SDK tests.
- `sdk/python/benchmarks/` - source-run end-to-end performance, pressure, and
  mixed-load drivers; their driver tests run with `make sdk-test`.
- `builder/` - Dockerfiles, service configs, runtime rootfs build, and image
  entrypoint scripts for the public all-in-one image.
- `deploy/` - Helm charts, standalone scripts, Terraform modules, and deployment helper scripts.
- `deploy/standalone/manage.py` - source-build standalone lifecycle shared by Linux and Apple Silicon Mac with OrbStack.
- `assets/` - static images used by the root README.
- `src/agent-dx/` - Agent DX source reference maintained as a Git submodule.

The open-source AKernel repository contains the SDK, deployment configuration,
build tooling, and examples. Node runtime components such as `sandboxd` and
`distill-fs` are maintained in their own upstream repositories and pinned as
Git submodules. The all-in-one build compiles sandboxd and downloads the
checksum-pinned static distill-fs release recorded in
`builder/distill-fs-versions.env`. The distill-fs submodule is an optional
source reference, not a build input. See the Build section below for runtime
payloads.

ADX platform and Execd binaries come from the release archives pinned in the
Dockerfiles. Archive manifests identify the packaged binaries;
`src/agent-dx/` is a source reference.
The internal adapter uses `adx-environment==0.1.0rc3` and
`/api/environment/v1`, while AKernel retains its public `Sandbox` API.
Keep source installation, CI and publication dependency resolution through
the configured package index.

Named creation reuses an already successful Environment and its retained
resource allocation. Keep the standalone replay regression aligned with that
contract; use `get_info()` to verify retained resources.

## Common Commands

All commands should be run from the repository root.

```bash
make help
make check VENDOR=aliyun
make config VENDOR=aliyun
make build
make standalone
make standalone-status
make standalone-stop
make push
make plan
make deploy
make token
make print-env
make sdk-test
make deploy-script-check
make deploy-standalone-test
make e2e
```

This is a command reference, not an unconditional sequence. Skip `make build`
and `make push` when the deployment profile selects an existing image. The
image repository and tag used by `make build` and `make push` come from the
profile created by `make config`; set both during configuration rather than
overriding only the build command.

`make plan` is read-only with respect to cloud resources. `make deploy` applies
Terraform and Helm changes, while `make destroy` destroys cloud resources.
Agents must show the plan and obtain explicit user approval before running
either mutating command. Do not use `AUTO_APPROVE=1` without that approval.

## Local Deployment State

Interactive deployment helpers write local state under `.akernel/default/` by
default. Pass `ENV=<name>` when you need multiple independent deployment
profiles. These directories are intentionally ignored by Git. They may contain:

- generated Terraform variables
- kubeconfig files and paths
- public HTTPS certificate material
- bootstrap API keys
- SDK environment exports

The source standalone entry point uses `.akernel/standalone/data/` by default, or an absolute `AKERNEL_STANDALONE_DATA_DIR`. It keeps generated runtime configuration, the bootstrap API key, token, `source-state.json`, and `sdk-env.sh` together in that directory. The environment file reads the protected token file when sourced; lifecycle commands print credential paths without their values. This local profile is independent of cloud `ENV` profiles.

Never commit `.akernel/`, Terraform state, kubeconfigs, tokens, signing seeds, cloud credentials, private registry URLs, or local debug artifacts.

## Build

AKernel uses Docker for building. The public distribution ships one all-in-one
image that can run the ADX control role, node role, or standalone topology
depending on the deployment entrypoint and environment.

Image build stages refresh APT indexes through `builder/scripts/apt-get-update.sh`.
Keep its bounded retries, complete-update requirement, and repository
verification intact when changing package installation.

```bash
make build
make build AKERNEL_ENABLE_RUNC=true
make build AKERNEL_ENABLE_FIRECRACKER=false
```

For a build that will be pushed and deployed, set `IMAGE_REPOSITORY` and
`IMAGE_TAG` when creating the deployment profile. A one-off `IMAGE_TAG`
override on `make build` does not update the profile consumed by `make push`.
The build creates only the selected image reference; it does not add a second
`akernel-all-in-one` alias. `make push` pushes that selected reference directly.

The build helper performs two Docker builds. `builder/runtime.Dockerfile`
creates the EXECD-only `akernel-runtime-rootfs.img` from the pinned ADX release.
`builder/node.Dockerfile` then compiles the node components and produces the
AKernel all-in-one image using that runtime image. The actor-based Python
runtime is not a build input.

Initialize sandboxd with `git submodule update --init src/sandboxd` before
building. The all-in-one image builds the sandboxd binaries, including
`firecracker-agent`; installs checksum-pinned static distill-fs, gVisor, and Kata
artifacts; installs the Firecracker VMM and guest kernel; and constructs the
matching guest-agent initrd. Runc remains build-time optional, and
`AKERNEL_ENABLE_FIRECRACKER=false` excludes the Firecracker payload.

AKernel builds virtiofsd 1.14.0 from the pinned source commit and release
Cargo.lock in `builder/node.Dockerfile`. Keep its shared-library dependencies
and licenses packaged with the Firecracker payload. Both standalone and Helm
disable virtio-fs by default and use EROFS roots; enabling directory-backed
roots requires an explicit compatible virtio-fs profile. Disabling the
Firecracker image payload also excludes virtiofsd.

The sandboxd submodule's runtime manifest is the source of truth for the gVisor, runc, and Firecracker releases used by both sandboxd E2E and AKernel packaging. Test an unreleased runtime by checking out the sandboxd commit that pins it rather than overriding manifest fields from the AKernel build. Keep sandboxd's pooled-TAP contract and the matching gVisor compatibility patches validated together when upgrading.

Native Linux/arm64 builds support the `execd` runtime profile with runsc and optional runc. Set `AKERNEL_TARGETARCH=arm64`, `AKERNEL_ENABLE_KATA=false`, and `AKERNEL_ENABLE_FIRECRACKER=false`; use `AKERNEL_ENABLE_RUNC=true` to include runc. ARM64 requires explicit ADX platform and Execd archive URL/checksum pairs matching its architecture, and rejects VM payloads and NVIDIA GPU requests. Builds require Docker BuildKit. The target precedence is explicit `AKERNEL_TARGETARCH`, then a supported single-platform `DOCKER_DEFAULT_PLATFORM`, then the stable `amd64` default; never infer the builder's architecture from the Docker client's host. Explicit runtime flags override the selected deployment profile, which overrides defaults. Validate the merged flags, release pins, and URL/checksum override pairs before either Docker build.

The build selects architecture-matched gVisor and runc pins from the sandboxd manifest and distill-fs pins from `builder/distill-fs-versions.env`, explicitly sets both Docker build platforms, and passes the target architecture to sandboxd compilation. Check downloaded executable ELF headers against the target architecture with `builder/scripts/verify-elf-arch.sh`; checksum verification, manifest claims, or successful execution under emulation do not establish architecture. Keep the ADX platform and Execd archives from the same release, and the collector archive checksums synchronized with `OTELCOL_CONTRIB_VERSION`. Collector URL overrides require a matching checksum. `make build-helper-test` covers build arguments and ELF validation without Docker builds.

Keep official build-download sources as the default and mirror/proxy selection explicit. Preserve third-party repository URLs, release pins, signing keys, checksums, and TLS verification. Forward proxy credentials only through Docker's predefined build arguments by name, without exposing or persisting values. The [build download guide](builder/README.md) owns mirror scope, override precedence, proxy/network options, and release URL/checksum pairs; update it when changing those interfaces.

PVM is an experimental opt-in node profile. Standalone selects `AKERNEL_FIRECRACKER_BACKEND=pvm`; Helm selects `node.config.sandboxd.firecrackerBackend=pvm`. This selects `firecracker-pvm`, removes Kata from that profile, and requires the validated PVM guest bundle and matched OOT host `kvm.ko`/`kvm-pvm.ko` modules. `deploy/pvm/host-profile.json` is the single versioned host qualification input: source/baseline, target/kernel/compiler identity, build-receipt digest, module pair, provisioning limits and immutable fixtures. `deploy/pvm/oot-host.env` derives compatibility exports from that JSON without another pin copy. Prepare that profile into a fresh export, verify the exact host inputs and build against the existing distribution kernel; do not build or replace the host kernel. The host-only OOT source and PVM guest source are distinct. VMX/SVM is unnecessary on the host/L1, but the other CPU features, supported boot settings and module-loading policy still apply. Sandboxd verifies the actual backend before advertising it. Keep default KVM behavior unchanged; see `deploy/pvm-runtime.md` for module preparation, placement, restore and rollout boundaries.

PVM integration targets `inclusionAI/AKernel:develop` after the sandboxd runtime change targeting `inclusionAI/sandboxd:main`. Fork feature branches provide isolated qualification before upstream review. Keep the sandboxd submodule URL and gitlink consistent. The OOT host profile includes the upstream OSPKE guard fix and NX recovery-worker retry backport; guest TSC changes and the optional DEBUGCTL optimization remain separate. The opt-in PVM bundle is independently pinned by sandboxd in `third_party/firecracker-pvm.json` and consumed without changing ordinary KVM releases. That descriptor selects either a locked Actions candidate or its unchanged promoted public asset; release downloads require neither a GitHub token nor an unexpired Actions artifact. The disposable PVM CI checkout must also clean ignored build files inside the separately initialized sandboxd submodule; cleaning only the parent repository leaves stale staged bundles behind. Keep checksum failures fatal. Strict TSC improves POSIX clock/timer recovery, but raw-TSC continuity and heterogeneous migration remain unqualified. The independent PVM workflow requires a maintainer dispatch, immutable allowed-ref resolution, the committed expected profile and a bounded disposable L1; ordinary hardware-KVM CI does not qualify PVM. See `deploy/pvm-runtime.md` before changing guest pins or CI.

Install the complete checksum-pinned gVisor release archive with sandboxd's
`third_party/install-gvisor.sh`. Preserve runsc, the containerd shim, and all
five adjacent `gvisor-bin/` helpers in the final image. The manifest SHA-512
covers the archive, not a bare runsc binary. Keep CI's final-image payload
checks synchronized; do not mix helper versions or fetch them at runtime.

The sandboxd submodule supplies the source compiled by `make build`.
Helm and standalone enable `runsc.background_restore` and `checkpoint_sync.sync_output`;
runtime files and private writable layers remain on node-local storage.
AKernel's `builder/distill-fs-versions.env` fixes the distill-fs release URL
and SHA-256. `make build` compiles the local sandboxd worktree and consumes the
static distill-fs release through `builder/scripts/install-distill-fs.sh`; editing
`src/distill-fs` no longer affects the image. The installer verifies the archive,
provenance, version, binary hash, and static ELF contract, and packages its
licenses and manifest under `/usr/local/share/distill-fs`.

Publish and verify a distill-fs release before updating the AKernel manifest
pin. This dependency does not require a sandboxd source or gitlink change.
Never use a guessed checksum or silently fall back to a source build.
Missing or invalid release pins prevent builds. `make versions` reports the
release tag and archive digest without requiring the distill-fs submodule.

The optional node-wide ChunkDB capacity is configured with standalone
`AKERNEL_CHUNK_DB_SIZE`, Helm `node.config.sandboxd.chunkDbSize` (under
`core` in the umbrella chart), or Terraform `chunk_db_size`. Guided profiles
accept `make config CHUNK_DB_SIZE=64GiB`. Empty values preserve the Linux
100 GiB default. Sandboxd passes the same capacity to every distill-fs user;
existing-cache resizing is unsupported. Preserve the
`# AKERNEL_CHUNK_DB_SIZE` marker in custom deployment templates. See
`deploy/README.md#shared-chunkdb-capacity` for cache replacement semantics.

Each component embeds its own semantic version: sandboxd uses
`version/VERSION`, while distill-fs uses its release package version in
`Cargo.toml`. AKernel does not inject parent-repository version metadata into
component compilation.

Inspect the selected local versions without building an image:

```bash
make versions
```

The final image uses standard OCI labels for the AKernel version and revision.
Component semantic versions are reported by their binaries, and their exact
source revisions are traceable through the sandboxd gitlink and the pinned
distill-fs release's packaged manifest.

## Deploy

Use [`deploy/README.md`](./deploy/README.md) as the deployment entry point. AKernel supports standalone, existing Kubernetes clusters via Helm, and Terraform-based cloud provisioning.

The [standalone guide](./deploy/standalone/README.md) owns source startup, supported platforms, runtime selection, and SDK setup. Preserve the source launcher's native-daemon architecture selection, clean sandboxd gitlink check, occupied-name preflight, profile locking, unique source image, and node/gateway health checks. Status and stop must verify recorded container identities, images, and data mounts; stop also requires an empty sandbox inventory. Source management is independent of cloud profiles and must preserve unrelated containers.

The all-in-one image and node launchers declare lowercase `container=oci`
for PID 1 systemd. Preserve this in the final image, Helm node environment,
and standalone launcher: without container detection, privileged systemd
shutdown can remount shared host filesystems read-only. See
[`deploy/README.md#systemd-container-identity`](./deploy/README.md#systemd-container-identity)
for deployment implications.

ADX's systemd launcher imports the selected ADX configuration, Redis address,
and node identity from the container PID 1 environment. Explicit systemd service
environment overrides take precedence. Preserve this handoff when changing
node deployment: otherwise the service can fall back to standalone configuration
even though the Pod declares a node configuration.

The managed Kubernetes Redis uses its own Helm-generated authentication Secret,
preserved by live lookup during upgrades. Its NetworkPolicy is additional
isolation and requires CNI enforcement. Coordinator and Ingress/API Server run in separate
Deployments with independent `adxctl` supervisors and health probes. Their
generated state is container-local, while Redis persists authoritative state.
Adxlet uses its own state subtree under `/home/akernel/adx/run/node`;
never start Coordinator, Ingress/API Server, or Redis in a node Pod.

Preserve explicit ADX/Collector preStop hooks and keep Pod termination grace
longer than service stop budgets. Qualify shutdown changes with the
[node lifecycle regression](deploy/tests/kubernetes/node_lifecycle.py) on an
idle dedicated cluster; see [deployment behavior](deploy/README.md#systemd-container-identity).

Helm `adx.checkpointStorage.kind=distributed_fs` consumes an operator-mounted
JuiceFS/AFS filesystem or an existing RWX claim. The node and Coordinator must
see the same namespace, alias and checkpoint prefix; only these two roles mount
it. Coordinator reclaims shared checkpoints. An init container verifies the
mount and runtime path isolation without rewriting sandboxd configuration.
Runtime state and private writable filestores stay on node-local disk, with the
same container path on all nodes so runsc rootfs annotations remain compatible
across nodes. Keep credentials and filesystem format/mount/GC in the deployment
layer. Drain before replacing a mount or changing storage identity.
The [shared storage guide](deploy/shared-storage/README.md)
owns PostgreSQL/Ceph/JuiceFS setup, values, acceptance and operational limits.
Both cloud Terraform modules expose `checkpoint_storage` and an optional
`juicefs` stack. Managed JuiceFS deploys PostgreSQL, PVC-backed Rook/Ceph RGW,
bucket provisioning, an idempotent format Job and host FUSE clients before ADX.
A bounded post-install/upgrade readiness Job checks current OnDelete clients
and mount coverage on every selected host. Managed client selectors also
constrain execution node and Coordinator placement; preserve existing node
affinity and the runtime mount guards. Shared aliases accept ASCII letters,
digits and hyphens, with `local` reserved, including when managed JuiceFS
implicitly selects distributed_fs.
`make config JUICEFS_ENABLED=true JUICEFS_STORAGE_CLASS=...` selects this stack;
external mounts and RWX claims use the checkpoint storage options instead.
Keep storage deployment disabled by default and preserve the matching
node/Coordinator alias, prefix and mount when changing these inputs.
Managed JuiceFS uses a 4096 MiB node-local hostPath block cache with
`cacheLargeWrite: true` and enforced `writeback: false`. The cache stays separate
from runtime and shared checkpoint paths. Helm, both cloud Terraform modules
and `make config` expose its size and host path. Managed Ceph enables RGW Beast
`tcp_nodelay=1`. Drain consumers before replacing OnDelete mount clients and
recreate the ADX consumers afterward; a chart upgrade alone does not remount.
Managed JuiceFS defaults to `trashDays: 0`; Environment and Snapshot references
control checkpoint retention. Update existing volume settings explicitly because
the idempotent format Job preserves them.

Aliyun's aggregate Pod PID budget is configurable independently of the
per-sandbox limit; see `deploy/terraform/aliyun/README.md#pod-pid-budget`.
Aliyun and Huawei default AKernel node pools also configure host PID/thread
ceilings and container scope TasksMax. Keep this separate from extra and
Dragonfly pools, and verify running Pod ancestors after existing-node migration.

For guided cloud deployment:

```bash
make config VENDOR=aliyun
make plan
# After reviewing the plan and obtaining explicit approval:
make deploy
make print-env
```

Kata and Firecracker are present in the default AKernel runtime configuration
but are optional node capabilities. Both require `/dev/kvm` to be usable from
the node container. Firecracker additionally validates its VMM, guest kernel,
initrd, and `mkfs.ext4`. A node without KVM remains ready for runsc workloads
and advertises neither VM runtime; a Kata or Firecracker request fails
scheduling with a no-resource error when no eligible node exists. Do not treat
a configured runtime as an advertised runtime.

Firecracker supports commands, files, PTYs, network policies, published ports,
reverse tunnels, EROFS roots and mounts, OCI/Nydus directory roots and explicitly
read-only or writable host directory mounts through virtio-fs, explicit
`storage_mb` quotas, and recovery across sandboxd restarts. Writable external
directories remain caller-owned and require their original backing files for
local C/R. Qualify the actual deployed image and virtio-fs stack. OCI image
mounts, NVIDIA GPUs, and nested KVM remain unsupported.

Do not add Firecracker-specific directory conversion, image caching, or
artifact reference counting to sandboxd or its image manager. Consume OCI/Nydus
directories directly from the image manager through read-only virtio-fs.
Explicit local/S3 imagefile roots and mounts must already be EROFS. The bundled
default runtime root also remains EROFS; sandbox writes use a private ext4 disk.

The bundled Firecracker writable disk policy is buffered `Async` with `Writeback`.
Validate io_uring on the target host; explicitly select `Sync` where it is
unavailable. Direct I/O profiles additionally require `STATX_DIOALIGN` support;
there is no silent policy fallback.
Keep standalone and Helm defaults synchronized. Drain before upgrading the
runtime stack: checkpoint compatibility includes VMM, kernel, initrd, and
virtiofsd digests, and restores retain the saved writable I/O engine.

Runc is excluded from default image builds and from the default advertised
runtime set. Guided cloud profiles use `make config ENABLE_RUNC=true`; this
records both the image build flag and Terraform runtime registration. For
direct configuration, build an image with `AKERNEL_ENABLE_RUNC=true`, then use
`AKERNEL_ENABLE_RUNC=true` for standalone,
`node.config.sandboxd.enableRunc=true` for Helm, or `enable_runc=true` for
Terraform. Runc uses the host kernel and therefore has a different isolation
boundary from runsc. It does not support experimental GPU or explicit
`storage_mb` requests. Its optional `enableKVM` extra configuration requires a
usable `/dev/kvm` device.

Standalone configures resolver sources by DNS mode. Enabling runc copies an approved host resolver into the data mount and sets `plugin.runtime.direct_resolv_conf_path`, while `plugin.runtime.resolv_conf_path` remains the node resolver used by managed DNS. `AKERNEL_RUNC_RESOLV_CONF` selects the host source; systemd-resolved hosts require it explicitly because a flat file cannot preserve split DNS. Preserve the direct-resolver marker under `[plugin.runtime]` in custom templates, and drain sandboxes before refreshing the resolver snapshot or replacing the node. See [`deploy/standalone/README.md#dns-resolver-sources`](deploy/standalone/README.md#dns-resolver-sources) for mode selection and reachability requirements.

Standalone must check platform and image compatibility before modifying credentials or resolver configuration, and explicitly use the native ARM64 platform with OrbStack. Keep source and existing-image profile defaults documented in the standalone guide. Container proxy credentials belong in the protected profile env-file. Drain sandboxes before stopping the node; stop or removal failures must return nonzero, and stopped profiles retain data and identity files.

The bundled sandboxd configuration enables per-sandbox network ACLs. Pooled TAP
networking requires the host `tun` module and a usable `/dev/net/tun`. The
default iptables backend additionally requires `iptables`, `ip6tables`,
`ipset`, IPv4/IPv6 filter tables, `br_netfilter`, `xt_physdev`, conntrack,
conntrack-netlink, connmark/CONNMARK, timeout-capable `hash:ip` sets, and
IPv4/IPv6 bridge netfilter. The optional bpfnat backend
instead requires Linux 5.17 or newer for `bpf_loop`, eBPF `SCHED_CLS`, TC
`clsact`, writable bpffs, and permission to load BPF programs and manage TC
filters. Both require free TCP/UDP port 53 on the sandbox bridge. Drain
existing sandboxes before enabling ACLs or upgrading a
node from a pre-ACL configuration; sandboxd refuses to initialize ACLs when
old sandbox records remain. A sandbox without a network policy stays
unrestricted. Schema v2 supports independent ingress and egress defaults,
allow and deny rules over IPv4 CIDRs, domains, protocols, and ports, plus an
independent DNS policy. See `deploy/README.md` for deployment requirements and
`sdk/python/README.md` for API limits.

Dragonfly distribution is optional and disabled by default. Enable it during
profile generation with `make config INSTALL_DRAGONFLY=true`. This installs the
pinned public chart and, by default, creates three seed nodes and one server
node in dedicated pools. Review the generated Terraform plan and expected cost
before applying it.

The deployment helper creates an HTTPS and API key Secret before Helm install.
It retains an existing Secret so certificates and the bootstrap administrator
API key stay stable across updates.

If `.akernel/default/` already exists, `make config` asks before overwriting
`config.env` and `terraform.tfvars`.

For agent/non-interactive deployment setup, do not rely on prompts. Pass config
values explicitly and use a named environment to avoid overwriting a user's
default profile. Populate the region-specific values after checking the target
cloud account; the number of Alibaba Cloud zones and vSwitch CIDRs must match.

```bash
ENV_NAME=agent-e2e
make config \
  ENV="${ENV_NAME}" \
  VENDOR=aliyun \
  NON_INTERACTIVE=1 \
  REGION="${REGION}" \
  ZONE_IDS="${ZONE_IDS}" \
  VSWITCH_CIDRS="${VSWITCH_CIDRS}" \
  IMAGE_REPOSITORY="${IMAGE_REPOSITORY}" \
  IMAGE_TAG="${IMAGE_TAG}"

make plan ENV="${ENV_NAME}"
```

Inspect an existing profile before reusing its name. Add `FORCE=1` only when
the user has explicitly approved overwriting its generated configuration.

## Agent DX API Key

Read the deployed bootstrap administrator API key with:

```bash
make token
make print-env
```

`make token` reads `admin-key` from the deployed `akernel-adx-tls` Secret.
`make print-env` writes mode-0600 `token` and `sdk.env` files under the selected
deployment profile. Treat their output as a secret: do not include it in logs,
commits, issue reports, or chat unless the user explicitly requests credential
handoff.

Standalone startup writes its selected SDK addresses to mode-0600
`deploy/standalone/data/sdk.env`; tests and callers can source this file and
read `data/token` separately. Keep the selected hostname and both ports aligned.

## SDK And CLI

Minimal sandbox usage:

```python
from akernel_sdk import Sandbox

with Sandbox(cpu=2000, memory=4096) as sb:
    result = sb.commands.run("echo hello")
    print(result.stdout)
```

`storage_mb` sets the storage scheduling quota, while `storage_limit_mb` sets
the actual writable-root hard limit. A zero limit follows the scheduling
quota or cluster default. A positive limit without an explicit scheduling
quota also reserves that amount for scheduling. Keep this distinction in SDK
documentation and examples; see `sdk/python/README.md` for runtime support.

`Mount(host_path=...)` binds an existing path visible to the selected node's
runtime. HostPath mounts default to read-only; writable binds require
`read_only=False`. Image/S3 mounts remain read-only. Keep the pinned ADX SDK's
HostPath serialization adapter under `_backends`, and preserve server-side
permission errors. Restrict this interface to trusted management callers and
operator-controlled source paths; client validation is not authorization to
access the node's filesystem.
The SDK does not create host directories, mount node storage, or replicate
node-local data. See the SDK guide and `sdk/python/examples/hostpath_mounts.py`.
For operator-owned node directories, use the supported Helm overlay and
ownership, retention, and validation contract in
[`deploy/docs/node-local-directories.md`](./deploy/docs/node-local-directories.md).
Keep helper lifecycle and core/umbrella rendering tests synchronized.

Same-node failover and explicit rollback retain the logical sandbox identity:

```python
with Sandbox(failover=True) as sb:
    if not sb.reload():
        print("no local checkpoint is available")
```

The current functional integration deliberately leaves anonymous local
checkpoint creation inside the workload through EXECD's internal Unix socket.
The node sets `ADX_EXECD_CONTROL_SOCKET_PATH=/run/akernel`, making the socket
available at `/run/akernel/execd.sock`. Do not present that socket protocol as a
stable public SDK interface. Reusable snapshots are a separate public contract:
`Sandbox.create_snapshot()`, `get_snapshot()`, `list_snapshots()`,
`delete_snapshot()` and `Sandbox.create(snapshot_id)` operate on independent
snapshot IDs. Preserve `Sandbox.from_snapshot()` as the compatible restore entry
point with identical options, ownership and error semantics. Without a snapshot,
`create(image=...)` and `create()` use fresh creation; existing constructors stay
supported. Foreign template aliases are not resolved. Keep backend-native types and
lifecycle/connection work in `_backends`; restore must inherit captured resources
and must not rerun fresh-boot setup.
`sandbox.fork(count=N)` captures once and returns an ordered Sandbox/Exception
per child, preserving partial successes. The pinned ADX SDK/API has no native
batch fork; the adapter composes reusable snapshot/create/delete operations.
Do not describe it as a single server-side request. Fork children are owned
handles, even for a detached source. SNAPSHOT_IN_USE/not_started keeps the
shared temporary snapshot until the last owned child is deleted; expose its ID
through fork_snapshot_id for explicit reconciliation after close/by-ID deletion.
Other cleanup errors remain visible and retryable through ForkCleanupError.
Never treat an arbitrary conflict as accepted or deferred deletion. Idle timeout
is not an absolute sandbox lifetime; snapshot/restore timeouts bound operations.
Restore budgets below schedule_timeout + 60 fail before creating an environment
or taking a fork snapshot. They are logical frontend budgets, not wall-clock
deadlines for client bootstrap/cleanup. Preserve known IDs and cleanup_errors
through constructor, route and facade failures, including BaseException during
tunnel bootstrap. The pinned ADX restore does not inherit network policy:
from_snapshot callers must supply restrictions explicitly. Fork sends the source
client's acknowledged policy during creation. Trust a creation policy only for
a confirmed newlyCreated=true response; named reuse, missing ownership and
connect require a successful explicit policy reapply before fork. Never infer
policy acknowledgment from constructor success or absence of a requested policy.
Fork cancellation cleanup includes final snapshot deletion. Preserve the original
exception and retain all unresolved IDs, cleanup-only public Sandbox handles,
snapshot ID and cleanup/creation errors, including secondary interruptions.
Collect creation errors from all backend results plus prior public binding
errors. Automatic rollback deletes only ownership-confirmed new children;
local closure without a remote deletion acknowledgement must leave a pending
cleanup handle for an explicit kill, including on older create responses.
Bootstrap rollback must retain secondary BaseExceptions on the original
failure, including before a session is returned; merge those known pending
children with the fork loop's results without automatically retrying them.
Run `sdk/python/tests/integration/test_fork.py` with explicit integration opt-in
and a complete Python root image to validate memory/FD and private-write
isolation. This is distinct from a read-only CLI mount image.
The companion test_fork_network.py verifies per-child tunnels/published ports,
creation-time and updated policy inheritance, and isolated child policy clear
against a reachable outbound baseline. Keep host services and all owned children
cleaned on both failure and success.

Select Kata explicitly only when the cluster has an eligible node:

```python
with Sandbox(runtime="kata", cpu=2000, memory=4096) as sb:
    print(sb.commands.run("uname -s").stdout)
```

Select an explicitly enabled runc runtime and pass runtime-owned options:

```python
with Sandbox(
    runtime="runc",
    extra_config={"enableKVM": True},
    cpu=2000,
    memory=4096,
) as sb:
    print(sb.commands.run("test -c /dev/kvm").exit_code)
```

Request an experimental gVisor GPU:

```python
with Sandbox(xpu="gpu:l20:1") as sb:
    print(sb.commands.run("nvidia-smi -L").stdout)
```

Configure a creation-time network policy:

```python
from akernel_sdk import NetworkPolicy, Sandbox

with Sandbox(network_policy=NetworkPolicy.block()) as sb:
    print(sb.commands.run("echo control-plane-access").stdout)
```

Configure a generic egress allowlist:

```python
from akernel_sdk import NetworkPolicy, NetworkRule

policy = NetworkPolicy.allowlist(
    [NetworkRule(domain="*.example.com", protocol="tcp", port_range=443)]
)
with Sandbox(network_policy=policy) as sb:
    print(sb.commands.run("curl https://api.example.com").stdout)
```

Required environment:

```bash
export AKERNEL_SERVER_ADDRESS="<server_address>"
export AKERNEL_TOKEN="<your_token>"
```

ADX always keeps two public listeners: HTTPS/WSS control traffic on 443 and
plain HTTP/WS instance data on 80. API Server and Ingress may share a process, but
their public ports remain distinct. Standalone publishes both embedded Ingress
listeners directly from the `akernel-node` container:

```bash
export AKERNEL_SERVER_ADDRESS=127.0.0.1
```

No separate `AKERNEL_GATEWAY_ADDRESS` is required for the default standalone layout. When a custom topology sets it, the override applies only to public sandbox port URLs and reverse tunnels; exec and file transfer continue to use `AKERNEL_SERVER_ADDRESS`. For source standalone, source the generated `sdk-env.sh` from the selected data directory. Direct `start.sh` uses `akerneldev/all-in-one:latest` by default; pass `IMAGE` for a locally built or differently tagged image.

Standalone GPU testing additionally requires NVIDIA Container Toolkit on the
host and `AKERNEL_ENABLE_GPU=true`. sandboxd uses the read-only cgroup
node-resource provider in standalone mode; Kubernetes deployments retain the
Kubernetes provider. Standalone explicitly enables local DNAT because Ingress
shares the node network namespace.

Standalone uses iptables NAT by default. Set `AKERNEL_NAT_BACKEND=bpfnat` to
use the experimental embedded TC eBPF backend. AKernel prepares the required
network-namespace sysctls, but bpfnat does not change firewall policy; custom
host-network deployments with `FORWARD=DROP` must allow traffic to and from
the sandbox bridge. YuanRong receives `INSTANCE_IP` in Kubernetes or the
default-route interface address in standalone mode; `AKERNEL_NODE_IP` is the
explicit override for multi-homed environments.

The standalone sandboxd filestore is a loop-mounted ext4 image under the selected bind-mounted data directory. Explicit `storage_mb` quotas for runsc and Firecracker use this local-disk filestore. Without an explicit quota, runsc retains its configured memory-backed overlay while Firecracker creates its configured sparse ext4 default.

Terraform-managed Alibaba Cloud node pools instead attach a dedicated 300 GiB
ESSD by default, have ACK format it as XFS, and mount it at `/home/akernel`.
The Aliyun sandboxd configuration leaves `filestore_dir_size` unset and uses
`/home/akernel/filestore` directly, so writable layers and
`/home/akernel/checkpoints` share the native reflink-capable filesystem. Do
not set a bounded filestore size for this profile because that reintroduces a
loop-backed filesystem and disables the high-performance Firecracker C/R
layout.

The default bundled node enables ADX local recovery points and
stores checkpoint state under the persistent `/home/akernel/adx/checkpoints`
mount. EXECD receives
`ADX_EXECD_CONTROL_SOCKET_PATH=/run/akernel` so sandbox workloads can trigger
their local checkpoint handoff through `/run/akernel/execd.sock`. Recovery points
follow the source sandbox lifecycle and remain the basis for failover/reload.
The public SDK also exposes independent reusable snapshots and restoration to a
new sandbox identity. Their storage locality and runtime support are controlled
by the backend; there is no public snapshot TTL or incremental-mode switch.
Keep local-only snapshot mode and the persistent checkpoint directory
configured together when changing node startup arguments. The optional Helm
shared filesystem profile replaces that node checkpoint directory with
`checkpoint_storage` and configures the matching Coordinator reclaimer.

Keep detailed SDK reference material with the SDK. The root README should
contain only the project-level entry points and representative examples:

- SDK guide: [`sdk/python/README.md`](./sdk/python/README.md)
- Examples: [`sdk/python/examples/`](./sdk/python/examples/)
- CLI reference: [`sdk/python/README.md#cli`](./sdk/python/README.md#cli)

Install the SDK development tools and run the complete local quality gate with:

```bash
python3 -m pip install -e './sdk/python[dev]'
make sdk-check
```

The Python SDK installs its execution backend automatically. Preserve the
public `AKERNEL_SERVER_ADDRESS=host[:port]` and `AKERNEL_TOKEN` contract.
Retired backend selector names map to ADX without installing old dependencies.
Native conversions remain under `akernel_sdk._backends`.
Lifecycle wrappers use explicit by-ID lookup/deletion, lazy filtered pagination
and an isolated Client; preserve legacy constructor, id, detached kill and
unchecked CommandResult behavior. close disconnects locally; pause/connect keep
the logical ID, while snapshot restore/fork create new IDs. Custom metadata,
running lifetime, live output callbacks and per-command user selection are
separate backend extensions, outside this SDK surface. Attachment rollback
must never delete an existing sandbox. Keep command reconnection free of replay.
Capture a command's stable ID atomically with start/connect and bind wait, stdin
and kill to that identity, even if another retained record reuses its PID. Map
the pinned native PermissionDenied exception without inventing a missing status.

`BackendOperationError` preserves optional structured service fields and the
original cause; missing context remains `None`. Keep retry/outcome semantics
backend-owned and document public error fields in `sdk/python/README.md`.

Keep sandbox cleanup explicit through context managers or `kill()`, with
observable, retryable deletion failures and workload exceptions preserved on
context exit. See [SDK lifecycle guidance](sdk/python/README.md#resources-and-lifecycle).

Dockerfile direct launch is a supported AKernel SDK capability through
`DockerContext` and
`Sandbox(dockerfile=DockerfileLaunch(context=..., auto_start_cmd=..., run_timeout=...))`.
The capability will remain available. Its documented strict subset evolves
incrementally with production experience, while unsupported inputs continue to
fail closed. The specific API surface may evolve; material changes require
documentation and migration guidance. Read
[`sdk/python/docs/launch-from-dockerfile.md`](./sdk/python/docs/launch-from-dockerfile.md)
before changing this path. `FROM` supplies only the root filesystem; inherited
OCI configuration is not applied. Runtime availability and compatibility remain
backend-owned. `DockerContext.walk()` exposes public structured file and
directory entries, including modes and empty directories; context transfer must
remain backend-neutral, reject unsafe manifests and unsupported syntax
fail-closed, and preserve documented Dockerfile-specific ignore-file
precedence. Dockerfiles and active or root ignore files remain ordinary context
entries unless the active matcher excludes them. Keep the public types, unit
tests, SDK README, Dockerfile launch guide, and
`examples/dockerfile_launch.py` in sync.

When changing a public SDK method, update its type annotations and docstring,
add or update unit coverage, and keep the SDK README and maintained examples in
sync. Benchmark programs under `sdk/python/benchmarks/` are manual tools and
are not part of the default test suite.

## Release

The CI workflow builds the public Linux/amd64 all-in-one image with only
gVisor runsc and the `execd` runtime profile. It explicitly sets
`AKERNEL_ENABLE_KATA=false`, `AKERNEL_ENABLE_FIRECRACKER=false`, and
`AKERNEL_ENABLE_RUNC=false`, excluding VM payloads and virtiofsd. Source-build
defaults still include Kata and Firecracker for operators who need them.
After SDK checks, distribution validation, deployment syntax checks, and
standalone runsc E2E pass, pushes to `main` in `inclusionAI/AKernel` publish
that tested image only as `akerneldev/all-in-one:latest`. PRs and forks never
publish. Check that the commit is still the current `main` head before any
push; superseded commits and reruns of older commits skip publication
entirely. Do not publish per-commit SHA tags or other historical image tags.
The job checks the image contains runsc and excludes Kata, Firecracker,
virtiofsd, and runc before starting standalone E2E.

CI runs examples with unbuffered Python output. Ordinary examples have a
120-second limit; `dockerfile_launch.py` gets 600 seconds for its nine
sections. Its core startup script uses the Ubuntu base image's shell and
does not install packages. Keep its RUN, context-transfer, and startup checks
independent of external package mirrors.

Configure the repository Actions variable `DOCKERHUB_USERNAME` and secret
`DOCKERHUB_TOKEN` with Docker Hub credentials that can push to
`akerneldev/all-in-one`. Keep credentials out of source and logs. Main CI runs
are not canceled by subsequent pushes. After E2E and teardown succeed on
upstream main, the standalone job exports the tested image as a compressed
Actions artifact retained for one day. The separate `publish-dockerhub` job
downloads that exact artifact by ID, loads it, and verifies its image ID
against the E2E job output before publishing. PRs and forks skip image
transfer and publication. Do not rebuild the image in the publishing job.

The publishing job serializes publication using `queue: max` (up to 100
pending jobs), so a slow or rerun job cannot overwrite a newer published
`latest`. Keep its existing lock key to coordinate with older workflow runs.
Check main before downloading and again immediately before pushing while
holding this lock. Do not remove the main-head check or the publication lock.
A failed check or push leaves the run failed. Rerun the failed publishing job
while the artifact is available; after it expires, rerun all jobs to rebuild
and retest the image.

Python SDK releases use stable `vX.Y.Z` tags or release-candidate
`vX.Y.ZrcN` tags. The tag version must match the version in
`sdk/python/pyproject.toml`. RC commits must be part of `develop`, with the
GitHub Release marked as a prerelease. Stable commits must be part of `main`,
with the prerelease flag disabled. Keep release tags and published distributions
immutable; candidate fixes use a new RC number.
Publishing a GitHub Release runs
`.github/workflows/release-python.yml`, which checks the SDK, builds and tests
the wheel and source distribution, and publishes them through the PyPI trusted
publisher configured for the `pypi` GitHub environment. Do not add a PyPI
password or API token to the repository.

Source installation, PR CI, and Python publication install the dependencies
declared in `sdk/python/pyproject.toml` from the configured package index.
Keep the ADX SDK version pin in that metadata as the single dependency entry
point for all three paths.

PR CI and publishing share `.github/actions/python-distributions`, which
builds the wheel and source distribution and installs each in a separate clean
environment with its declared dependencies. Keep dependency validation,
isolated imports, version checks, and CLI smoke tests in this shared action.

## Test

Run SDK unit tests with:

```bash
make sdk-test
```

Run syntax checks for the tracked Bash deployment scripts, Terraform shell
templates, and Python deployment helpers with:

```bash
make deploy-script-check
```

S3 checkpoint capture/restore uses the pinned ADX backend through
`adx.checkpointStorage.kind=s3`, with identity and cache settings under `s3`.
Only nodes and Coordinator receive the existing credential Secret. Native runsc
reflink uses `node.config.sandboxd.nativeFilestore` and `runscCheckpointMode`;
the preflight probes FICLONE between local checkpoint inputs and filestore.
It does not require JuiceFS. Direct distributed_fs remains supported separately.
See `deploy/shared-storage/production.md` for transfer costs and migration gates.
Validate HA layouts with `python deploy/shared-storage/tests/test_ha_layout.py -v`;
rendering does not replace native cross-node or failover tests. Existing ACK
clusters can add dedicated storage pools through `deploy/terraform/aliyun-node-pools`.

For Kubernetes SDK ingress changes, validate the rendered ADX public Service
and cloud-provider templates:

```bash
python deploy/akernel/charts/core/tests/test_adx_chart.py -v
python deploy/terraform/test_adx_contract.py -v
```

ADX directly terminates HTTPS control/API/PTY on its 8443 listener and exposes
HTTP sandbox traffic on 8080. Public Services map these to configurable external
ports (443/80 by default). Preserve both ports and the existing SDK variables.
Validate real TLS authentication, PTY and HTTP port access with the
`direct-ingress` standalone E2E group; PR CI discovers this group alongside the
other standalone tests. Keep ADX public certificates and remove redundant
external-proxy certificates and cloud-profile settings.

For standalone lifecycle or launch changes, also run the helper tests without starting containers:

```bash
make deploy-standalone-test
```

Keep opt-in native Restore fault tests under `deploy/tests/native_restore/`
aligned with the pinned ADX source and Sandboxd/runtime bundle; run only against
a dedicated empty backend with checkpoint paths shared by the test process.
The directory's README documents remote native probes and the SDK lifecycle
driver, including TLS, actual worker registration, outage ownership and cleanup.

The opt-in [TCP/conntrack checkpoint diagnostic](deploy/tests/checkpoint_conntrack/README.md)
uses a dedicated empty local standalone and an explicitly installed SDK. Keep
checkpoint completion, file rollback and restored network access as separate
checks; retain setup failures and unknown command outcomes as incomplete coverage.

Run the basic e2e example against a deployed cluster with:

```bash
make e2e
```

The SDK integration and pressure tests are runtime-selectable. Kata and
Firecracker tests require a KVM-capable standalone or cluster node:

```bash
AKERNEL_RUN_INTEGRATION=1 \
AKERNEL_TEST_RUNTIME=kata \
python sdk/python/tests/integration/test_sandbox.py -v

python sdk/python/benchmarks/sandbox_pressure.py --runtime kata

AKERNEL_RUN_INTEGRATION=1 \
AKERNEL_TEST_RUNTIME=firecracker \
python sdk/python/tests/integration/test_sandbox.py -v

python sdk/python/benchmarks/sandbox_pressure.py --runtime firecracker
python sdk/python/benchmarks/sandbox_pressure.py \
  --runtime firecracker --storage-mb 256
python sdk/python/benchmarks/sandbox_pressure.py \
  --xpu gpu:a10:1 --storage-mb 256 --processes 1 --threads 1
```

Set `AKERNEL_TEST_IMAGE=ubuntu:24.04` with `AKERNEL_TEST_RUNTIME=firecracker`
after explicitly enabling a compatible virtio-fs profile on the test node
to run integration and reload coverage against an OCI/Nydus image root. This
also verifies that two sandboxes using the same image have private writes.
Test both an ordinary OCI image and a Nydus image resolved through the deployed
image manager. The pinned distill-fs supports RAFS v5; use
`nydusify convert --fs-version 5` when preparing Nydus test images. Select
`AKERNEL_TEST_RUNTIME=firecracker-pvm` for a PVM-enabled node. For isolated
network verification, set `AKERNEL_TEST_NETWORK_URL` to a reachable private
HTTP endpoint and use a test image with curl and CA certificates preinstalled.
The image integration checks include network policy replacement while retaining
SDK control-plane access.

## Maintenance Rules

- Keep root README and repo-level agent guidance focused on current
  `akernel-sdk` sandbox workflows.
- Prefer linking to `sdk/python/README.md` for detailed SDK usage instead of
  copying long examples into other docs.
- Do not reintroduce obsolete top-level examples or tests. Maintained SDK
  examples and tests live under `sdk/python/`.
- When code changes alter source layout, public APIs, build or deployment
  commands, supported platforms, or operational assumptions, update this file
  in the same change so future agents receive current guidance.
- Use Conventional Commits with a concise title and a prose body explaining
  what changed and why; do not create title-only commits. Keep a blank line
  between title and body, wrap the body for terminal readability, and sign
  commits with `git commit -s` to include the Developer Certificate of Origin
  sign-off.
- Keep unrelated dirty files out of commits, especially local deployment state,
  generated binaries, Terraform state, kubeconfigs, tokens, and private
  registry configuration.

## ADX observability

The monitor chart's opt-in `prometheusServer.config.sharedStorage` discovers
Ceph, optional CloudNativePG and Redis/Sentinel metrics and loads storage alert
rules. Disable postgresEnabled when no PostgreSQL consumers run. Validate the
rendered configuration with promtool, then verify live targets and samples.
Backup and isolated recovery remain independent acceptance requirements;
never enable irreversible cluster identity features implicitly.

Collector configs live in `deploy/akernel/charts/core/files/otel-collector/`;
standalone adds scrape targets through a small overlay. Preserve dashboard UIDs,
optional native detail sampling and runtime-control events. Verify real SDK
telemetry after collection changes. The [standalone guide](deploy/standalone/README.md)
owns tracing and AFS options; test actual mount validation and checkpoint reload.

Environment details are opt-in scrape jobs on the existing Prometheus instance,
with the same datasource and retention policy as summary metrics. Keep default
and detail-enabled provisioning compatible with private images and node placement.

Keep Grafana's datasource interval aligned with business scrapes (15s), with
a 30s override for Collector queries. Use `$__rate_interval` for counter and
histogram histories; validate bursts between coarse query steps with the
dashboard query contract.

Scope ADX dashboards, logs and TraceQL by `cluster` and `adx_env`; preserve both
labels in identity joins and per-deployment recording rules. Grafana date fields
consume milliseconds; source `_seconds` metrics retain seconds. Keep custom-All
values valid for TraceQL and Explore URLs, and run the browser signal contract
when changing their interpolation. Keep `core.monitoring.clusterName`, `AKERNEL_CLUSTER_NAME` and
the monitor cluster identity aligned across control, node and standalone
Collectors. API metrics stay on 19093; autoscaling stays on 19092. Unknown
outcomes, missing coverage and stale snapshots must remain visible.

Initialization rollback must use the server's `newlyCreated` ownership bit.
Failed reuse closes local clients and preserves the original workload; missing
ownership is conservative. Use `BackendSession.rollback_creation()` at every
initialization stage, including public facades and Dockerfile application.
Keep explicit delete/terminate semantics unchanged.
Tracing/PVM/checkpoint configuration must support explicit services and each
ADX profile, applying patches only to present roles. Resolve profile/role
selectors with ADX's single-pass environment/default/escape rules while
preserving source expressions for the authoritative loader. Severity queries consume
Collector-normalized log metadata and plain bodies.


## Managed Docker runtime

The source-preview SDK owns Compose image discovery/OCI metadata validation, ROFS mount plans and guest bridge/containerd/dockerd startup. Sources live in `builder/image-bridge`, `builder/dind`, and `sdk/python/akernel_sdk/_docker_runtime.py`. Callers supply original project files and keep service orchestration in their adapter. Snapshot restore must rebind mounts, runtime extra_config and local tunnels without rerunning startup. Reuse the existing sandboxd image provider; do not add a parallel layer cache or modify deployment/runtime pins for client refactoring. See the SDK and builder guides for managed-upper prerequisites and supported scope.

Compose project lifecycle and Docker service/process operations are SDK extensions in `_compose.py` and `_service_process.py`; the dependency-free `_service_process_guest.py` runs in the selected service PID namespace. Keep project descriptors JSON-compatible and restore attachment free of fresh-boot operations. Preserve explicit build/network-policy/up boundaries, unknown-outcome identities, source fencing, and container/process start-time checks before signals. Callers retain task/harness semantics. Run the real-process helper unit tests with owned-process cleanup and the complete SDK quality gate when changing these contracts. This feature does not add Docker semantics to Agent DX or change runtime pins.

Automatic Compose image preparation covers image-only services as well as Dockerfile bases. Verify pinned Docker references and committed ROFS snapshots instead of running native pulls, including during up. Keep digest-reference regression coverage and fail closed on missing registrations; never relax bridge extraction protection or populate the private upper with replacement lower layers.

Managed discovery requires helper schema 2 and probes capabilities before sending project input. Confine client Dockerfile/build-context and `.env` reads to the frozen project; never inherit unspecified host environment variables. External include/extends files remain unsupported. Validate manifest/config/chain relationships and allow legitimate same-chain roots with different target digests. Keep the empty automatic-plan marker for scratch-only builds. Unknown command exits and failed cancellation retain their identities and cleanup errors without replay.

Managed Compose defaults preserve native IPv4 bridge definitions. Keep forwarding/NAT and per-service tunnel exposure in the SDK/runtime, with daemon-only legacy iptables selection for Docker DNS. Restore attachment stays pure; explicit reconnect_network refreshes only owned chains for the new outer TAP before the harness resumes. Do not claim external TCP address migration. Keep the opt-in Compose network integration regression, including existing inner and published-port sockets, synchronized with these APIs. Docker 28 operator flags are prerequisites; client work must not change production or runtime bundle pins.
