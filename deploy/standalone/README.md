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

### Snapshot storage

Sandbox checkpoints for runsc and Firecracker use the embedded YuanRong
DataSystem by default. With no snapshot environment overrides, `start.sh`
passes `AKERNEL_SNAPSHOT_STORAGE_BACKEND=datasystem`; the S3-only storage mode,
provider, endpoint and credentials are not emitted.

Workloads trigger an anonymous recovery point through `POST /checkpoint` on
`/run/akernel/rrt.sock`, and the SDK can reload the same logical sandbox from
the latest usable point. Recovery points follow the source sandbox lifecycle;
they are not exposed as reusable SDK objects.

Select the unified S3-compatible backend when snapshots must live in object
storage. S3 is always an explicit distributed mode:

```bash
AKERNEL_SNAPSHOT_STORAGE_BACKEND=s3 \
AKERNEL_SNAPSHOT_STORAGE_MODE=distributed_cache \
AKERNEL_SNAPSHOT_S3_PROVIDER=generic \
AKERNEL_SNAPSHOT_S3_ENDPOINT=minio.example.internal:9000 \
AKERNEL_SNAPSHOT_S3_REGION=us-east-1 \
AKERNEL_SNAPSHOT_S3_BUCKET=akernel-snapshots \
AKERNEL_SNAPSHOT_S3_ACCESS_KEY='<encrypted-access-key>' \
AKERNEL_SNAPSHOT_S3_SECRET_KEY='<encrypted-secret-key>' \
AKERNEL_SNAPSHOT_S3_USE_HTTPS=false \
AKERNEL_SNAPSHOT_S3_PATH_STYLE=true \
./start.sh
```

`AKERNEL_SNAPSHOT_STORAGE_MODE` accepts only `distributed_cache` or
`distributed_only` for S3 and defaults to `distributed_cache`. In cache mode,
the object is authoritative after publication while the local checkpoint is a
bounded restore cache. In distributed-only mode, the local directory exists
only while capturing, publishing, materializing or pinned for restore.
AKernel deliberately rejects `local_only` with `backend=s3`; omit all S3
settings to keep the default embedded DataSystem behavior.

The provider is `generic`, `obs`, or `oss`; it selects validation and
addressing defaults while every provider uses the same AWS Signature V4 S3
protocol client. Private endpoints and CNAMEs are allowed. OSS requires
virtual-hosted addressing (`PATH_STYLE=false`). The optional
`AKERNEL_SNAPSHOT_S3_SECURITY_TOKEN` carries an encrypted temporary token.
`provider=obs` is not the removed OBS-native backend and does not migrate
`AKERNEL_SNAPSHOT_OBS_*` configuration or existing objects. Those removed
variables are not accepted.

The all-in-one image contains `/home/yuanrong/.akernel-s3-snapshot-capable`
only when its real openYuanRong package passes the build-time detector. The
detector requires all of the following evidence from the same package:

- process config parses, validates and exports the six non-secret S3 options
  plus credential environment;
- both FunctionSystem launch paths pass the six non-secret options without
  credential argv;
- the executable FunctionAgent contains the S3 flags, credential environment,
  5 GiB guard and publication postcondition contract.

The detector removes a stale marker before checking. If the marker is absent,
`AKERNEL_SNAPSHOT_STORAGE_BACKEND=s3` fails before producing YuanRong argv or
exporting credentials. Do not create the marker manually: it is package
capability evidence, not a user feature flag. Upgrade the all-in-one image as a
unit before enabling S3, and test rollback by confirming an old or incomplete
package removes the marker.

For standalone S3, `start.sh` validates every value for CR/LF, writes all
snapshot environment to a mode-`0600` temporary env-file under
`${TMPDIR:-/tmp}`, and supplies Docker/Pouch with `--env-file`. The file is
removed by an EXIT/signal trap after container creation or on failure. This
keeps AK/SK/token out of the host process list and generated command output;
they remain visible to the container engine, host root and the FunctionAgent
process, which are therefore part of the credential trust boundary.

`start.sh` uses the container engine directly when the caller has access. If
that probe fails, it uses only passwordless `sudo -n`; the same root-owned
engine reads the `0600` env-file by path. Interactive sudo is not attempted.
Failure to read the file, start the container or validate the S3 capability
causes startup to fail, and the cleanup trap still removes the file.

Remote snapshots larger than 5 GiB are rejected before upload because this
version does not implement multipart CopyObject. `/home/akernel/checkpoints`
is the node's local staging directory; SDK checkpoint records have no automatic
TTL and remain until `Sandbox.delete_checkpoint()` is called. A restored
sandbox is a new sandbox and receives fresh network routes.

One node configures one remote snapshot backend. Snapshot records freeze their
backend, and restore rejects a record whose backend does not match the current
FunctionAgent. Before switching from DataSystem to S3 or rolling back, stop new
checkpoint creation, wait for in-flight publish/restore/delete operations, and
restore or delete records that belong to the old backend. Do not remove the S3
Secret or bucket while S3-backed records remain. There is no implicit dual-read,
cross-provider copy or destination atomic CAS.

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
