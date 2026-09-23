# AKernel Deployment

AKernel ships in three deployment modes. Pick the one that matches your target.

| Mode | Target | Directory |
|------|--------|-----------|
| **Standalone** | Single machine (Docker / Pouch, no K8s) | [`standalone/`](./standalone/) |
| **Kubernetes (Helm)** | Existing K8s cluster | [`akernel/`](./akernel/) |
| **Multi-Cloud (Terraform)** | Alibaba Cloud ACK / Huawei Cloud CCE | [`terraform/`](./terraform/) |

## Guided deployment

The image installs the complete gVisor bundle pinned by sandboxd's runtime manifest: runsc, the containerd shim, and adjacent `gvisor-bin/` helpers. Its SHA-512 verifies the archive, not a standalone runsc binary. Keep these executables together when building custom images; replacing only runsc can leave mismatched checkpoint/restore helpers.

For a fresh cloud deployment, prefer the repository-level Makefile. It keeps
local deployment state under `.akernel/<env>/`, builds the all-in-one image,
plans/applies Terraform, creates the HTTPS and API key Secret, and reads the
bootstrap administrator API key for SDK access.

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

The node image downloads the pinned ADX linux/amd64 release directly from OBS,
verifies the archive SHA-256, and runs the release's own `install.sh`, which
verifies its internal manifest before installation. The runtime build downloads
the separately published `adx-execd` component archive and verifies its SHA-256.
AKernel then builds its own runtime rootfs with that binary; the prebuilt ADX
runtime image is not copied into the all-in-one image. When advancing ADX,
update `ADX_RELEASE_URL` and `ADX_RELEASE_SHA256` in `builder/node.Dockerfile`,
and `ADX_EXECD_URL` and `ADX_EXECD_SHA256` in `builder/runtime.Dockerfile`.
The published release is linux/amd64, so `make build` explicitly targets
`linux/amd64`; Mac ARM builds require Docker's amd64 emulation.

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
- `.akernel/default/grafana-admin-password`
- `.akernel/default/kubeconfig` after `make deploy`
- `.akernel/default/terraform.tfstate` after `make plan` / `make deploy`
- `.akernel/default/terraform.tfplan` after `make plan`
- `.akernel/default/token` and `.akernel/default/sdk.env` after `make print-env`

These files are intentionally ignored by Git because they can contain local
deployment state and secrets.

`VENDOR` selects the cloud vendor and defaults to `aliyun`. The guided flow
supports `aliyun` for ACK and `huaweicloud` for CCE. Both vendors store their
generated configuration, Terraform state, deployment secrets, and kubeconfig
under the same `.akernel/<env>/` layout. Cloud access credentials remain in the
process environment. Future AWS and GCP providers should follow this same
public entrypoint and local-state contract.

If `.akernel/default/` already exists, `make config` asks before overwriting
the generated config files. The ADX identity Secret is created during
`make deploy`; an existing Secret is retained so certificates and the
administrator API key remain stable across updates.

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

Read the deployed bootstrap administrator API key after `make deploy`:

```bash
make token
make print-env
```

`make token` reads `admin-key` from the `akernel-adx-tls` Secret through the
Terraform kubeconfig. `make print-env` writes mode-0600 `token` and `sdk.env`
files under the selected `.akernel/<env>/` directory and prints the SDK
exports. Treat both commands' output as secret material.

## 1. Standalone

Run a single-node AKernel using the scripts in [`standalone/`](./standalone/).
See [`standalone/README.md`](./standalone/README.md) for configuration and
start/stop instructions.

## 2. Kubernetes (Helm)

The umbrella chart in [`akernel/`](./akernel/) deploys AKernel's control
service, persistent state store, one worker Pod per eligible node, and the
public ingress. They use the same AKernel all-in-one image.

The `monitor` subchart remains optional and contains Prometheus, Grafana, Loki,
and Tempo. Kubernetes nodes must support privileged Pods and the runtime
requirements described earlier in this guide. Managed Redis requires a default
StorageClass or an explicit `core.adx.redis.persistence.storageClassName`.

### Create the HTTPS and API key Secret

Create the public HTTPS certificate and API key before installing the chart. The helper is
idempotent and leaves an existing Secret unchanged:

```bash
./deploy/scripts/ensure-adx-secret.sh \
  --namespace akernel \
  --name akernel-adx-tls
```

The Secret contains only `public.pem`, `public.key`, and `admin-key`. Internal
RPC and worker forwarding use network mode without component certificates;
keep these listeners within the deployment network. Node sessions, capsule
ownership, tenant permissions and user API keys are still checked.

### Read and rotate the administrator key

Terraform-managed deployments use `make token ENV=<env>` or
`make print-env ENV=<env>`. Each call reads the current Secret. For a directly
installed Helm chart, retrieve it with:

```bash
export AKERNEL_TOKEN="$(kubectl -n akernel get secret akernel-adx-tls -o jsonpath='{.data.admin-key}' | base64 -d)"
```

