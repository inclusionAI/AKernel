# AKernel Standalone Deployment

This directory contains scripts and configurations for running AKernel in
standalone mode using Docker or Pouch, without Kubernetes. One privileged `akernel-node` container serves the SDK on HTTPS port 443 and
public sandbox ports on HTTP port 80. Both are ready before startup returns.

## Quick start from source

Run these commands from the AKernel repository root. Install Git, GNU Make, Python 3.10 or newer, curl, and Docker with BuildKit first. Supported hosts are Linux with a native Linux/amd64 or Linux/arm64 Docker daemon, or an Apple Silicon Mac with OrbStack running a native Linux/arm64 daemon. The current user must be able to access the local Docker daemon directly. Linux also needs the kernel capabilities described under [network backend](#network-backend); loading host modules requires root or passwordless sudo.

```bash
git clone https://github.com/inclusionAI/AKernel.git
cd AKernel
make standalone
source .akernel/standalone/data/sdk-env.sh
```

`make standalone` checks the host and Docker daemon, initializes `src/sandboxd` if missing, verifies its revision against the parent gitlink, builds the current source, and starts that exact image through `start.sh`. It waits for the node and gateway health checks before printing the SDK address and environment-file path. It does not need a cloud profile or registry push.

The source entry point selects the Docker daemon's native architecture and uses the `execd` profile with runsc. Kata, Firecracker, runc, and GPU access are disabled unless explicitly enabled on a supported platform. It builds a unique local image tag and records source revision, image, and container identities in `source-state.json` under the data directory. Local AKernel edits are built and marked `.dirty` in the recorded revision. An initialized sandboxd checkout must be clean and match the gitlink; the helper does not fetch, switch, or overwrite it.

The default source data directory is `.akernel/standalone/data`, including the bootstrap API key, SDK token reference, generated service configuration, `source-state.json`, and `sdk-env.sh`. The environment file reads the token from its protected file when sourced; commands print credential paths rather than token values. The default local state directory is ignored by Git; keep custom data directories out of version control too.

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
# Release all sandboxes before stopping:
make standalone-stop
make standalone
source .akernel/standalone/data/sdk-env.sh
```

Source management records the node container's full identity, image and data
mount. Status reads the deployment key from its protected file and sends it
to the authenticated resource-directory endpoint through curl stdin; key values
stay out of command arguments and status output. It refuses occupied names,
replaced containers, and stopping while
sandboxd still lists sandboxes. Failed builds and starts retain their profile
for inspection and retry. Data and credentials survive stopping.

For an independent data profile, set an absolute `AKERNEL_STANDALONE_DATA_DIR`
for every command. Only one standalone node can run per Docker daemon.

### Runtime selection

Runsc is the source default. Opt into runc with
`make standalone AKERNEL_ENABLE_RUNC=true` and an approved
`AKERNEL_RUNC_RESOLV_CONF`. Linux/amd64 with usable `/dev/kvm` can also enable
`AKERNEL_ENABLE_KATA=true` or `AKERNEL_ENABLE_FIRECRACKER=true`.
Experimental runsc GPU access requires host NVIDIA drivers and Container
Toolkit, with `AKERNEL_ENABLE_GPU=true`.

Native ARM64 and Apple Silicon OrbStack builds require architecture-matched
`ADX_RELEASE_URL` / `ADX_RELEASE_SHA256` and `ADX_EXECD_URL` /
`ADX_EXECD_SHA256`; the default archive pins are AMD64. VM and GPU payloads
remain disabled on ARM64. The launcher verifies image architecture and probes
TUN/TAP, cgroups, iptables, FUSE and loop-backed ext4 before creating credentials.
See [build downloads](../../builder/README.md) for mirror and archive options.

### DNS resolver sources

With ACLs enabled, runsc, Kata and Firecracker use sandboxd's managed DNS
proxy. Runc uses direct DNS. `plugin.runtime.resolv_conf_path` configures the
managed upstream; `plugin.runtime.direct_resolv_conf_path` configures direct
DNS and otherwise inherits the upstream path. Existing explicit resolver
mounts retain precedence in direct mode.

When runc is enabled, startup copies an approved non-loopback resolver file
to `sandboxd/config/direct-resolv.conf`. Supply its absolute path through
`AKERNEL_RUNC_RESOLV_CONF`. Hosts with systemd-resolved or split DNS need an
explicit approved resolver; Docker's namespace-local `127.0.0.11` cannot serve
a direct sandbox namespace. Release sandboxes and restart to refresh this
snapshot, then verify DNS and certificate-validated HTTPS in a new sandbox.

### Network backend

Standalone uses iptables NAT by default. Linux requires the [deployment network capabilities](../README.md#network-acls); the launcher loads the required host modules and the node prepares namespace-local sysctls. OrbStack checks equivalent capabilities through its disposable container preflight.

Linux nodes without the required iptables NAT and conntrack support may select the experimental embedded TC eBPF backend with `AKERNEL_NAT_BACKEND=bpfnat`. It requires permission to load TC eBPF programs and mount or access writable bpffs. AKernel enables forwarding and disables global reverse-path filtering inside the node namespace for bpfnat local DNAT. It does not override host firewall policy; custom host-network deployments with `FORWARD=DROP` must allow bridge- and sandbox-CIDR-scoped traffic to and from `sandbox0`.

ADX receives the IPv4 address of the default-route interface so creation of `sandbox0` cannot change the advertised node address. Use `AKERNEL_NODE_IP` for an explicit multi-homed override. TCP and UDP port 53 on the sandbox bridge must be free for the managed DNS proxy.

Release all sandboxes and stop the old node before upgrading a data directory to an ACL-enabled image. Sandboxd refuses ACL initialization while pre-ACL sandbox records remain. See the [deployment network requirements](../README.md#network-acls) for the full capability contract.

### Storage and recovery

Explicit `storage_mb` quotas for runsc and Firecracker use the bounded ext4 filestore at `/home/akernel/filestore`. The selected host data directory is bind-mounted at `/home/akernel`; sandboxd creates a loop-backed filesystem image there when needed. Without an explicit quota, runsc retains its configured memory-backed overlay while Firecracker uses its configured sparse ext4 default. For shared image-cache capacity, see [ChunkDB configuration](../README.md#shared-chunkdb-capacity).

Runsc and Firecracker checkpoints use ADX local-only snapshots under `/home/akernel/adx/checkpoints`. Workloads trigger anonymous recovery points through `POST /checkpoint` on `/run/akernel/execd.sock`, and the SDK can reload the same logical sandbox from its latest usable point. Recovery points follow the sandbox lifecycle and are not reusable SDK objects.

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
docker exec -it akernel-node bash
docker exec akernel-node systemctl status
```

Use passwordless `sudo` when required for Docker access. For direct Pouch deployments, replace `docker` with `pouch`. Stop/removal failures return nonzero; inspect the reported failure before retrying. Stopping leaves the data directory and identity files intact.

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
├── manage.py                  # Source build, status and stop
├── start.sh                   # Start an existing AKernel image
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
- Write the SDK addresses to `data/sdk.env` (mode 0600), and print the token path

The default listeners bind all host interfaces. Override the bind addresses or
ports before starting when the defaults conflict with another service:

```bash
AKERNEL_CONTROL_BIND=127.0.0.1 AKERNEL_CONTROL_PORT=8443 \
AKERNEL_DATA_BIND=127.0.0.1 AKERNEL_DATA_PORT=8080 \
AKERNEL_ENDPOINT_HOST=127.0.0.1 ./start.sh
```

`AKERNEL_ENDPOINT_HOST` controls the hostname written and printed for SDK configuration;
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
source data/sdk.env
export AKERNEL_TOKEN="$(cat data/token)"
```

Before starting the container, `start.sh` generates the API key once at
`data/adx/secrets/admin-key` (mode 0600) and makes `data/token` point to it.
An existing deployment token is retained. The service startup separately
initializes the public HTTPS certificate. Both are reused on restart. Internal components use network mode without mTLS;
these listeners remain within the standalone container network. SDK address
and token settings are unchanged.

`data/sdk.env` follows the hostname and published ports selected at startup.
It exports the server address and an HTTPS gateway address on the same control
listener, including custom ports. Private port forwarding sends credentials;
the plaintext data listener rejects them. The standalone HTTP listener test uses
`AKERNEL_TEST_HTTP_DATA_PORT` (default `80`) when custom data ports are selected.
CI reads the same file. Source the
complete SDK environment rather than exporting only the server address.
`data/token` points to the deployment token. Keep the data directory private.

The bundled standalone Ingress accepts Host-subdomain port forwarding under
`localhost`: `<sandbox-id>-<port>.localhost`. The SDK continues to return its
existing path URL; a client may send that URL with the corresponding `Host`
header. This avoids relying on wildcard DNS during local tests. For a public
domain, set `ADX_DATA_PLANE_INGRESS_PORT_HOST_DOMAIN` in the Ingress `env` of
`builder/config/adx-standalone.yaml` before building, and configure matching
wildcard DNS and TLS for the public listener.

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


### Container Image Version

By default, `start.sh` uses the public Docker Hub image
`akerneldev/all-in-one:latest`. Override it with the `IMAGE` environment
variable to test another registry, tag, or locally built image:
```bash
IMAGE="<your-docker-registry>:<your-tag>" ./start.sh
```

### Data Directory Location

By default, direct startup stores data in `deploy/standalone/data`. Override it with:
```bash
export AKERNEL_STANDALONE_DATA_DIR="/path/to/your/data"
```

### AFS shared checkpoints

Use a separately managed AFS **DFS** mount for shared checkpoint artifacts.
The default standalone profile keeps checkpoints and runtime files on local
storage. To select AFS, mount DFS on the Linux Docker host first, then launch:

```bash
AKERNEL_AFS_MOUNT=/srv/afs-dfs \
AKERNEL_AFS_CHECKPOINT_PREFIX=akernel/checkpoints \
make standalone
```

The launcher requires a writable, existing FUSE mount with source `afs-dfs`;
an ordinary directory, OwnerFs mount, or read-only mount is rejected before
startup. Docker binds it at `/mnt/akernel-shared`. The ADX service checks the
mount again and configures the same `distributed_fs` alias and prefix for the
Adxlet checkpoint writer and Coordinator artifact reclaimer. Runtime rootfs,
writable layers, journals and local caches remain under the node's local state.
Keep the mount available throughout the node lifecycle. Bring up AFS Meta and
Node services independently; stopping AKernel does not stop those services.
Use durable local-file Meta storage and current AFS mTLS configuration. Existing
legacy AFS metadata formats require the upstream documented migration policy.

The ADX Sandbox Lifecycle dashboard (`adx-sandbox-lifecycle`) adds creation-call
success ratios, successful actual-new-create P95 latency, unknown outcomes,
queued/starting counts and wait ages, interruption/recovery results, worker
incidents, and statistics coverage/freshness. Its data source is the summary
Prometheus datasource; optional Environment detail collection uses the same
store. P95 excludes replay and unknown outcomes. Missing data, zero denominators and stale
snapshots are not evidence of health. The current-impact panel documents the
private Coordinator query boundary; it does not expose that administrative API
through the public SDK or Grafana.

All ADX dashboards scope queries by `cluster` and `adx_env`. Environment and
node identity joins, node table keys, and creation QPS recording rules retain
both labels. Loki indexes `cluster`; log and TraceQL queries and dashboard
navigation carry the same scope. TraceQL and Explore Requests match the selected cluster literally, including
cluster names containing dots, and retain an explicit All option. The Statistics Started At panel converts the Unix seconds metric to
milliseconds for Grafana date formatting. For Helm, set
`core.monitoring.clusterName` and `monitor.prometheusServer.config.clusterName`
to the same stable cluster identity (default `akernel`); Terraform supplies its
`cluster_name` to both. For standalone, set `AKERNEL_CLUSTER_NAME` and
`AKERNEL_ENV` before startup (defaults `standalone` and `default`). Use distinct
cluster identities when multiple clusters send to the same Prometheus.

The other ADX dashboards provide creation request/completion QPS and peak QPS,
matching Running/Paused counts, aggregated node/runtime resources, and separate
Environment detail and log pages. Helm monitoring can enable
`environmentDetails.enabled=true` to add native inventory and resource scrape
jobs to the existing Prometheus instance. Detail collection defaults to off,
uses a 30-second interval and caps each target at 200,000 samples per scrape.
Collected detail and summary series share `prometheusServer.config.retentionTime`
(default 30 days); allow for Environment churn when sizing its storage. Summary
scrapes retain their 15-second interval. Match `environmentDetails.namespaces` and
`environmentDetails.environment` to the deployment's namespace and
`core.monitoring.akernelEnv`. The log detail page defaults to a 15-minute,
manual-refresh query with bounded results. Per-instance OTLP samples from sandboxd
are excluded from the summary store; native ADX detail endpoints supply instance
CPU and memory. The existing `sandbox-metrics` dashboard URL now uses those
Environment detail panels on the same `prometheus` datasource. With detail
collection disabled, the pages remain available. A fresh deployment has no
detail samples; disabling collection later stops sampling while retained history
follows the shared retention policy.

Validate colliding Environment/node IDs across clusters and the coverage
clock using the provisioned dashboard queries and recording rules:

```bash
python deploy/akernel/charts/core/tests/test_adx_multicluster.py -v
python deploy/tests/observability/dashboard_query_contract.py --docker --output /tmp/adx-dashboard-queries
python deploy/tests/observability/dashboard_signal_contract.py --browser --output /tmp/adx-dashboard-signals
```

The signal contract requires Docker, Helm, headless Chrome, PyYAML and websockets. It opens the
provisioned dashboard in Grafana 11.5.2 and records the actual interpolated Tempo
queries for default All and a dotted cluster name.

For a standalone Collector, set `PROMETHEUS_ENDPOINT`, `LOKI_ENDPOINT`, and
`TEMPO_ENDPOINT` to reachable `host:port` addresses before startup; the launcher
passes supplied endpoint variables, `AKERNEL_ENV` and `AKERNEL_CLUSTER_NAME`
into the node container. Endpoints use `host:port` without a URL scheme; Tempo
uses its OTLP gRPC listener (4317).
Standalone collection includes Coordinator, API Server, Adxlet and relay summary
metrics on private listeners. API creation metrics use port 19093; Coordinator
autoscaling keeps port 19092. Collector self-metrics use port 18889.

Set `ENABLE_TRACE=true` to trace standalone control processes and Execd at a
10% sampling ratio through the local Collector. The default keeps tracing off.
Custom `AKERNEL_ADX_CONFIG` inputs may use explicit services or any supported
ADX profile, including node-only profiles. Configuration options apply only to
roles present in that deployment. Profile and service-role selectors support
ADX's `${NAME}` and `${NAME:-default}` syntax using the ADX service process
environment. Empty values use the default, and `$$` escapes a dollar sign.
Variable values are inserted once. The generated file retains source
expressions, including unrelated environment values, for the ADX loader.

The signal regression feeds ordinary Rust JSON logs through the shipped
Collector file receiver and normalization processors into Loki, then checks
WARN/ERROR counts for both clusters and All. Diagnostic counter regressions
include startup zeros, the first event and an initial burst through Prometheus.

The AMD64 default pins the AFS-enabled ADX release and Execd artifacts from
[Build 197](https://buildkite.com/agent-dx/agent-dx/builds/197), revision
`6b342cda054602dae315da8ff7a14c9e83f0a90b` from
[ADX #132](https://github.com/openJiuwen-ai/agent-dx/pull/132), based on master
`2ec519fea1b0cce22f3eefd3b207f84f2076e833`. Both archives
are SHA256-pinned in the builder Dockerfiles. ARM builds require their own
architecture-matched artifact overrides; this AMD64 candidate does not establish
ARM compatibility.
