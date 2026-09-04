# AKernel Standalone Deployment

This directory contains scripts and configurations for running AKernel in
standalone mode using Docker or Pouch, without Kubernetes. The deployment uses
two containers on the default container bridge:

- `akernel-node` runs the AKernel all-in-one image.
- `akernel-traefik` runs the official Traefik image as the external gateway.

Keeping the gateway in a separate network namespace allows sandboxd's normal
`PREROUTING` rules to handle gateway traffic. Traefik passes HTTPS/TLS through
to the Rust Edge Frontend on port 8443 and forwards plain HTTP to Edge on port
8080. Edge sends control requests to the core frontend and sandbox data traffic
through the Rust Node Proxy. The all-in-one components send traffic from the
node network namespace, so the standalone sandboxd config also enables its
local-output DNAT support.

Building the all-in-one image requires the separately published Rust data-plane
wheel. Download it first, then provide its absolute path and SHA-256:

```bash
make build IMAGE_REPOSITORY=akernel-local IMAGE_TAG=rust-data-plane \
  OPEN_YR_DATA_PLANE_WHEEL_PATH=/absolute/path/openyuanrong_data_plane.whl \
  OPEN_YR_DATA_PLANE_WHEEL_SHA256=<sha256>
```

The local wheel is mounted into the Docker build through a dedicated read-only
BuildKit context and is not retained in an image layer. If a public artifact
URL becomes available, use `OPEN_YR_DATA_PLANE_WHEEL_URL` instead of
`OPEN_YR_DATA_PLANE_WHEEL_PATH`.

The default runtime is gVisor `runsc`. The bundled image also contains Kata
Containers and Firecracker. Both `Sandbox(runtime="kata")` and
`Sandbox(runtime="firecracker")` require `/dev/kvm` plus hardware or nested
virtualization on the Docker host. Nodes without KVM remain usable with runsc
and do not advertise either VM runtime to the scheduler.

`runsc` uses the `systrap` platform by default. Hosts that expose `/dev/kvm`
may select gVisor's KVM platform for standalone startup with
`AKERNEL_RUNSC_PLATFORM=kvm ./start.sh`.

See the maintained
[runtime selection example](../../sdk/python/examples/sandbox_runtime.py) for
client usage.

Default image builds exclude the optional native Linux `runc` runtime, and
standalone does not advertise it by default. Build an image containing the
payload, then enable it when host-kernel container isolation is appropriate:

```bash
AKERNEL_ENABLE_RUNC=true make build \
  IMAGE_REPOSITORY=akernel-runc IMAGE_TAG=local

IMAGE=akernel-runc:local AKERNEL_ENABLE_RUNC=true ./start.sh
```

Clients then select it with `Sandbox(runtime="runc")`. A sandbox may request
the configured KVM character device with
`extra_config={"enableKVM": True}` when the host exposes `/dev/kvm`.

Experimental NVIDIA GPU sandboxes use gVisor nvproxy. The host must provide a
compatible NVIDIA driver and NVIDIA Container Toolkit. Enable GPU access to
the node container with:

```bash
AKERNEL_ENABLE_GPU=true ./start.sh
```

The all-in-one image contains `nvidia-container-cli`, but not the host driver.
Use `AKERNEL_GPU_DEVICES` to override Docker's `--gpus` value when only a
device subset should be assigned.

Explicit sandbox storage quotas for runsc and Firecracker use the bounded ext4
filestore mounted at `/home/akernel/filestore`. The standalone data directory
is bind-mounted from the host, and sandboxd creates a loop-backed filesystem
image there when needed; quota-backed writable layers therefore use local disk
rather than tmpfs. Without `storage_mb`, runsc retains its configured
memory-backed overlay while Firecracker uses its configured sparse ext4
default.

Sandbox checkpoints for runsc and Firecracker use YuanRong's local-only
snapshot mode. Checkpoint state is kept under the persistent
`/home/akernel/checkpoints` data mount. Workloads trigger an anonymous recovery
point through `POST /checkpoint` on `/run/akernel/rrt.sock`, and the SDK can
reload the same logical sandbox from the latest usable point. Recovery points
follow the source sandbox lifecycle; they are not exposed as reusable SDK
objects.

`start.sh` loads the host `tun` module and verifies `/dev/net/tun` before
starting the pooled-TAP runtimes. Runc retains its separate veth network path.

### Network backend

Standalone uses the iptables NAT backend by default. Nodes without the
required iptables NAT and conntrack kernel modules can select the experimental
embedded TC eBPF backend:

```bash
AKERNEL_NAT_BACKEND=bpfnat ./start.sh
```

The node container remains privileged and must be able to load TC eBPF
programs and mount or access bpffs. AKernel enables IPv4 forwarding before
sandboxd starts and disables global reverse-path filtering inside the node
network namespace when bpfnat local DNAT is enabled. bpfnat replaces NAT; it
does not override firewall policy. A custom host-network deployment whose
`FORWARD` policy is `DROP` must allow traffic to and from `sandbox0` with
bridge- and sandbox-CIDR-scoped rules.

AKernel passes YuanRong the IPv4 address of the default-route interface so the
later creation of `sandbox0` cannot change the advertised node address. Set
`AKERNEL_NODE_IP` only when a multi-homed deployment requires an explicit
override.