Generate a replacement without changing the public HTTPS certificate, then
restart the Coordinator Deployment so it reads the mounted `key_file` again:

```bash
python3 -c 'import json,secrets; print(json.dumps({"stringData":{"admin-key":secrets.token_hex(32)}}))' \
  | kubectl -n akernel patch secret akernel-adx-tls --type merge --patch-file /dev/stdin
kubectl -n akernel rollout restart deployment/akernel-adx-coordinator
kubectl -n akernel rollout status deployment/akernel-adx-coordinator
```

Read the new key again using the command above (or `make token`) and update SDK
processes. The updated ADX Coordinator atomically replaces its administrator key set
and persistently revokes removed keys. Tenant keys are preserved. Old keys
cannot be reused, even after restart. Existing ingress caches remain bounded by
the configured authentication TTL (10 seconds here); in-flight requests and
established streams are not canceled by rotation. Keep Redis data during rotation.
This requires an ADX release containing administrator reconciliation; #71 does
not contain it. The rollout procedure has not been validated on a live cluster.

### Install with managed Redis

Managed mode is the default. It creates one Redis StatefulSet with AOF enabled,
`appendfsync=everysec`, and a persistent volume. Helm generates an independent
64-character Redis password in `akernel-adx-redis-auth` and reuses it on upgrades
through a live Secret lookup. Use `helm install/upgrade` against the cluster;
offline `helm template` cannot recover an existing password. Coordinator, Ingress/API Server,
and node Pods receive the password through Secret references. Redis requires
authentication for Pod connections. An ingress NetworkPolicy also limits port 6379 to the
Coordinator, Ingress/API Server, and node Pods in the same namespace when the CNI enforces
NetworkPolicy. Redis is exposed only by a ClusterIP Service. Coordinator and Ingress/API Server
have independent readiness/liveness checks and independent `adxctl` supervisors.
Their generated configuration and logs are container-local under
`/home/akernel/adx/run/<role>` and reset on Pod replacement; the authoritative cluster
state remains in Redis. Node state and logs use the persistent
`/home/akernel/adx/run/node` subtree. During an upgrade from the retired
all-in-one control layout, the node init container removes only the obsolete
`/home/akernel/adx/run/control` subtree and root-level numeric
`/home/akernel/adx/run/config-<pid>` directories. Checkpoints, the degradation
journal, and the new `run/node` subtree are preserved.
A minimal values file is:

```yaml
core:
  image:
    repository: registry.example.com/akernel/all-in-one
    tag: "<release-tag>"
  adx:
    tls:
      existingSecret: akernel-adx-tls
    redis:
      persistence:
        storageClassName: fast-rwo
        size: 20Gi
```

Render first, then install:

```bash
helm dependency build ./akernel
helm template akernel ./akernel \
  --namespace akernel \
  -f my-values.yaml > /tmp/akernel-rendered.yaml

helm upgrade --install akernel ./akernel \
  --namespace akernel --create-namespace \
  -f my-values.yaml
```

### Install with external Redis

Store the complete Redis URL in a Kubernetes Secret. This keeps credentials out
of the rendered ConfigMap:

```bash
read -r -s -p "Redis URL: " ADX_REDIS_URL
printf '%s' "${ADX_REDIS_URL}" | kubectl -n akernel create secret generic akernel-adx-redis \
  --from-file=redis-url=/dev/stdin
unset ADX_REDIS_URL
```

Select external mode in the values file:

```yaml
core:
  adx:
    redis:
      mode: external
      external:
        existingSecret: akernel-adx-redis
        urlKey: redis-url
```

External mode omits the bundled Redis Service, StatefulSet, PVC, authentication
Secret and NetworkPolicy. The URL
must use the `redis://` scheme accepted by the current ADX release.

### Sandbox placement policy

The existing placement setting remains `spread` (default) or `binpack`:

```yaml
core:
  coordinator:
    schedulePlacementPolicy: spread
```

Changing this setting affects future placements.

### Public Traefik entrypoint

For a public Kubernetes deployment, enable Traefik's web entrypoints:

```yaml
core:
  traefik:
    enabled: true
    enableWebEntrypoint: true
    ports:
      websecure: 443
      web: 80
```

The public entrypoint exposes HTTPS on 443 and HTTP sandbox ports on 80.
Use the existing SDK configuration, or run `make print-env`:

```bash
export AKERNEL_SERVER_ADDRESS=<traefik-load-balancer-ip>
export AKERNEL_TOKEN="$(kubectl -n akernel get secret akernel-adx-tls \
  -o jsonpath='{.data.admin-key}' | base64 -d)"
```

Set `traefik.tls.enabled` only when supplying a custom default certificate for
the public entrypoint. The HTTPS certificate used between Traefik and Ingress
comes from `akernel-adx-tls`; this connection does not require client certificates.

### Verify the deployment

