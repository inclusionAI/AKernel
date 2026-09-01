# AGENTS.md

This is the tool-neutral project instruction entry point for coding agents.
Tool-specific compatibility files should point here rather than duplicate
project guidance.

## Project Overview

AKernel provides cluster-backed remote sandbox environments for agents and
developer workflows. The current public user-facing surface is the Python
`akernel-sdk`, including the `akernel_sdk.Sandbox` API and the `ak` CLI.
The default sandbox runtime is gVisor runsc. Runtime identifiers and generic
JSON-compatible runtime configuration are forwarded to the selected backend,
which owns availability and compatibility checks. The bundled deployment also
advertises Kata Containers and Firecracker on KVM-capable nodes. The native
Linux runc payload is build-time optional and must be explicitly included and
enabled by an operator.
Creation-time network policies and atomic runtime replacement support
unrestricted networking, blocking new flows except the YuanRong control and
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
- `src/yuanrong/` - pinned openYuanRong mirror checkout, including its
  recursive component submodules.
- `builder/` - Dockerfiles, service configs, runtime rootfs build, and image
  entrypoint scripts for the public all-in-one image.
- `deploy/` - Helm charts, standalone scripts, Terraform modules, and
  deployment helper scripts.
- `assets/` - static images used by the root README.

The open-source AKernel repository contains the SDK, deployment configuration,
build tooling, and examples. Node runtime components such as `sandboxd` and
`distill-fs` are maintained in their own upstream repositories and pinned as
Git submodules. The all-in-one build compiles those revisions and packages the
runtime payloads described in the Build section below.

## Common Commands

All commands should be run from the repository root.

