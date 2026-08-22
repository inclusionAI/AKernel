# AKernel Standalone Deployment

This directory contains scripts and configurations for running AKernel in
standalone mode using Docker or Pouch, without Kubernetes. The deployment uses
two containers on the default container bridge:

- `akernel-node` runs the AKernel all-in-one image.
- `akernel-traefik` runs the official Traefik image as the external gateway.

Keeping the gateway in a separate network namespace allows sandboxd's normal
`PREROUTING` rules to handle gateway traffic. The all-in-one frontend sends
traffic from the node network namespace, so the standalone sandboxd config
also enables its local-output DNAT support.

The default runtime is gVisor `runsc`. The bundled image also contains Kata
Containers and Firecracker. Both `Sandbox(runtime="kata")` and
`Sandbox(runtime="firecracker")` require `/dev/kvm` plus hardware or nested
virtualization on the Docker host. Nodes without KVM remain usable with runsc
and do not advertise either VM runtime to the scheduler.

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
default iptables backend, `start.sh` loads `br_netfilter` on the host before
the node starts; the node then enables bridge netfilter in its own network
namespace. The host also requires conntrack plus connmark/CONNMARK support.
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
- Configure Traefik to poll FunctionMaster's HTTP provider for per-sandbox
  tunnel routes, including custom tunnel ports
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

### Data Directory Location

By default, data is stored in `./data`. To change this, edit `start.sh`:
```bash
DATA_DIR="/path/to/your/data"
```
