# AKernel Deployment

AKernel ships in three deployment modes. Pick the one that matches your target.

| Mode | Target | Directory |
|------|--------|-----------|
| **Standalone** | Single machine (Docker / Pouch, no K8s) | [`standalone/`](./standalone/) |
| **Kubernetes (Helm)** | Existing K8s cluster | [`akernel/`](./akernel/) |
| **Multi-Cloud (Terraform)** | Alibaba Cloud ACK / Huawei Cloud CCE | [`terraform/`](./terraform/) |

## Guided deployment

For a fresh cloud deployment, prefer the repository-level Makefile. It keeps
local deployment state under `.akernel/<env>/`, builds the all-in-one image,
plans/applies Terraform, and generates SDK JWT tokens from the local IAM seed.

```bash
make check
make config VENDOR=aliyun
make build
make push
make plan
make deploy
make print-env
make e2e
```

Kata Containers and Firecracker are enabled in the default AKernel image and
runtime configuration. Both require `/dev/kvm` to be available to the node
container. Nodes without a usable KVM device remain ready for runsc workloads
and do not advertise either VM runtime. If no eligible node advertises a
requested runtime, `Sandbox(runtime="kata")` or
`Sandbox(runtime="firecracker")` fails scheduling with a no-resource error.

The bundled Firecracker VMM and guest kernel are selected by sandboxd's shared
runtime manifest, and its guest-agent initrd is built from that same sandboxd
revision. AKernel also builds pinned virtiofsd 1.14.0 with its release lockfile
and enables read-only virtio-fs in standalone and Helm. OCI/Nydus image roots
use the directory provided by the image manager directly, without EROFS
conversion. The sandbox's writable layer remains a private ext4 image;
writable host sharing and OCI image mounts are unsupported. EROFS roots and
mounts remain supported. Set `AKERNEL_ENABLE_FIRECRACKER=false` while building
to exclude the VMM, kernel, virtiofsd, and initrd.

The default writable disk policy is `AsyncDirect` with `Writeback`. Hosts must
provide usable `io_uring` and filesystem alignment queries through
`statx(STATX_DIOALIGN)` (normally ext4/XFS on Linux 6.1 or newer). Capability
checks, rather than the kernel version alone, determine compatibility. There
is no automatic buffered fallback. For older hosts, explicitly configure
`writable_io_engine="Async"`, or `"Sync"` without io_uring, under
`[plugin.runtime.firecracker]` in the standalone config or Helm's
`node.config.sandboxd.config`. Keep `writable_cache_type="Writeback"`.