```bash
make help
make check VENDOR=aliyun
make config VENDOR=aliyun
make build
make push
make plan
make deploy
make token TTL=24h
make print-env
make sdk-test
make deploy-script-check
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
- IAM signing seeds
- generated JWT tokens
- SDK environment exports

Never commit `.akernel/`, Terraform state, kubeconfigs, tokens, signing seeds,
cloud credentials, private registry URLs, or local debug artifacts.

## Build

AKernel uses Docker for building. The public distribution ships one all-in-one
image that can run as master, frontend, node, or standalone depending on the
deployment entrypoint and environment.

```bash
make build
make build RUNTIME_PROFILE=python
make build AKERNEL_ENABLE_RUNC=true
make build AKERNEL_ENABLE_FIRECRACKER=false
```

For a build that will be pushed and deployed, set `IMAGE_REPOSITORY` and
`IMAGE_TAG` when creating the deployment profile. A one-off `IMAGE_TAG`
override on `make build` does not update the profile consumed by `make push`.
The build creates only the selected image reference; it does not add a second
`akernel-all-in-one` alias. `make push` pushes that selected reference directly.

The build helper performs two Docker builds. `builder/runtime.Dockerfile`
creates `yr-runtime-rootfs.img`; the default `rrt` profile contains the
pinned openYuanRong RRT binary without Python. Set
`RUNTIME_PROFILE=python` to include the optional Python 3.10 through 3.14
runtimes and `openyuanrong_sdk`. `builder/node.Dockerfile` then compiles the
node components and produces the AKernel all-in-one image using the selected
runtime image and its matching service configuration.

The control-plane and RRT release version is independent of the optional
actor-based `openyuanrong_sdk` installed in the Python runtime profile. This
actor backend is deprecated and retained only for compatibility with existing
applications. Keep it on its explicitly pinned legacy version; do not advance
it with the default `openyuanrong-sandbox` backend or use it for new features.

Initialize submodules with `git submodule update --init --recursive` before
building. The all-in-one image builds the sandboxd binaries, including
`firecracker-agent`, and `distill_fs`; installs checksum-pinned gVisor and Kata
artifacts; installs the Firecracker VMM and guest kernel; and constructs the
matching guest-agent initrd. Runc remains build-time optional, and
`AKERNEL_ENABLE_FIRECRACKER=false` excludes the Firecracker payload.

The sandboxd submodule's runtime manifest is the source of truth for the
gVisor and Firecracker releases used by both sandboxd E2E and AKernel
packaging. Test an unreleased runtime by checking out the sandboxd commit that
pins it rather than overriding manifest fields from the AKernel build. Keep
sandboxd's pooled-TAP contract and the matching gVisor compatibility patches
validated together when upgrading.

The submodule gitlinks are the single source of truth for the sandboxd and
distill-fs revisions included in a clean release. `make build` always compiles
the local submodule worktrees, so developers may check out a different commit
or edit either directory and rebuild without pushing first. Each component
maintains and embeds its own semantic version: sandboxd uses
`version/VERSION`, while distill-fs uses the package version in `Cargo.toml`.
AKernel does not inject parent-repository version metadata into component
compilation.

To test an unreleased openYuanRong core wheel without rebuilding YuanRong,
provide both `OPEN_YR_CORE_WHEEL_URL` and `OPEN_YR_CORE_WHEEL_SHA256` to
`make build`. The complete wheel is verified before it replaces the pinned
release control plane.

To test an unreleased RRT binary, provide both `RRT_RUNTIME_URL` and
`RRT_RUNTIME_SHA256` to `make build`. The runtime build verifies the binary
before packaging it into the selected runtime root filesystem.

Inspect the selected local versions without building an image:

```bash
make versions
```

The final image uses standard OCI labels for the AKernel version and revision.
Component semantic versions are reported by their binaries, and their exact
source revisions are traceable through the AKernel commit's submodule gitlinks.

## Deploy

Use [`deploy/README.md`](./deploy/README.md) as the deployment entry point.
AKernel supports standalone, existing Kubernetes clusters via Helm, and
Terraform-based cloud provisioning.

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
reverse tunnels, read-only EROFS image roots and mounts, explicit `storage_mb`
quotas, and recovery across sandboxd restarts. Its root and filesystem image
mounts must be local or image-provider-backed regular EROFS files. It rejects
OCI/Nydus directory roots, directory mounts, writable live host binds, NVIDIA
GPUs, and nested KVM rather than weakening their semantics.

Do not add Firecracker-specific directory conversion, image caching, or
artifact reference counting to sandboxd or its image manager. Build EROFS
before sandbox creation and distribute it through the existing local or S3
imagefile paths. The bundled default runtime root already follows this model.

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

The deployment helper generates a stable IAM signing seed for the environment
and passes it to the Helm chart through Terraform. This allows JWT tokens to be
generated locally without exposing the IAM token API publicly.

If `.akernel/default/` already exists, `make config` asks before overwriting
`config.env` and `terraform.tfvars`. The existing `iam-seed` is reused unless
you delete it or explicitly provide `IAM_SEED_HEX`, so previously generated
tokens normally remain compatible.

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

## JWT Tokens

Generate SDK tokens locally with:

```bash
make token TTL=24h
make token TTL=100y
make token TTL=never
```

`make token` and `make print-env` print JWT credentials. Treat their output as
a secret: do not include it in logs, commits, or issue reports, and do not
repeat it in chat unless the user explicitly requests credential handoff.

The token generator intentionally follows openYuanrong's current signed JWT
format:
the `LITEBUS_DATA_KEY` hex seed is decoded to bytes, the JWT header and payload
are signed with HMAC-SHA256, and the hex digest string is base64url encoded.

Long-lived or never-expiring tokens are supported but should not be the default.
Current signed JWT tokens are stateless; a leaked token cannot be revoked
individually. Rotate the IAM signing seed to invalidate existing tokens.

## SDK And CLI

Minimal sandbox usage:

```python
from akernel_sdk import Sandbox

with Sandbox(cpu=2000, memory=4096) as sb:
    result = sb.commands.run("echo hello")
    print(result.stdout)
```

Same-node failover and explicit rollback retain the logical sandbox identity:

```python
with Sandbox(failover=True) as sb:
    if not sb.reload():
        print("no local checkpoint is available")
