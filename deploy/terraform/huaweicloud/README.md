# Huawei Cloud Deployment (CCE + Helm)

This Terraform module creates a Huawei Cloud CCE cluster and installs the
AKernel core and optional monitor charts. It follows the same deployment
contract as the Aliyun module: one all-in-one AKernel image, an Agent DX
identity Secret, managed Redis, a direct ADX public Service, optional public
Grafana, and local state under `.akernel/<env>/`.

## Control pool

See [control and compute placement](../../README.md#control-and-compute-placement)
for control-pool defaults, existing-pool reuse and migration requirements.

CCE control nodes keep one base container-runtime data volume, using
`node_pool_data_volume_type` and `node_pool_data_volume_size` (SSD / 300 GiB by
default). They do not attach the sandbox-specific extra volume or run compute
node bootstrap scripts.

The module requires Huawei Cloud provider 1.83.0 or later for its data sources.
With Terraform 1.13.4 (as used in CI), validate the full resource schema and run
plans with all providers mocked. Initialization downloads provider binaries;
these tests do not create cloud resources. CI checks providers 1.83.0 and 1.99.1:

```bash
terraform -chdir=deploy/terraform/huaweicloud init -backend=false
terraform -chdir=deploy/terraform/huaweicloud validate
terraform -chdir=deploy/terraform/huaweicloud test
```

## Pod PID budget

[CCE documents `pod-pids-limit=-1` by default](https://support.huaweicloud.com/intl/en-us/usermanual-cce/cce_10_0652.html),
so no ACK-specific override is applied. For customized pools, verify actual
Pod and ancestor PID limits and adjust via CCE node pool configuration if
needed. The per-sandbox limit remains 4096.

The default AKernel node pool additionally persists `kernel.pid_max=4194304`,
raises `kernel.threads-max` to at least `4194304`, and removes implicit systemd
limits on containerd/Docker scopes. Extra and Dragonfly pools are unchanged.
Existing CCE pools ignore `postinstall` updates: migrate existing hosts and
future-node initialization separately after review, and restart kubelet if
its node-wide PID capacity is stale. Verify all Pod ancestor limits; a high
kernel ceiling alone does not remove a smaller container or Pod limit.

## Prerequisites

- Terraform 1.5 or later
- Docker, Helm, kubectl, and Python 3
- Huawei Cloud permissions for VPC, CCE, ECS, ELB, EIP, and NAT resources
- An AKernel all-in-one image pushed to a registry reachable by CCE nodes

Export Huawei Cloud credentials before planning or deploying:

```bash
export HW_ACCESS_KEY="<your-access-key-id>"
export HW_SECRET_KEY="<your-secret-access-key>"
export HW_REGION_NAME="cn-north-4"
```

## Guided deployment

Run the repository-level workflow from the AKernel root:

```bash
make config VENDOR=huaweicloud
make plan VENDOR=huaweicloud
make deploy VENDOR=huaweicloud
make token VENDOR=huaweicloud
make print-env VENDOR=huaweicloud
```

The generated profile contains Terraform variables, state, kubeconfig, and the
Grafana administrator password under `.akernel/default/`. After deployment,
`make print-env` stores mode-0600 SDK credential files there. Use `ENV=<name>`
for an independent deployment profile.

For non-interactive agent usage, provide all values explicitly:

```bash
make config \
  VENDOR=huaweicloud \
  NON_INTERACTIVE=1 \
  FORCE=1 \
  REGION=cn-north-4 \
  AVAILABILITY_ZONE=cn-north-4a \
  NODE_POOL_KEY_NAME=<your-ecs-key-pair> \
  IMAGE_REPOSITORY=swr.cn-north-4.myhuaweicloud.com/akernel/all-in-one \
  IMAGE_TAG=<existing-image-tag>
```

Set `NODE_POOL_LOGIN_PASSWORD` instead of `NODE_POOL_KEY_NAME` when password
login is required. The two settings are mutually exclusive.

## Network model

The generated profile enables the public CCE API endpoint and node-subnet SNAT
so Terraform can reach the cluster and worker nodes can pull public images.
ADX is exposed directly through a public ELB with two ports:

- HTTPS 443 forwards API, command/file and PTY traffic to ADX control port 8443.
- HTTP 80 forwards sandbox-port traffic to ADX data port 8080.

The generated ADX Secret provides the HTTPS certificate and API key.
`adx_public_service_enabled` controls public Service creation;
`adx_public_access` controls public ELB auto-provisioning. Configure an existing
load balancer with `adx_service_annotations` / `adx_load_balancer_ip` when required.
`adx_control_port` and `adx_data_port` select distinct external ports.

```bash
export AKERNEL_SERVER_ADDRESS=<adx-elb-address>
```

For custom ports, `make print-env VENDOR=huaweicloud` discovers the Service
ports and writes the corresponding gateway override.

Grafana uses a separate public ELB when monitoring and public Grafana access
are enabled. Its generated administrator password is stored at
`.akernel/<env>/grafana-admin-password`.

## Images

ADX control and node workloads use the same configured AKernel all-in-one
image. Managed Redis, Grafana, Prometheus, Loki, Tempo, and BusyBox
use pinned public images by default. Use the component image variables or
`monitor_image_registry` only when the cluster requires private mirrors.

## Optional components

Enable monitoring or Dragonfly during profile generation:

```bash
make config \
  VENDOR=huaweicloud \
  INSTALL_MONITOR=true \
  INSTALL_DRAGONFLY=true
```

Dragonfly is disabled by default. When enabled, the module installs the pinned
official chart, creates dedicated node pools, and injects its seed-client proxy
into the AKernel node configuration.

OpenKruise is not required by the default AKernel charts. The
`install_prereqs` Terraform option remains available only for deployments that
add their own Kruise-based overrides.

## Direct Terraform usage

The Makefile workflow is preferred because it keeps generated files outside
the module directory. Advanced users can instead copy
`terraform.tfvars.example` and provide an
external state path explicitly:

```bash
terraform -chdir=deploy/terraform/huaweicloud init
terraform -chdir=deploy/terraform/huaweicloud apply \
  -state="$(pwd)/.akernel/default/terraform.tfstate" \
  -var-file="$(pwd)/deploy/terraform/huaweicloud/terraform.tfvars"
```

The module can reuse an existing cluster by setting `create_cluster=false` and
providing `kubeconfig_path`. Environment-specific chart overrides can be added
with `core_values_override_files` and `monitor_values_override_files`.

## Verification and cleanup

```bash
kubectl --kubeconfig .akernel/default/kubeconfig -n akernel get pods

make destroy VENDOR=huaweicloud
```

The monitor namespace defaults to `akernel-monitor`. If CCE is still
initializing during the first apply, wait for the cluster and node pool to
become ready, then rerun `make deploy VENDOR=huaweicloud`.