Drain sandboxes before replacing the Firecracker stack. Checkpoints record
VMM, kernel, initrd, and, when used, virtiofsd digests; mismatched stacks are
rejected on restore. Changing the writable I/O default does not convert the
engine saved in an existing checkpoint. For the full contract, see the
[sandbox runtime comparison](https://github.com/inclusionAI/sandboxd/blob/b8f4656c57432bce1b50111e766bcd21510ee482/doc/runtime.md).

The native Linux runc backend is opt-in because it uses the host kernel. For a
guided cloud profile, `make config ENABLE_RUNC=true` records both sides of the
selection: `make build` includes the checksum-pinned runc payload, and
Terraform registers the runtime with sandboxd. Direct Helm users must likewise
build with `AKERNEL_ENABLE_RUNC=true` and set
`node.config.sandboxd.enableRunc=true`.

The iptables sandbox NAT backend remains the default. Terraform deployments
can set `sandboxd_nat_backend = "bpfnat"` to use sandboxd's experimental
embedded TC eBPF backend on nodes without iptables NAT or conntrack modules.
The node must support TC eBPF and bpffs. bpfnat does not manage host firewall
policy, so custom host-network deployments must allow forwarding to and from
the sandbox bridge when their `FORWARD` policy is `DROP`.

### systemd container identity

The all-in-one image, Helm node template, and standalone launcher set
`container=oci` so PID 1 systemd recognizes the container and does not remount
shared host filesystems read-only during shutdown. Preserve this variable in
custom launchers. Applying the fix requires replacing the node Pod or
standalone container; it does not repair an already read-only filesystem.

### Network ACLs

The bundled standalone, Helm, and Terraform sandboxd configurations enable
per-sandbox network ACLs. Runsc, Kata, and Firecracker require the host `tun`
module and a usable `/dev/net/tun` for their pooled TAP endpoints. A sandbox
created without a policy remains on the unrestricted fast path. The default
`iptables` backend additionally requires the `iptables`, `ip6tables`, and
`ipset` userspace commands; IPv4/IPv6 filter-table, `br_netfilter`,
`xt_physdev`, conntrack and conntrack-netlink, connmark/CONNMARK, and
timeout-capable `hash:ip` ipset support; and both bridge netfilter sysctls for
iptables and ip6tables set to `1`. Sandboxd probes the IPv6 physdev rule and
the required ipset type when it initializes this backend. The optional `bpfnat`
backend instead requires Linux 5.17 or newer for `bpf_loop`, eBPF
`SCHED_CLS`, TC `clsact`,
supported hash and array maps, a writable bpffs at `/sys/fs/bpf` (or
permission to mount one), and permission to load BPF programs and manage TC
filters. Both backends require TCP and UDP port 53 on the sandbox bridge to be
free and at least one usable upstream nameserver. AKernel's privileged node
container prepares the selected backend's namespace-local settings. Host
provisioning must load the required kernel modules before the node pod starts;
the Terraform node bootstrap does this automatically.

Drain all sandboxes from a node before enabling ACLs or upgrading an existing
deployment to a release that enables them. Sandboxd deliberately refuses to
start ACL support when its store contains pre-ACL sandboxes, preventing a
silent fail-open migration. Start new sandboxes only after the upgraded
sandboxd is healthy.

Sandboxd selects the ACL implementation matching the configured `iptables` or
`bpfnat` NAT backend. DNS policies manage each sandbox's `/etc/resolv.conf`; a
caller mount that owns that path is rejected while ACL support is enabled.
Schema v2 domain traffic rules also use the managed DNS proxy to install
TTL-bound address grants, even when no separate DNS policy is supplied.

`make config` is interactive by default. It writes:

- `.akernel/default/config.env`
- `.akernel/default/terraform.tfvars`
- `.akernel/default/iam-seed`
- `.akernel/default/grafana-admin-password`
- `.akernel/default/kubeconfig` after `make deploy`
- `.akernel/default/terraform.tfstate` after `make plan` / `make deploy`
- `.akernel/default/terraform.tfplan` after `make plan`

These files are intentionally ignored by Git because they can contain local
deployment state and secrets.

`VENDOR` selects the cloud vendor and defaults to `aliyun`. The guided flow
supports `aliyun` for ACK and `huaweicloud` for CCE. Both vendors store their
generated configuration, Terraform state, deployment secrets, and kubeconfig
under the same `.akernel/<env>/` layout. Cloud access credentials remain in the
process environment. Future AWS and GCP providers should follow this same
public entrypoint and local-state contract.

If `.akernel/default/` already exists, `make config` asks before overwriting
the generated config files. The existing `iam-seed` is reused unless it is
deleted or explicitly replaced with `IAM_SEED_HEX`.

For agent or CI usage, provide values as Make variables and set
`NON_INTERACTIVE=1`. This is also the recommended path when you want to reuse an
already-pushed all-in-one image and skip `make build` / `make push`. Query the
target account first and set `REGION`, `ZONE_IDS`, and `VSWITCH_CIDRS`; the zone
and CIDR list lengths must match.

```bash
ENV_NAME=agent-e2e
make config \
  ENV="${ENV_NAME}" \
  VENDOR=aliyun \
  NON_INTERACTIVE=1 \
  REGION="${REGION}" \
  ZONE_IDS="${ZONE_IDS}" \
  VSWITCH_CIDRS="${VSWITCH_CIDRS}" \
  IMAGE_REPOSITORY=akerneldev/all-in-one \
  IMAGE_TAG=latest

make plan ENV="${ENV_NAME}"
# After reviewing the plan and approving the cloud changes:
make deploy ENV="${ENV_NAME}"
```

Inspect an existing profile before reusing its name. Use `FORCE=1` only after
explicitly approving replacement of its generated configuration.

Dragonfly is optional and disabled by default. Enable it in either interactive
configuration or non-interactive configuration with:

```bash
make config INSTALL_DRAGONFLY=true
```

The generated Terraform profile installs the pinned public Dragonfly chart and
configures `sandboxd` to use the seed-client HTTP proxy for registry-backed OCI
and Nydus downloads and as the optional object-storage proxy. The Aliyun module
creates dedicated seed and server node pools by default, so review their size
and disk settings in `.akernel/<env>/terraform.tfvars` before applying the
plan.

For multiple deployments, pass an explicit local profile name:

```bash
make config ENV=staging
make plan ENV=staging
make deploy ENV=staging
```

JWT tokens can be generated locally without exposing the IAM token API:

```bash
make token TTL=24h
make token TTL=100y
make token TTL=never
```

Long-lived tokens are useful for manual testing, but rotating the IAM seed is
currently the revocation mechanism for signed JWT tokens.

## 1. Standalone

Run a single-node AKernel using the scripts in [`standalone/`](./standalone/).
See [`standalone/README.md`](./standalone/README.md) for configuration and
start/stop instructions.

## 2. Kubernetes (Helm)

The umbrella chart in [`akernel/`](./akernel/) bundles two subcharts:

- **core** — scheduler + node-side components (etcd, master, frontend, node
  DaemonSet, and Edge ingress)
- **monitor** — observability stack (Prometheus, Grafana, Loki, Tempo)

```bash
# Render / inspect
helm template akernel ./akernel

# Install (provide your own values overrides)
helm install akernel ./akernel \
  --namespace akernel --create-namespace \
  -f my-values.yaml
```

Set image repositories, registry pull secrets, and storage classes in your own
values override file. See [`akernel/charts/core/values.yaml`](./akernel/charts/core/values.yaml)
and [`akernel/charts/monitor/values.yaml`](./akernel/charts/monitor/values.yaml)
for the full set of configurable values.

The bundled etcd StatefulSet is a durable single-member deployment. It keeps
fsync enabled and uses persistent storage by default. Production environments
that require etcd high availability should point AKernel at an externally
managed multi-member etcd cluster instead of increasing `etcd.replicas`.

The core chart defaults master, frontend, and node to the same all-in-one image:

```yaml
image:
  repository: registry.example.com/akernel/all-in-one
  tag: "<release-tag>"
```

Each component can still override `master.image`, `frontend.image`, or
`node.image` when a split-image deployment is required.

### Public Edge entrypoints

The core chart starts Edge alongside Frontend and Node Proxy alongside each
node by default. Edge serves API and exec WebSocket traffic over HTTPS/WSS on
443, and published sandbox ports and reverse tunnels over HTTP/WS on 80.

```bash
export AKERNEL_SERVER_ADDRESS=<edge-load-balancer-ip>
```

See [Edge and Node Proxy ingress](#edge-and-node-proxy-ingress) for certificate,
network allowlist, and existing LoadBalancer migration settings.

### IAM token signing seed

Master and frontend share `LITEBUS_DATA_KEY` from a Kubernetes Secret. A fresh
seed gives each deployment its own JWT signing key.

For regular `helm install` / `helm upgrade`, the chart creates the Secret when
it is missing and reuses it on later upgrades. For `helm template | kubectl
apply`, pre-create the Secret once so repeated renders do not rotate the seed:

```bash
./scripts/ensure-iam-secret.sh \
  --namespace akernel \
  --name akernel-master-secret

helm template akernel ./akernel \
  --namespace akernel \
  --set core.auth.existingSecret=akernel-master-secret \
  -f my-values.yaml \
  | kubectl apply -n akernel -f -
```

### Component TLS certificate

The all-in-one image does not contain a TLS private key. The core chart creates
one deployment-specific Secret and mounts the same certificate into master and
frontend Pods. This certificate protects the openYuanrong frontend and IAM
service connections and is also the default Edge ingress certificate. Set
`dataPlane.edge.tlsSecretName` to use a dedicated ingress certificate.

Regular `helm install` and `helm upgrade` reuse the existing Secret. For a
render-and-apply workflow, create it once before rendering so a new certificate
is not generated on every invocation:

```bash
./scripts/ensure-component-tls-secret.sh \
  --namespace akernel \
  --name akernel-component-tls

helm template akernel ./akernel \
  --namespace akernel \
  --set core.componentTLS.existingSecret=akernel-component-tls \
  -f my-values.yaml \
  | kubectl apply -n akernel -f -
```

## 3. Multi-Cloud (Terraform)

Provision a cluster and deploy AKernel in one flow on Alibaba Cloud (ACK) or
Huawei Cloud (CCE):

```bash
cd terraform/aliyun        # or terraform/huaweicloud

cp terraform.tfvars.example terraform.tfvars
vi terraform.tfvars        # set region, node pool, image repositories, etc.

terraform init
terraform plan
terraform apply
```

Per-vendor details are in
[`terraform/aliyun/README.md`](./terraform/aliyun/README.md) and
[`terraform/huaweicloud/README.md`](./terraform/huaweicloud/README.md).

The Alibaba Cloud Terraform defaults follow the recommended public layout:
frontend enabled, Edge HTTPS 443 plus HTTP 80, and Grafana exposed
through its own LoadBalancer when `install_monitor=true`. Set
`install_dragonfly=true` to install the pinned official Dragonfly chart and
inject its seed-client proxy into the node runtime configuration.

Terraform-managed Alibaba Cloud nodes also receive a dedicated 300 GiB XFS
disk mounted at `/home/akernel` by default. sandboxd consumes that native
filesystem directly for writable layers and local checkpoints, without a
loop-backed filestore. See the Aliyun guide for capacity, opt-out, and node
replacement details.

Only the AKernel all-in-one image is pushed to the registry selected by
`make config`. etcd, Grafana, Prometheus, Loki, Tempo, and BusyBox use
their pinned official public images by default. Set the per-component image
overrides when a private cluster requires mirrored third-party images.

## Directory Layout

```
deploy/
├── standalone/     # single-machine deployment
├── akernel/        # Helm umbrella chart (core + monitor subcharts)
├── terraform/      # multi-cloud provisioning (aliyun, huaweicloud, shared)
└── scripts/        # deployment and image helper scripts
```

## Edge and Node Proxy ingress

Node shutdown is supervised by systemd. The YuanRong bootstrap forwards the
stop signal to the Go CLI and waits for its ordered cleanup before exiting;
sandboxd stops after YuanRong. The YuanRong service uses `KillMode=mixed` so
the bootstrap controls the initial shutdown of its child processes.

The image must contain the YuanRong Edge and Node Proxy binaries and Go CLI
data-plane deployment support. The builder pins Core, data plane, RRT and the
sandbox SDK to `0.10.3rc1`. Core includes `config.sh`/`deploy.sh` and
FunctionProxy address registration; the matching data-plane wheel supplies
Edge, Node Proxy and Forward. Both data-plane components are enabled by
default. Edge uses the
component TLS certificate unless an existing Secret (`tls.crt` and `tls.key`)
is selected with `dataPlane.edge.tlsSecretName`. Configure the allowed client
and Edge source CIDRs for the deployment:

```yaml
dataPlane:
  enabled: true
  edge:
    tlsSecretName: akernel-edge-tls
    allowedClientCIDRs: "0.0.0.0/0"
    service:
      name: akernel-edge
      type: LoadBalancer
  nodeProxy:
    allowedEdgeCIDRs: "192.168.0.0/16"
    allowedTargetCIDRs: "" # Read sandboxd plugin.network.ip_range at node startup.
```

Node Proxy reads its default target range from the final sandboxd TOML, so changing
`plugin.network.ip_range` also updates the target ACL after restart (for example,
`10.88.0.1/16` becomes `10.88.0.0/16`). `allowedTargetCIDRs` remains an explicit
override. Missing or invalid address pools fail Node Proxy startup.

Terraform generates Edge source CIDRs from the managed CCE Pod/ENI network or
ACK Flannel/managed Terway vSwitch networks. Existing clusters and imported Pod
networks require `node_proxy_allowed_edge_cidrs`; direct Helm installs require
`allowedEdgeCIDRs`. If the CNI applies SNAT between Edge and Node Proxy, override
this with the source range actually seen by Node Proxy. Standalone defaults to
the shared container's node address and IPv4 loopback. These startup defaults
require an image built with the updated bootstrap script.

 Edge exposes HTTPS/WSS on
service port 443 and HTTP/WS on port 80; API and authenticated direct routes
use TLS. The local Frontend and IAM hops use HTTP with IAM validation enabled.
Node Proxy uses network security mode and only accepts connections from the
configured Edge CIDRs. Node readiness includes Node Proxy; nodes roll one at
a time. Frontend readiness includes Edge route readiness.

During migration, `dataPlane.edge.service.name` can match the existing ingress
Service name to retain its LoadBalancer identity and public address. Transfer
its annotations, clusterIP and loadBalancerIP as well. The Service then selects
Frontend/Edge pods; the Traefik Deployment and configuration are removed.
Existing SDK API and gateway address settings select TLS or plain WebSocket.
The `/internal-stats` endpoint reports the Edge Pod address and listener ports
for SDK `internal=True` URLs. Set `dataPlane.edge.grafanaURL` to the Grafana
HTTP service URL to expose its configured `/grafana` subpath through Edge.
See [operations dashboards](./akernel/charts/monitor/README.md) for the matching
Grafana public URL, metric collection settings and dashboard interpretation.

Cloud profiles use `edge_*` and `node_proxy_allowed_*` Terraform variables.
Migrate existing ingress Service annotations, name and IP to the corresponding
`edge_service_*` variables before planning an upgrade.

`make print-env` discovers the Service labeled `app.kubernetes.io/component=edge`.
Set `AKERNEL_GATEWAY_SERVICE` to select a specific gateway Service.

### distill-fs release dependency

The all-in-one image downloads the static Linux/amd64 distill-fs release pinned in `builder/distill-fs-versions.env`. It does not compile `src/distill-fs`; that checkout is optional source reference. `make versions` reports the release tag and archive SHA-256. The AKernel installer at `builder/scripts/install-distill-fs.sh` checks the archive, package provenance, CLI version, and static ELF linkage, and retains licenses and provenance in `/usr/local/share/distill-fs`.

Publish and verify the distill-fs release before updating the AKernel version, URL, and checksum pin together. Missing or invalid pins stop `make build` before either image is built. There is no source-build fallback.

Validate installation against a downloaded candidate or release with `python3 builder/scripts/test-install-distill-fs.py /path/to/distill-fs-vX.Y.Z-linux-amd64.tar.gz` on Linux/amd64 with curl, jq, and binutils. This checks normal installation and rejects missing/invalid pins, corrupted archives, version/architecture mismatch, binary hash mismatch, and dynamically linked executables. The sandboxd pipeline and gitlink are independent of this dependency.