```

The current functional integration deliberately leaves anonymous local
checkpoint creation inside the workload through RRT's internal Unix socket.
The node sets `YR_RRT_CONTROL_SOCKET_PATH=/run/akernel`, making the socket
available at `/run/akernel/rrt.sock`. Do not present that socket protocol as a
stable public SDK interface or add public checkpoint catalog methods to the
SDK.

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

When the public Traefik dual-entrypoint mode is enabled, a host/IP-only
`AKERNEL_SERVER_ADDRESS` uses HTTPS/WSS on 443 for the frontend API and exec
websocket, and HTTP on 80 for sandbox port URLs. For standalone deployments,
use the Traefik container IP printed by `deploy/standalone/start.sh`:

```bash
export AKERNEL_SERVER_ADDRESS=<traefik-container-ip>
```

No separate `AKERNEL_GATEWAY_ADDRESS` is required for the default standalone
layout. When a custom topology sets it, the override applies only to public
sandbox port URLs and reverse tunnels; exec and file transfer continue to use
`AKERNEL_SERVER_ADDRESS`. Standalone uses `akerneldev/all-in-one:latest` by
default; pass `IMAGE` to test a locally built or differently tagged image.

Standalone GPU testing additionally requires NVIDIA Container Toolkit on the
host and `AKERNEL_ENABLE_GPU=true`. sandboxd uses the read-only cgroup
node-resource provider in standalone mode; Kubernetes deployments retain the
Kubernetes provider. Standalone explicitly enables local DNAT because the
frontend shares the node network namespace.

Standalone uses iptables NAT by default. Set `AKERNEL_NAT_BACKEND=bpfnat` to
use the experimental embedded TC eBPF backend. AKernel prepares the required
network-namespace sysctls, but bpfnat does not change firewall policy; custom
host-network deployments with `FORWARD=DROP` must allow traffic to and from
the sandbox bridge. YuanRong receives `INSTANCE_IP` in Kubernetes or the
default-route interface address in standalone mode; `AKERNEL_NODE_IP` is the
explicit override for multi-homed environments.

The standalone sandboxd filestore is a loop-mounted ext4 image under the
bind-mounted `deploy/standalone/data/` directory. Explicit `storage_mb`
quotas for runsc and Firecracker use this local-disk filestore. Without an
explicit quota, runsc retains its configured memory-backed overlay while
Firecracker creates its configured sparse ext4 default.

Terraform-managed Alibaba Cloud node pools instead attach a dedicated 300 GiB
ESSD by default, have ACK format it as XFS, and mount it at `/home/akernel`.
The Aliyun sandboxd configuration leaves `filestore_dir_size` unset and uses
`/home/akernel/filestore` directly, so writable layers and
`/home/akernel/checkpoints` share the native reflink-capable filesystem. Do
not set a bounded filestore size for this profile because that reintroduces a
loop-backed filesystem and disables the high-performance Firecracker C/R
layout.

The bundled node enables YuanRong's local-only sandbox snapshot data plane and
stores checkpoint state under the persistent `/home/akernel/checkpoints`
mount. RRT receives
`YR_RRT_CONTROL_SOCKET_PATH=/run/akernel` so sandbox workloads can trigger
their local checkpoint handoff through `/run/akernel/rrt.sock`. Recovery points
follow the source sandbox lifecycle. The public SDK exposes only failover and
reload, not checkpoint identifiers, restore, list, delete, or snapshot TTLs.
Keep local-only snapshot mode and the persistent checkpoint directory
configured together when changing node startup arguments.

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

The Python SDK installs `openyuanrong-sandbox` as its default execution
backend. The actor-based `openyuanrong-sdk` backend is deprecated and retained
only for compatibility with existing applications through the
`openyuanrong-sdk` extra. Do not update its pinned legacy version alongside
the default backend or extend it with new capabilities. Installing that extra
leaves both distributions present, so `openyuanrong-sandbox` remains the
automatic default unless `AKERNEL_BACKEND=openyuanrong-sdk` is set before
import. Backend selection happens once during import and backend modules are
loaded lazily on first use. Keep public `Sandbox`, `Commands`, `Filesystem`,
and value types independent of both native packages; all native conversions
belong under `akernel_sdk._backends`.

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

Python SDK releases use stable `vX.Y.Z` tags or release-candidate
`vX.Y.ZrcN` tags. The tag version must match the version in
`sdk/python/pyproject.toml`, and the tagged commit must be part of `main`.
Publishing a GitHub Release runs
`.github/workflows/release-python.yml`, which checks the SDK, builds and tests
the wheel and source distribution, and publishes them through the PyPI trusted
publisher configured for the `pypi` GitHub environment. Do not add a PyPI
password or API token to the repository.

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
