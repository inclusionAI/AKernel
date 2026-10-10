# AKernel Standalone Deployment

Standalone runs AKernel on one machine without Kubernetes. Two containers share the default container bridge:

- `akernel-node` runs the privileged AKernel all-in-one image.
- `akernel-traefik` runs the official Traefik image as the HTTPS API and HTTP sandbox-port gateway.

The separate gateway network namespace lets sandboxd's normal `PREROUTING` rules handle incoming traffic. Local-output DNAT handles frontend traffic originating in the node namespace. No host ports are published; clients use the Traefik container IP.

## Quick start from source

Run these commands from the AKernel repository root. Install Git, GNU Make, Python 3.10 or newer, curl, and Docker with BuildKit first. Supported hosts are Linux with a native Linux/amd64 or Linux/arm64 Docker daemon, or an Apple Silicon Mac with OrbStack running a native Linux/arm64 daemon. The current user must be able to access the local Docker daemon directly. Linux also needs the kernel capabilities described under [network backend](#network-backend); loading host modules requires root or passwordless sudo.

```bash
git clone https://github.com/inclusionAI/AKernel.git
cd AKernel
make standalone
source .akernel/standalone/data/sdk-env.sh
```

`make standalone` checks the host and Docker daemon, initializes `src/sandboxd` if missing, verifies its revision against the parent gitlink, builds the current source, and starts that exact image through `start.sh`. It waits for the node and gateway health checks before printing the SDK address and environment-file path. It does not need a cloud profile or registry push.

The source entry point selects the Docker daemon's native architecture and uses the `rrt` profile with runsc. Kata, Firecracker, runc, and GPU access are disabled unless explicitly enabled on a supported platform. It builds a unique local image tag and records source revision, image, and container identities in `source-state.json` under the data directory. Local AKernel edits are built and marked `.dirty` in the recorded revision. An initialized sandboxd checkout must be clean and match the gitlink; the helper does not fetch, switch, or overwrite it.

The default source data directory is `.akernel/standalone/data`, including the signing seed, 24-hour SDK token, generated service configuration, `source-state.json`, and `sdk-env.sh`. The environment file reads the token from its protected file when sourced; commands print credential paths rather than token values. The default local state directory is ignored by Git; keep custom data directories out of version control too.

Install the SDK in a virtual environment and create a sandbox:

```bash
python3 -m venv .akernel/standalone/venv
source .akernel/standalone/venv/bin/activate
python3 -m pip install ./sdk/python
python3 - <<'PYCODE'
from akernel_sdk import Sandbox

with Sandbox() as sb:
    print(sb.commands.run("echo hello-from-standalone").stdout)
PYCODE
```

See the [SDK guide](../../sdk/python/README.md) for commands, files, PTYs, network policies, and port forwarding.

### Status, stop, and rebuild

```bash
make standalone-status
make standalone-stop
make standalone
source .akernel/standalone/data/sdk-env.sh
```

Status reports the selected profile and image, container state, gateway health, and credential-file paths. Release all sandboxes before stopping. The source stop command checks recorded container identities and data mounts, verifies that the sandbox inventory is empty, then stops the gateway before the node. It retains data, deployment identity, and local images.

Startup refuses either existing standalone container name, including stopped containers, before building. It does not replace a running instance. Use the matching stop command before rebuilding; source management refuses containers that do not match the selected source profile. After a failed startup, inspect `make standalone-status` and stop the partial instance after releasing any sandboxes; the helper retains its data and images.

To use an absolute data directory, set the same value for each management command and source its environment file:

```bash
export AKERNEL_STANDALONE_DATA_DIR=/absolute/path/to/standalone-data
make standalone
source "${AKERNEL_STANDALONE_DATA_DIR}/sdk-env.sh"
make standalone-status
# After releasing all sandboxes:
make standalone-stop
```

Only one standalone instance can run per Docker daemon because container names are shared. Separate data directories retain independent state; they do not provide concurrent instances.

## Start an existing image

The direct launcher remains available for Docker or Pouch. It uses `akerneldev/all-in-one:latest` unless `IMAGE` is set, reusing a local copy or pulling the image when absent:

```bash
./deploy/standalone/start.sh
# After releasing all sandboxes:
./deploy/standalone/stop.sh
```

Unlike the source entry point, this path does not build source or create source-management metadata. Its default data directory is `deploy/standalone/data`, independent of the current working directory. Override it with an absolute `AKERNEL_STANDALONE_DATA_DIR`. The launcher prints the SDK address and token path; see [SDK connection and token lifetime](#sdk-connection-and-token-lifetime).

Select an existing image or gateway image explicitly:

```bash
IMAGE=registry.example.com/akernel/all-in-one:release \
  ./deploy/standalone/start.sh

TRAEFIK_IMAGE=traefik:v3.6.8 ./deploy/standalone/start.sh
```

The gateway defaults to `traefik:v3.6.8`. For private registry or OSS access, configure the [authentication inputs](#authentication-and-registry-inputs) before starting.

## Runtime selection

Runsc is the default sandbox runtime. The source entry point uses `RUNTIME_PROFILE=rrt` and excludes optional payloads by default. A configured VM runtime is advertised only when the node has usable KVM and its matching payload. Check image contents before selecting optional runtimes with the existing-image launcher.

### Optional runc

Enable ordinary Linux runc in the same source build and launch:

```bash
make standalone AKERNEL_ENABLE_RUNC=true \
  AKERNEL_RUNC_RESOLV_CONF=/absolute/path/to/approved-resolv.conf
```

Then use `Sandbox(runtime="runc")`. Runc uses the Docker host kernel, including the OrbStack Linux kernel on Mac, and does not require KVM for ordinary sandboxes. See [DNS resolver sources](#dns-resolver-sources) before selecting the resolver file. On a Linux host exposing usable `/dev/kvm`, a runc sandbox can request it with `extra_config={"enableKVM": True}`.

For the existing-image launcher, both build inclusion and runtime enablement are required: select an image built with `AKERNEL_ENABLE_RUNC=true` and pass the same flag to `start.sh`.

### Kata, Firecracker, and GPU

On Linux/amd64, opt into the VM payloads with `make standalone AKERNEL_ENABLE_KATA=true` or `make standalone AKERNEL_ENABLE_FIRECRACKER=true`. Both require usable `/dev/kvm` and hardware or nested virtualization on the Docker host. Firecracker also requires compatible host storage capabilities; see the [deployment guide](../README.md#guided-deployment) for its I/O policy and checkpoint compatibility.

Experimental NVIDIA GPU sandboxes require runsc, a compatible host NVIDIA driver, and NVIDIA Container Toolkit. Enable node GPU access with `AKERNEL_ENABLE_GPU=true` on a supported Linux/amd64 host. `AKERNEL_GPU_DEVICES` selects Docker's `--gpus` device subset. The image provides `nvidia-container-cli`, not a host driver.

Linux/arm64 supports runsc and optional runc, with Kata, Firecracker, and GPU access unavailable. The source launcher selects the native daemon architecture rather than a client-side `DOCKER_DEFAULT_PLATFORM`; cross-architecture emulation is not supported. Use `make build` for image-only cross builds or the optional AMD64 Python runtime profile; launch existing images on a matching supported daemon.

See the maintained [runtime selection example](../../sdk/python/examples/sandbox_runtime.py) for client usage.

## OrbStack on Apple Silicon

Use the source quick start with a running OrbStack Docker engine. It chooses native Linux/arm64 automatically. The launcher validates the platform and image capabilities before modifying credentials or resolver configuration. It verifies both node and gateway image architectures and uses `--platform linux/arm64` for pulls and container launches. A wrong-architecture cached image is rejected rather than overwritten.

Before starting the node, a disposable privileged container probes TUN/TAP, veth, writable cgroup v2, iptables/ip6tables, conntrack matches, ipset, and loop-backed ext4. It also checks for the FUSE device; verify userspace FUSE image mounts through a sandbox when needed. Enabling runc adds EROFS and ext4-backed writable-overlay probes. The probes clean up temporary mounts and paths under the selected data directory. macOS does not run `modprobe`.

The profile uses iptables networking; runc KVM requests are unavailable. Before enabling runc, compare `scutil --dns` with resolver addresses reachable from sandbox namespaces: a macOS `/etc/resolv.conf` snapshot does not capture all scoped or split-DNS policies. Follow [DNS resolver sources](#dns-resolver-sources) and verify DNS and certificate-validated HTTPS in a new runc sandbox.

The Traefik container IP is the SDK address. For a manual local gateway health check, the standalone certificate is self-signed:

```bash
curl --noproxy '*' -fkSs "https://<traefik-container-ip>/healthz"
```

For outbound proxy behavior and build downloads, see the [image build download guide](../../builder/README.md#build-proxy-and-network).

## Configuration

### SDK connection and token lifetime

For the source entry point, source the `sdk-env.sh` path printed at startup. For a direct launch, use the gateway IP and token from its selected data directory:

```bash
export AKERNEL_SERVER_ADDRESS="$(docker inspect \
  --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
  akernel-traefik)"
export AKERNEL_TOKEN="$(cat "${AKERNEL_STANDALONE_DATA_DIR:-$PWD/deploy/standalone/data}/token")"
```

Run this direct-launch example from the repository root; replace Docker with Pouch when appropriate. An IP-only SDK address selects HTTPS/WSS on port 443 for the API and exec, and HTTP on port 80 for sandbox ports. No separate `AKERNEL_GATEWAY_ADDRESS` is required.

The signing seed and token remain in the selected data directory. The seed is reused on restart; token lifetime defaults to 24 hours. Set `STANDALONE_TOKEN_TTL=7d` on `make standalone` or `start.sh` to choose a different lifetime. Keep token and signing-seed contents out of logs. Removing the seed creates a new deployment identity; release sandboxes and stop the instance before replacing identity or data.

### DNS resolver sources

With the bundled ACL-enabled configuration, runsc, Kata, and Firecracker use managed DNS through the node proxy, even without a sandbox network policy. Runc uses direct DNS; disabling node ACLs selects direct DNS for every runtime. `plugin.runtime.resolv_conf_path` supplies the managed proxy upstream and resolver search/domain/options. The optional `plugin.runtime.direct_resolv_conf_path` supplies direct-DNS sandboxes; when empty, it inherits `resolv_conf_path`. Existing runtime-owned or explicit resolver mounts retain precedence in direct mode.

When runc is enabled, the launcher copies a non-loopback resolver file to `sandboxd/config/direct-resolv.conf` under the selected data directory and sets the node's `plugin.runtime.direct_resolv_conf_path` to that snapshot. It preserves `plugin.runtime.resolv_conf_path`, which defaults to `/etc/resolv.conf`. Docker's embedded resolver (`127.0.0.11`) is valid in the node namespace but not a direct-DNS sandbox's separate namespace. Custom templates must retain exactly one `# AKERNEL_DIRECT_RESOLVER` marker under `[plugin.runtime]` and the `# AKERNEL_RUNTIME_RUNC` marker under `[plugin.runtime.runtime_binary]` when enabling runc.

On hosts without systemd-resolved, the launcher can select a usable `/etc/resolv.conf` automatically. On systemd-resolved hosts, automatic selection fails because a flat file cannot preserve per-link or VPN split-DNS routing. Set `AKERNEL_RUNC_RESOLV_CONF` to an absolute file containing approved resolver addresses for all names direct-DNS workloads need. An upstream server list alone does not encode which domains belong to each link; use an approved internal resolver or operator-managed forwarder reachable from sandbox namespaces for private domains.

Validation rejects namespace-local addresses but cannot prove reachability or domain routing. To refresh the snapshot, release all sandboxes, stop, and restart standalone. Verify internal and external DNS in a new direct-DNS sandbox and verify managed DNS separately. Do not restart the node while a sandbox or nested VM is running.

### Build proxy and network

Downloads use official sources by default. Optional package mirrors, proxy forwarding, build networking, and checksum-paired release caches are documented in the [image build download guide](../../builder/README.md). Container proxy credentials belong in the launcher's protected profile env-file.

### Network backend

Standalone uses iptables NAT by default. Linux requires the [deployment network capabilities](../README.md#network-acls); the launcher loads the required host modules and the node prepares namespace-local sysctls. OrbStack checks equivalent capabilities through its disposable container preflight.

Linux nodes without the required iptables NAT and conntrack support may select the experimental embedded TC eBPF backend with `AKERNEL_NAT_BACKEND=bpfnat`. It requires permission to load TC eBPF programs and mount or access writable bpffs. AKernel enables forwarding and disables global reverse-path filtering inside the node namespace for bpfnat local DNAT. It does not override host firewall policy; custom host-network deployments with `FORWARD=DROP` must allow bridge- and sandbox-CIDR-scoped traffic to and from `sandbox0`.

YuanRong receives the IPv4 address of the default-route interface so creation of `sandbox0` cannot change the advertised node address. Use `AKERNEL_NODE_IP` for an explicit multi-homed override. TCP and UDP port 53 on the sandbox bridge must be free for the managed DNS proxy.

Release all sandboxes and stop the old node before upgrading a data directory to an ACL-enabled image. Sandboxd refuses ACL initialization while pre-ACL sandbox records remain. See the [deployment network requirements](../README.md#network-acls) for the full capability contract.

### Storage and recovery

Explicit `storage_mb` quotas for runsc and Firecracker use the bounded ext4 filestore at `/home/akernel/filestore`. The selected host data directory is bind-mounted at `/home/akernel`; sandboxd creates a loop-backed filesystem image there when needed. Without an explicit quota, runsc retains its configured memory-backed overlay while Firecracker uses its configured sparse ext4 default. For shared image-cache capacity, see [ChunkDB configuration](../README.md#shared-chunkdb-capacity).

Runsc and Firecracker checkpoints use YuanRong's local-only snapshot mode under `/home/akernel/checkpoints`. Workloads trigger anonymous recovery points through `POST /checkpoint` on `/run/akernel/rrt.sock`, and the SDK can reload the same logical sandbox from its latest usable point. Recovery points follow the sandbox lifecycle and are not reusable SDK objects.

### Authentication and registry inputs

Public-image startup needs no registry or OSS credentials. For private backends, edit the applicable files under `deploy/standalone/config/` before launch:

| File | Purpose |
| :--- | :--- |
| `oss_auths.json` | OSS endpoint/bucket access credentials |
| `registry_auths.json` | Registry authentication |
| `oss.json` | OSS endpoint configuration |
| `registry.json` | Image registry configuration |

Keep credential changes local and out of Git. The launcher copies the inputs into the selected data directory. Consult the backend's required credential format rather than placing secrets in command arguments.

### Logs and service inspection

```bash
docker logs -f akernel-node
docker logs -f akernel-traefik
docker exec -it akernel-node bash
docker exec akernel-node systemctl status
```

Use passwordless `sudo` when required for Docker access. For direct Pouch deployments, replace `docker` with `pouch`. Stop/removal failures return nonzero; inspect the reported failure before retrying. Stopping leaves the data directory and identity files intact.

## Directory structure

```text
deploy/standalone/
├── README.md             # Deployment guide
├── manage.py             # Source build, status, and stop entry point
├── start.sh              # Start an existing AKernel image and gateway
├── stop.sh               # Stop the standalone containers
├── orbstack-preflight.sh # Probe OrbStack Linux capabilities
└── config/               # Runtime, registry, and OSS inputs
```
