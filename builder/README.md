# Image build downloads

The shared `deploy/scripts/build-image.sh` helper builds the runtime rootfs and all-in-one node image for `make build` and `make standalone`. Downloads use official repositories and pinned upstream releases by default.

## Optional E2B runtime

The default build still creates AKernel's Execd-only runtime. To package envd,
select the optional target and provide both archives from the same ADX build:

```bash
AKERNEL_ENABLE_E2B=true \
ADX_RELEASE_URL="$RELEASE_URL" ADX_RELEASE_SHA256="$RELEASE_SHA256" \
ADX_EXECD_URL="$EXECD_URL" ADX_EXECD_SHA256="$EXECD_SHA256" \
make build
```

The selected ADX release must implement E2B compatibility. Its Execd component
must have been built with ADX's `ADX_ENVD_SOURCE` option and contain
`adx-runtime-rootfs.img`, `envd-manifest.json` and `envd-LICENSE`, covered by
the component manifest. The current default release pins do not provide this
capability; this option requires explicit archive URL/checksum pairs. Standard
ADX split-component archives use a GNU release target even though Execd itself
is a static musl executable.

AKernel verifies the archive and component hashes and architecture, then uses
the ADX EROFS image unchanged as `akernel-runtime-rootfs.img`. It contains Execd,
envd, bash and the dependencies assembled by ADX. AKernel does not compile envd
or maintain another copy of its upstream pins. The Node build also checks that
the platform release and runtime component identify the same commit, target
and Execd bytes; mismatches fail the build. Provenance and the envd license are
installed under `/opt/akernel/runtime/metadata/`, with the envd manifest at
`envd/manifest.json`.

Packaging alone does not enable E2B routes. Supply a reviewed deployment YAML
through the existing `AKERNEL_ADX_CONFIG` entry point. Keep its current runtime
paths and bootstrap entrypoint, and add these fields to the corresponding
sections (merge them into the existing maps):

```yaml
runtime_profile:
  env:
    ADX_ENVD_PATH: /__adx/usr/local/bin/envd
    ADX_ENVD_SHA256: REPLACE_WITH_ENVD_MANIFEST_SHA256_FIELD
    ADX_ENVD_DEFAULT_USER: user
    ADX_ENVD_DEFAULT_WORKDIR: /home/user
service_overrides:
  apiserver:
    config:
      e2b:
        domain: sandbox.example.com
        envdVersion: REPLACE_WITH_ENVD_MANIFEST_VERSION_FIELD
        envdIngressSources: [REPLACE_WITH_EXACT_RELAY_SOURCE_IPV4]
  ingress:
    env:
      ADX_DATA_PLANE_INGRESS_E2B_ENABLED: "true"
      ADX_DATA_PLANE_INGRESS_PORT_HOST_DOMAIN: sandbox.example.com
```

This fragment applies to profile-based standalone deployments. Explicit-service
deployments put the same API configuration and Ingress environment in their
existing service objects; the Helm chart does not yet expose dedicated E2B
values. Configure matching trusted profiles on API Server and workers. Select
runsc, Firecracker or firecracker-pvm according to the ADX release's supported
backends and the actual node capability. Firecracker `fromImage` uses OCI
directory roots: enable `virtiofs_enabled = true` under
`[plugin.runtime.firecracker]` in the dedicated worker's sandboxd TOML. The
stock profile disables virtio-fs for its local EROFS runtime.
E2B lifecycle requests also need their
control-plane routes forwarded to API Server by Ingress; preserve the existing
native routes and include `/sandboxes`, `/v2/sandboxes`, `/snapshots`, `/templates`,
`/v2/templates` and `/v3/templates` as required by that release.

Use ADX's E2B deployment instructions for the complete route allowlist, OCI
capabilities, authentication, TLS/DNS and network isolation configuration.
In gateway-authenticated mode envd has no guest token: restrict external and
other-sandbox access to its port to the exact trusted Relay sources. This
packaging option does not establish those network rules by itself. Use a
dedicated matching worker profile and retain old runtime artifacts for existing
instances and snapshots; building a new image does not migrate them.

`make build-helper-test` checks target selection, rejection of missing or
inconsistent artifacts, and byte-preserving assembly without compiling ADX or
running a sandbox. A full image build and E2B/native SDK lifecycle tests against
the resulting image remain separate acceptance steps.

## Package repositories

Select mirrors for one invocation without changing host package-manager or Docker daemon configuration:

```bash
make standalone AKERNEL_BUILD_MIRROR=aliyun
# The same option applies to make build.
```

`AKERNEL_BUILD_MIRROR` accepts `official` (the default) or `aliyun`. The Aliyun preset covers Ubuntu APT, Debian APT in the Go/Rust builder stages. Ubuntu ARM64 selects `ubuntu-ports`; AMD64 selects `ubuntu`. APT source rewriting preserves suites, components, signing keys and signature verification, and changes only recognized official repository URIs in `.list` and `.sources` files. Third-party repositories retain their own URLs. Go keeps its official proxy unless the caller explicitly sets `GOPROXY`; module checksum verification stays enabled.

