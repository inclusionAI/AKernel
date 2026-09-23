# AKernel Standalone Deployment

This directory contains scripts and configurations for running AKernel in
standalone mode using Docker or Pouch, without Kubernetes. One privileged `akernel-node` container serves the SDK on HTTPS port 443 and
public sandbox ports on HTTP port 80. Both are ready before startup returns.

The default runtime is gVisor `runsc`. The bundled image also contains Kata
Containers and Firecracker. Both `Sandbox(runtime="kata")` and
`Sandbox(runtime="firecracker")` require `/dev/kvm` plus hardware or nested
virtualization on the Docker host. Nodes without KVM remain usable with runsc
and do not advertise either VM runtime to the scheduler.

Firecracker enables read-only virtio-fs by default, so
`Sandbox(runtime="firecracker", image="ubuntu:24.04")` can use OCI or Nydus
roots directly. The image includes a pinned virtiofsd and matching VMM,
kernel, and guest agent. Its private writable disk uses `AsyncDirect` and
`Writeback`; the host must support io_uring and `STATX_DIOALIGN` on the
filestore filesystem. See the [deployment guide](../README.md) for older-host
configuration and checkpoint compatibility when upgrading the runtime stack.

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

Sandbox checkpoints for runsc and Firecracker are local to the node. Checkpoint state is kept under the persistent
`/home/akernel/adx/checkpoints` data mount. Workloads trigger an anonymous recovery
point through `POST /checkpoint` on `/run/akernel/rrt.sock`, and the SDK can
reload the same logical sandbox from the latest usable point. Recovery points
follow the source sandbox lifecycle; they are not exposed as reusable SDK
objects.

The currently pinned release does not yet contain workload checkpoint support. The source changes
were verified with an overlay validation image; see
[checkpoint validation](checkpoint-validation.md) for results and the remaining
release update.

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
├── start.sh                   # Start the AKernel container
├── stop.sh                    # Stop the AKernel container
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
- Publish HTTPS port `443` and HTTP port `80`
- Initialize and reuse local credentials automatically
- Generate a sandboxd config using `AKERNEL_NAT_BACKEND` (`iptables` by
  default)
- Wait until the node has allocatable capacity
- Print the SDK address and token path

The default listeners bind all host interfaces. Override the bind addresses or
ports before starting when the defaults conflict with another service:

```bash
AKERNEL_CONTROL_BIND=127.0.0.1 AKERNEL_CONTROL_PORT=8443 \
AKERNEL_DATA_BIND=127.0.0.1 AKERNEL_DATA_PORT=8080 \
AKERNEL_ENDPOINT_HOST=127.0.0.1 ./start.sh
```

`AKERNEL_ENDPOINT_HOST` controls the hostname printed for SDK configuration;
it does not change the Docker bind address.

### 4. Check Status

```bash
# View AKernel logs
sudo docker logs -f akernel-node

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

Use the existing SDK environment variables:

```bash
export AKERNEL_SERVER_ADDRESS="127.0.0.1"
export AKERNEL_TOKEN="$(cat data/token)"
```

Before starting the container, `start.sh` generates the API key once at
`data/adx/secrets/admin-key` (mode 0600) and makes `data/token` point to it.
An existing deployment token is retained. The service startup separately
initializes the public HTTPS certificate. Both are reused on restart. Internal components use network mode without mTLS;
these listeners remain within the standalone container network. SDK address
and token settings are unchanged.

This configuration requires the ADX internal-network-mode update; the current
#71 package pin predates that update. See [validation status](checkpoint-validation.md).
`data/token` points to the deployment token. Keep the data directory private.
For custom host port mappings, use the additional gateway override printed by
`start.sh`; the default deployment does not require it.

### Read and rotate the administrator key

Run these commands from `deploy/standalone/`:

```bash
cat data/token
export AKERNEL_TOKEN="$(cat data/token)"
```

To rotate the administrator key, keep the data directory and Redis data, stop
the deployment, replace the key atomically and restart with the same image:

```bash
./stop.sh
(umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > data/adx/secrets/.admin-key-new)
mv data/adx/secrets/.admin-key-new data/adx/secrets/admin-key
IMAGE="<your-current-image>" ./start.sh
export AKERNEL_TOKEN="$(cat data/token)"
```

Master reads `key_file` at startup and atomically reconciles the configured
administrator keys. Removed keys are revoked and cannot be reused; tenant keys
are preserved. Do not restore a revoked old key as a rollback. SDK processes
must reload `AKERNEL_TOKEN`. Existing ingress authentication caches can accept
the old key until their TTL expires (10 seconds in this deployment); requests
already in flight also remain subject to their RPC deadlines. Rotation does not
terminate already established streams.

For a staged transition, configure both old and new administrator key files in
Master's `bootstrap_credentials`, restart Master, update clients, then remove
the old entry and restart again. At least one valid administrator key must remain.
These rotation semantics require the updated ADX package; the #71 pin predates
the implementation.

### Container Image Version

By default, `start.sh` uses the public Docker Hub image
`akerneldev/all-in-one:latest`. Override it with the `IMAGE` environment
variable to test another registry, tag, or locally built image:
```bash
IMAGE="<your-docker-registry>:<your-tag>" ./start.sh
```

### Data Directory Location

By default, data is stored in `./data`. To change this, edit `start.sh`:
```bash
DATA_DIR="/path/to/your/data"
```
