# AGENTS.md

This is the tool-neutral project instruction entry point for coding agents.
Tool-specific compatibility files should point here rather than duplicate
project guidance.

## Project Overview

AKernel provides cluster-backed remote sandbox environments for agents and
developer workflows. The current public user-facing surface is the Python
`akernel-sdk`, including the `akernel_sdk.Sandbox` API and the `ak` CLI.
The default sandbox runtime is gVisor runsc; callers may select Kata
Containers when the cluster has a KVM-capable node. Experimental whole-device
NVIDIA GPU and configurable writable-storage requests currently require
runsc.

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
`distill-fs` are maintained in their own upstream repositories. The all-in-one
Docker build copies and compiles their pinned Git submodules in dedicated
builder stages. It also downloads `runsc` from the official gVisor release
bucket and verifies its published SHA-512 checksum. The node image includes
the pinned `nvidia-container-cli` userspace tooling for experimental gVisor
GPU sandboxes; NVIDIA kernel drivers remain host-provided.
It installs the checksum-pinned openYuanRong core wheel as the control plane
and packages the checksum-pinned Kata Containers 4.0 runtime-rs shim,
Dragonball configuration, guest kernel, guest images, license, and sandbox
logger.

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
```

For a build that will be pushed and deployed, set `IMAGE_REPOSITORY` and
`IMAGE_TAG` when creating the deployment profile. A one-off `IMAGE_TAG`
override on `make build` does not update the profile consumed by `make push`.
The build creates only the selected image reference; it does not add a second
`akernel-all-in-one` alias. `make push` pushes that selected reference directly.

The build helper performs two Docker builds. `builder/runtime.Dockerfile`
assembles the Python runtimes and creates `yr-runtime-rootfs.img`.
`builder/node.Dockerfile` then compiles the node components and produces the
AKernel all-in-one image using that runtime image.

Initialize submodules with `git submodule update --init --recursive` before
building. The all-in-one image builds `sandboxd` and `sbox` from
`src/sandboxd`, builds `sandbox-logger`, builds `distill_fs` from
`src/distill-fs`, downloads the official gVisor `runsc` release pinned by
`GVISOR_RELEASE`, and extracts the required amd64 artifacts from the
checksum-pinned Kata release.

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

Kata is present in the default AKernel runtime configuration but is an
optional node capability. It additionally requires `/dev/kvm` to be usable
from the node container. A node without KVM remains ready and advertises only
runsc; a Kata request fails scheduling with a no-resource error when no
eligible node exists. Do not treat a configured runtime as an advertised
runtime.

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

Select Kata explicitly only when the cluster has an eligible node:

```python
with Sandbox(runtime="kata", cpu=2000, memory=4096) as sb:
    print(sb.commands.run("uname -s").stdout)
```

Request experimental gVisor GPU and disk-backed writable storage resources:

```python
with Sandbox(xpu="gpu:l20:1", storage_mb=20 * 1024) as sb:
    print(sb.commands.run("nvidia-smi -L").stdout)
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
layout. Standalone uses `akerneldev/all-in-one:latest` by default; pass `IMAGE`
to test a locally built or differently tagged image.

Standalone GPU testing additionally requires NVIDIA Container Toolkit on the
host and `AKERNEL_ENABLE_GPU=true`. sandboxd uses the read-only cgroup
node-resource provider in standalone mode; Kubernetes deployments retain the
Kubernetes provider. Standalone explicitly enables local DNAT because the
frontend shares the node network namespace.

The standalone sandboxd filestore is a loop-mounted XFS image under the
bind-mounted `deploy/standalone/data/` directory. Explicit `storage_mb` quotas
use this local-disk filestore; omitting `storage_mb` retains the configured
memory-backed writable overlay.

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

When changing a public SDK method, update its type annotations and docstring,
add or update unit coverage, and keep the SDK README and maintained examples in
sync. Benchmark programs under `sdk/python/benchmarks/` are manual tools and
are not part of the default test suite.

## Release

Python SDK releases use stable `vX.Y.Z` tags. The tag version must match the
version in `sdk/python/pyproject.toml`, and the tagged commit must be part of
`main`. Publishing a GitHub Release runs
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

The SDK integration and pressure tests are runtime-selectable. Kata tests
require a KVM-capable standalone or cluster node:

```bash
AKERNEL_RUN_INTEGRATION=1 \
AKERNEL_TEST_RUNTIME=kata \
python -m pytest sdk/python/tests/integration/test_sandbox.py

python sdk/python/benchmarks/sandbox_pressure.py --runtime kata

python sdk/python/benchmarks/sandbox_pressure.py --storage-mb 256
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
- Do not assume a fixed proxy address. Follow the user's network instructions:
  public dependency downloads may require the configured proxy, while
  Docker-local addresses, Pod IPs, private Kubernetes APIs, and cloud-internal
  endpoints normally require `NO_PROXY` or command-scoped proxy removal. Note
  that repository Make helpers unset shell proxy variables and Docker builds
  rely on the Docker daemon or BuildKit proxy configuration.
- Use Conventional Commits with a concise title and a prose body explaining
  what changed and why; do not create title-only commits. Keep a blank line
  between title and body, wrap the body for terminal readability, and sign
  commits with `git commit -s` to include the Developer Certificate of Origin
  sign-off.
- Keep unrelated dirty files out of commits, especially local deployment state,
  generated binaries, Terraform state, kubeconfigs, tokens, and private
  registry configuration.