APT uses the Aliyun HTTP endpoints because minimal Ubuntu images do not yet contain CA certificates; APT still verifies signed repository metadata and package hashes. An explicit HTTPS APT mirror requires a base image with a populated CA trust store; TLS verification is never disabled. See the official [Ubuntu](https://developer.aliyun.com/mirror/ubuntu), [Ubuntu Ports](https://developer.aliyun.com/mirror/ubuntu-ports), [Debian](https://developer.aliyun.com/mirror/debian), [Debian security](https://developer.aliyun.com/mirror/debian-security) mirror documentation.

Nonempty caller overrides take precedence over the preset. These public repository URLs are passed as Docker build arguments, so they must not contain credentials, query parameters or fragments:

| Environment variable | Scope |
| :--- | :--- |
| `AKERNEL_APT_UBUNTU_MIRROR` | Ubuntu AMD64 archive and security repository root |
| `AKERNEL_APT_UBUNTU_PORTS_MIRROR` | Ubuntu ARM64 ports repository root |
| `AKERNEL_APT_DEBIAN_MIRROR` | Debian archive repository root |
| `AKERNEL_APT_DEBIAN_SECURITY_MIRROR` | Debian security repository root |
| `GOPROXY` | Go module downloads; public URL with optional `,direct`, or `direct`/`off` |

Go repository selections are used only during builds; no mirror environment setting is added to the sandbox runtime. Selected APT repository URLs remain in the built images. Mirrors may lag upstream, and failures remain visible instead of automatically switching sources. Docker base-image pulls, GitHub release assets, GitLab source, Cargo packages, NVIDIA repositories use their existing sources. Docker registry mirrors belong to the daemon configuration.

Image builds retry failed APT index refreshes up to five times with increasing delays. Each attempt requires a complete index update and retains repository signature and hash verification. Persistent failures stop the build before package installation.

## Release artifacts

Initialize sandboxd with `git submodule update --init src/sandboxd`; existing checkouts should first run `git submodule sync -- src/sandboxd`. Helm and standalone enable runsc background restore and checkpoint output synchronization. The sandboxd runtime archive manifest supplies the build inputs for gVisor, runc and Firecracker.

Package mirrors do not serve the ADX packages or other release files. For an explicitly selected artifact cache or an unreleased build, the helper accepts these URL/checksum pairs:

| Environment variables | Build stage | Equivalent helper options |
| :--- | :--- | :--- |
| `ADX_EXECD_URL`, `ADX_EXECD_SHA256` | Execd archive | `--adx-execd-url`, `--adx-execd-sha256` |
| `ADX_RELEASE_URL`, `ADX_RELEASE_SHA256` | ADX platform archive | `--adx-release-url`, `--adx-release-sha256` |
| `OTELCOL_CONTRIB_URL`, `OTELCOL_CONTRIB_SHA256` | OTel collector archive | `--otelcol-contrib-url`, `--otelcol-contrib-sha256` |

The default ADX archives are pinned for AMD64. ARM64 builds require both ADX URL/checksum pairs to select packages built for ARM64; an AMD64 archive is rejected by ELF validation.

Each pair is validated before either image build; downloads retain checksum and native ELF architecture checks. A mirror of the same release must use its existing pinned checksum. Runtime versions and gVisor, runc, Firecracker and distill-fs pins continue to come from their existing manifests.

Source version/revision labels and artifact URL overrides are declared near their uses so changing those inputs preserves preceding dependency layers.

### Environment API source builds

The current AKernel source adapter uses `adx-environment==0.1.0rc3` and
`/api/environment/v1`. The Dockerfiles pin the platform and Execd archive
URLs and SHA-256 values. Archive manifests identify the packaged binaries;
`src/agent-dx/` is a source reference.

Use the matching SDK wheel bundled under `sdk/` in the platform archive for
source validation when the dependency is not available in your package index:

```bash
export UV_FIND_LINKS=/absolute/path/to/extracted-adx-release/sdk
make standalone-source-test
```

Archive URL/checksum overrides remain available for validating another build;
select the platform and Execd archives from the same ADX revision and install
that release's SDK wheel. SDK publication requires the declared dependency
to be available from the configured package index.

## Build proxy and network

Export proxy variables for an endpoint reachable from the Docker builder, then opt into forwarding them:

```bash
make standalone AKERNEL_BUILD_PROXY=true
# The same option applies to make build.
```

`AKERNEL_BUILD_PROXY` accepts `true` or `false` and defaults to `false`. The helper forwards the predefined uppercase and lowercase `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, and `ALL_PROXY` build arguments by name to both builds. Keep their values out of command arguments, logs, saved configuration, and Dockerfile `ARG` declarations. Docker may also populate proxies from its [client configuration](https://docs.docker.com/engine/cli/proxy/); omitting this option does not disable those settings.

`AKERNEL_BUILD_NETWORK` accepts `default` (the default) or `host`. Selecting `host` passes `--network host` to both builds, allowing RUN instructions to reach host-local services, including a localhost proxy with OrbStack. These options affect image-build downloads only; standalone containers and sandbox traffic retain their deployment settings.

OrbStack can follow [macOS proxy settings](https://docs.orbstack.dev/docker/network) independently of container environment variables. An empty proxy environment or `curl --noproxy '*'` alone does not establish a direct outbound connection. Build and standalone helpers do not change host-wide proxy settings.
