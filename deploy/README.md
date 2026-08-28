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

The bundled Firecracker payload is selected by sandboxd's shared runtime
manifest. Set `AKERNEL_ENABLE_FIRECRACKER=false` while building to exclude it.
For supported operations and filesystem constraints, see the
[sandbox runtime comparison](https://github.com/inclusionAI/sandboxd/blob/1918fadb03b59bc6f540196e14b90a91bdf31b7d/doc/runtime.md).

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

### Network ACLs

The bundled standalone, Helm, and Terraform sandboxd configurations enable
per-sandbox network ACLs. Runsc, Kata, and Firecracker require the host `tun`
module and a usable `/dev/net/tun` for their pooled TAP endpoints. A sandbox
created without a policy remains on the unrestricted fast path. The default
`iptables` backend additionally requires `ip_tables`, `br_netfilter`,
conntrack, connmark/CONNMARK support, and
`net.bridge.bridge-nf-call-iptables=1`. The optional `bpfnat` backend instead
requires Linux eBPF `SCHED_CLS`, TC `clsact`, supported hash and array maps, a
writable bpffs at `/sys/fs/bpf` (or permission to mount one), and permission
to load BPF programs and manage TC filters. Both backends require TCP and UDP
port 53 on the sandbox bridge to be free and at least one usable upstream
nameserver. AKernel's privileged node container prepares the selected
backend's namespace-local settings. Host provisioning must load the required
kernel modules before the node pod starts; the Terraform node bootstrap does
this automatically.

Drain all sandboxes from a node before enabling ACLs or upgrading an existing
deployment to a release that enables them. Sandboxd deliberately refuses to
start ACL support when its store contains pre-ACL sandboxes, preventing a
silent fail-open migration. Start new sandboxes only after the upgraded
sandboxd is healthy.

Sandboxd selects the ACL implementation matching the configured `iptables` or
`bpfnat` NAT backend. DNS policies manage each sandbox's `/etc/resolv.conf`; a
caller mount that owns that path is rejected while ACL support is enabled.

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
  DaemonSet, and Traefik)
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

### Snapshot object storage

The core chart keeps the existing DataSystem snapshot backend by default. To
use S3-compatible object storage, create a Secret containing encrypted
credentials and configure the single S3 backend:

```yaml
core:
  snapshot:
    storage:
      backend: s3
      s3:
        provider: generic # generic, obs, or oss
        endpoint: minio.storage.svc:9000
        region: us-east-1
        bucket: akernel-snapshots
        existingSecret: akernel-snapshot-s3
        useHttps: false
        pathStyle: true
```

The Secret keys default to `access-key`, `secret-key`, and the optional
`security-token`. All providers share the same AWS Signature V4 client;
profiles do not select provider SDKs or restrict valid private endpoints and
CNAMEs. OSS requires virtual-hosted addressing. The removed `backend=obs` and
`snapshot_obs_*` configuration are not accepted. Remote snapshots larger than
5 GiB fail before upload because multipart CopyObject is not supported yet.

### Public Traefik entrypoints

For cloud deployments, use Traefik with two public entrypoints:

```yaml
traefik:
  enabled: true
  enableWebEntrypoint: true
  ports:
    websecure: 443
    web: 80
```

The `websecure` entrypoint serves the AKernel frontend API and exec websocket
over HTTPS/WSS. The `web` entrypoint serves function port-forwarding traffic
over plain HTTP/WS. With this layout the Python SDK only needs the LoadBalancer
host or IP:

```bash
export AKERNEL_SERVER_ADDRESS=<traefik-load-balancer-ip>
```

Do not set `traefik.tls.enabled` just to make port 443 work. The frontend
router is already configured as a TLS router; `traefik.tls.enabled` only mounts
a custom default certificate Secret. When it is `false`, Traefik uses its
default certificate.

The legacy single-entrypoint mode is still available by setting
`traefik.enableWebEntrypoint=false`. In that mode API, exec, and function
traffic share `traefik.ports.tcp`, so SDK clients should use an explicit port:

```bash
export AKERNEL_SERVER_ADDRESS=<traefik-load-balancer-ip>:<port>
```

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
service connections; it is separate from the certificate served by Traefik's
public `websecure` entrypoint.

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
frontend enabled, Traefik `websecure:443` plus `web:80`, and Grafana exposed
through its own LoadBalancer when `install_monitor=true`. Set
`install_dragonfly=true` to install the pinned official Dragonfly chart and
inject its seed-client proxy into the node runtime configuration.

Only the AKernel all-in-one image is pushed to the registry selected by
`make config`. etcd, Traefik, Grafana, Prometheus, Loki, Tempo, and BusyBox use
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
