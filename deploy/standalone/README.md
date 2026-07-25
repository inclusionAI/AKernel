# AKernel Standalone Deployment

This directory contains scripts and configurations for running AKernel in
standalone mode using Docker or Pouch, without Kubernetes. The deployment uses
two containers on the default container bridge:

- `akernel-node` runs the AKernel all-in-one image.
- `akernel-traefik` runs the official Traefik image as the external gateway.

Keeping the gateway in a separate network namespace allows sandboxd's normal
`PREROUTING` port-forwarding rules to handle traffic without standalone-only
NAT synchronization logic.

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
- Print the address to use as `AKERNEL_SERVER_ADDRESS`

On Linux, no host ports are published by default and the host accesses Traefik
through its Docker bridge IP. Docker Desktop on macOS cannot route directly to
that bridge IP, so `start.sh` automatically publishes ports 80 and 443 on
`127.0.0.1` and prints `AKERNEL_SERVER_ADDRESS=127.0.0.1`.

Docker Desktop on macOS also uses the `akernel-standalone-data` named volume
for `/home/akernel`. This avoids a VirtioFS limitation triggered by placing
individual configuration bind mounts beneath a parent host-directory bind
mount. Credentials and generated Traefik configuration remain in the ignored
host-side `data/` directory.

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
forwarding. On Linux, use the Traefik container IP printed by `start.sh`, or
retrieve it later:

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

On macOS, use the host address printed by the script:

```bash
export AKERNEL_SERVER_ADDRESS=127.0.0.1
export AKERNEL_TOKEN="$(cat data/token)"
```

Ports 80 and 443 must be available. Override the automatic behavior with
`AKERNEL_PUBLISH_HOST_PORTS=true|false`; change the bind address with
`AKERNEL_HOST_BIND_ADDRESS`. When host ports are enabled, keep them on ports 80
and 443 because the SDK intentionally uses the same hostname with HTTPS on 443
and HTTP on 80.

### Container Image Version

By default, `start.sh` uses the public Docker Hub image
`akerneldev/all-in-one:latest`. Override it with the `IMAGE` environment
variable to test another registry, tag, or locally built image:

```bash
IMAGE="<your-docker-registry>:<your-tag>" ./start.sh
```

For a native Apple Silicon development build from the repository root:

```bash
make build \
  PLATFORM=linux/arm64 \
  IMAGE_REPOSITORY=akernel-local/all-in-one \
  IMAGE_TAG=arm64
IMAGE=akernel-local/all-in-one:arm64 ./deploy/standalone/start.sh
```

The gateway defaults to `traefik:v3.6.8`. Override it independently when
needed:

```bash
TRAEFIK_IMAGE="traefik:v3.6.8" ./start.sh
```

### Data Directory Location

By default, credentials and gateway configuration are stored in `./data`.
Change the host-side directory with:

```bash
AKERNEL_DATA_DIR="/path/to/your/data" ./start.sh
```

Set `AKERNEL_DATA_VOLUME` to use a named container volume for AKernel runtime
state. macOS defaults to `akernel-standalone-data`; Linux defaults to the
host-side data directory. `stop.sh` removes the containers but deliberately
retains the named volume.

## Host Requirements

The node container is privileged and uses the host cgroup namespace plus a
read-write `/sys/fs/cgroup` mount. This supplies the permissions sandboxd needs
for cgroups, iptables, loop mounts, and gVisor. The Docker Linux VM or native
Linux host must provide cgroup v1; container privilege cannot substitute for
the required cgroup version or missing kernel filesystems.