The standalone configuration enables per-sandbox network ACLs. With the
default iptables backend, `start.sh` loads IPv6 filter-table, `br_netfilter`,
`xt_physdev`, conntrack/connmark, and timeout-capable ipset modules on the host
before the node starts; the node then enables IPv4 and IPv6 bridge netfilter in
its own network namespace.
The optional bpfnat backend instead
requires TC eBPF support and a writable bpffs. TCP and UDP port 53 on the
sandbox bridge must remain free for sandboxd's managed DNS proxy. Before
upgrading an existing standalone data directory to an ACL-enabled image,
terminate its sandboxes and stop the old node cleanly; sandboxd refuses to
initialize ACLs while pre-ACL sandboxes remain in its store.

## Directory Structure

```
deploy/standalone/
├── README.md                  # This file
├── start.sh                   # Start AKernel and Traefik containers
├── stop.sh                    # Stop AKernel and Traefik containers
└── config/                    # Configuration files
    ├── config.json            # OCI runtime configuration
    ├── oss_auths.json         # OSS authentication (edit as needed)
    ├── oss.json               # OSS backend configuration (edit as needed)
    ├── registry_auths.json    # Registry authentication (edit as needed)
    ├── registry.json          # Image registry configuration (edit as needed)
    └── sandboxd_config.toml   # sandboxd runtime configuration
```

## Quick Start

### 1. Configure Authentication (as needed)

If you need to access private registries or OSS backends, edit the following configuration files to add your authentication credentials:

#### `config/oss_auths.json`
Update with your OSS credentials:
```json
{
  "your-oss-endpoint/your-oss-bucket": {
    "access_key_id": "your-access-key-id",
    "access_key_secret": "your-access-key-secret"
  }
}
```

#### `config/registry_auths.json`
Update with your registry credentials:
```json
{
  "auths": {
    "your-docker-registry": {
      "Auth": "base64-encoded-username:password"
    }
  }
}
```

### 2. Optional: Configure OSS and Registry Endpoints

Edit `config/oss.json` and `config/registry.json` to point to your actual OSS and registry endpoints.

### 3. Start AKernel

```bash
cd deploy/standalone
./start.sh
```

This will:
- Check prerequisites (Docker or Pouch availability)
- Create data directory
- Use `akerneldev/all-in-one:latest` if `IMAGE` is not set, reusing a local
  copy when present and otherwise pulling it from Docker Hub
- Start the privileged AKernel all-in-one container
- Start an independent Traefik container for the HTTPS API and HTTP sandbox
  port-forwarding gateway
- Configure Traefik with TLS passthrough and HTTP forwarding to the Rust Edge
  Frontend
- Wait for the Rust Edge Frontend readiness endpoint before exposing it
- Generate a deployment-specific IAM signing seed and a 24-hour SDK token
- Generate a sandboxd config using `AKERNEL_NAT_BACKEND` (`iptables` by
  default)
- Print the Traefik container IP to use as `AKERNEL_SERVER_ADDRESS`

No host ports are published. On Linux, the host accesses Traefik directly
through its Docker bridge IP.

### 4. Check Status

```bash
# View AKernel logs
sudo docker logs -f akernel-node

# View gateway logs
sudo docker logs -f akernel-traefik

# Enter the container
sudo docker exec -it akernel-node bash

# Check systemd services
sudo docker exec akernel-node systemctl status
```

**Note:** If using Pouch, replace `docker` with `pouch` in the commands above.

### 5. Stop AKernel

```bash
./stop.sh
```

## Customization

### SDK Connection

Traefik listens on port 443 for the AKernel API and port 80 for sandbox port
forwarding. These ports are not published on the host. Use the Traefik
container IP printed by `start.sh`, or retrieve it later:

```bash
TRAEFIK_IP=$(docker inspect \
  --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
  akernel-traefik)
```

Set the SDK environment:

```bash
export AKERNEL_SERVER_ADDRESS="${TRAEFIK_IP}"
export AKERNEL_TOKEN="$(cat data/token)"
```

The signing seed is stored in `data/iam-seed` and reused while that standalone
data directory exists. Delete the data directory to create a new deployment
identity. Set `STANDALONE_TOKEN_TTL` when starting AKernel to choose a different
token lifetime, for example `STANDALONE_TOKEN_TTL=7d ./start.sh`.

When `AKERNEL_SERVER_ADDRESS` contains only an IP address, the SDK uses HTTPS
port 443 for the API and HTTP port 80 for sandbox port forwarding. No separate
`AKERNEL_GATEWAY_ADDRESS` is required.

### Container Image Version

By default, `start.sh` uses the public Docker Hub image
`akerneldev/all-in-one:latest`. Override it with the `IMAGE` environment
variable to test another registry, tag, or locally built image:
```bash
IMAGE="<your-docker-registry>:<your-tag>" ./start.sh
```

The gateway defaults to `traefik:v3.6.8`. Override it independently when
needed:

```bash
TRAEFIK_IMAGE="traefik:v3.6.8" ./start.sh
```

To run an isolated second standalone instance on the same Docker host, give it
distinct container names and a distinct data directory. Pass the same names to
`stop.sh`:

```bash
AKERNEL_NODE_CONTAINER_NAME=akernel-rustdp-node \
AKERNEL_TRAEFIK_CONTAINER_NAME=akernel-rustdp-traefik \
AKERNEL_STANDALONE_DATA_DIR=/absolute/path/to/rustdp-data \
IMAGE=akernel-local:rust-data-plane ./start.sh
```

### Data Directory Location

By default, data is stored in `./data`. Override it without editing the script:
```bash
AKERNEL_STANDALONE_DATA_DIR="/path/to/your/data" ./start.sh
```