```bash
kubectl -n akernel rollout status statefulset/akernel-adx-redis  # managed mode
kubectl -n akernel rollout status deployment/akernel-adx-coordinator
kubectl -n akernel rollout status deployment/akernel-adx-ingress-api
kubectl -n akernel rollout status daemonset/akernel-node
kubectl -n akernel get pods -o wide
kubectl -n akernel logs deployment/akernel-adx-coordinator --tail=200
kubectl -n akernel logs deployment/akernel-adx-ingress-api --tail=200
kubectl -n akernel exec deployment/akernel-adx-coordinator -- \
  tail -n 200 /home/akernel/adx/run/coordinator/logs/coordinator.log
kubectl -n akernel exec deployment/akernel-adx-ingress-api -- \
  tail -n 200 /home/akernel/adx/run/ingress-api/logs/apiserver.log
```

There must be one ready `akernel-node` Pod for every eligible Kubernetes node.
The Coordinator and Ingress/API Server Deployments become ready independently. In external
Redis mode, verify that Redis is reachable from both deployments and every node
Pod before diagnosing ADX discovery. `kubectl logs` shows the `adxctl`
supervisor stream; component output is stored under each role's
`state_dir/logs`. The node-local Collector reads
`/home/akernel/adx/run/node/logs/*.log` when log export is configured.

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
separate ADX Coordinator and Ingress/API Server Deployments, one Adxlet DaemonSet,
managed Redis, Traefik `websecure:443` plus `web:80`, and Grafana exposed
through its own LoadBalancer when `install_monitor=true`. Set
`install_dragonfly=true` to install the pinned official Dragonfly chart and
inject its seed-client proxy into the node runtime configuration.

Terraform-managed Alibaba Cloud nodes also receive a dedicated 300 GiB XFS
disk mounted at `/home/akernel` by default. sandboxd consumes that native
filesystem directly for writable layers and local checkpoints, without a
loop-backed filestore. See the Aliyun guide for capacity, opt-out, and node
replacement details.

Only the AKernel all-in-one image is pushed to the registry selected by
`make config`. Managed Redis, Traefik, Grafana, Prometheus, Loki, Tempo, and
BusyBox use pinned public images by default. Set the per-component image
overrides when a private cluster requires mirrored third-party images.

## Directory Layout

```
deploy/
├── standalone/     # single-machine deployment
├── akernel/        # Helm umbrella chart (core + monitor subcharts)
├── terraform/      # multi-cloud provisioning (aliyun, huaweicloud, shared)
└── scripts/        # deployment and image helper scripts
```

### distill-fs release dependency

The all-in-one image downloads the static Linux/amd64 distill-fs release pinned in `builder/distill-fs-versions.env`. It does not compile `src/distill-fs`; that checkout is optional source reference. `make versions` reports the release tag and archive SHA-256. The AKernel installer at `builder/scripts/install-distill-fs.sh` checks the archive, package provenance, CLI version, and static ELF linkage, and retains licenses and provenance in `/usr/local/share/distill-fs`.

Publish and verify the distill-fs release before updating the AKernel version, URL, and checksum pin together. Missing or invalid pins stop `make build` before either image is built. There is no source-build fallback.

Validate installation against a downloaded candidate or release with `python3 builder/scripts/test-install-distill-fs.py /path/to/distill-fs-vX.Y.Z-linux-amd64.tar.gz` on Linux/amd64 with curl, jq, and binutils. This checks normal installation and rejects missing/invalid pins, corrupted archives, version/architecture mismatch, binary hash mismatch, and dynamically linked executables. The sandboxd pipeline and gitlink are independent of this dependency.

## Shared ChunkDB capacity

The node image's distill-fs v0.1.2 supports a configurable shared image-cache database capacity. The Linux default remains 100 GiB when omitted. Set `AKERNEL_CHUNK_DB_SIZE=64GiB` for standalone, `core.node.config.sandboxd.chunkDbSize=64GiB` in the umbrella Helm chart (`node.config.sandboxd.chunkDbSize` in the core chart), or `chunk_db_size = "64GiB"` in either cloud Terraform module. Guided profiles accept `make config CHUNK_DB_SIZE=64GiB`; this records the value in the generated Terraform configuration. Use a node image built with the matching sandboxd and distill-fs pins before enabling this option.

Whole bytes and integer `B`, `KiB`, `MiB`, `GiB`, or `TiB` values are supported. Sandboxd validates the minimum of 1 MiB, addressability, and host page alignment and supplies the same capacity to mounts, stats, GC, and recovered daemons. This is the LMDB map limit, not a memory reservation, a whole-cache disk quota, or a sandbox `storage_mb` limit.

Resizing an existing cache is unsupported. Same-Pod service restarts retain it; a replacement Pod with a changed hostname clears the image-manager root and creates a new cache with the configured capacity. Drain workloads before replacement. Standalone deployments must stop all users and select a fresh image-manager cache directory when changing capacity. Custom sandboxd configuration templates must retain the `# AKERNEL_CHUNK_DB_SIZE` marker when using the standalone or Helm override.

Internal network mode requires an ADX package containing the optional internal
transport implementation. The current #71 artifact pin predates it; validate
with the updated package before deploying these templates.
