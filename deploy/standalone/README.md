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

The default runtime is gVisor `runsc`. `Sandbox(runtime="kata")` additionally requires `/dev/kvm` and hardware or nested virtualization on the Docker host. Nodes without KVM remain usable with runsc and do not advertise Kata to the scheduler.

Experimental NVIDIA GPU sandboxes use gVisor nvproxy. The host must provide a
compatible NVIDIA driver and NVIDIA Container Toolkit. Enable GPU access to
the node container with:

```bash
AKERNEL_ENABLE_GPU=true ./start.sh
```

The all-in-one image contains `nvidia-container-cli`, but not the host driver.
Use `AKERNEL_GPU_DEVICES` to override Docker's `--gpus` value when only a
device subset should be assigned.

Explicit sandbox storage quotas use the XFS filestore mounted at
`/home/akernel/xfs`. The standalone data directory is bind-mounted from the
host, and sandboxd creates `data/xfs.img` as a loop-backed XFS filesystem when
needed; quota-backed writable layers therefore use local disk rather than
tmpfs.

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
- Generate a deployment-specific IAM signing seed and a 24-hour SDK token
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
