# Huawei Cloud Deployment (CCE + Helm)

This Terraform module creates a Huawei Cloud CCE cluster and installs the
AKernel core and optional monitor charts. It follows the same deployment
contract as the Aliyun module: one all-in-one AKernel image, a generated IAM
seed, dual-entrypoint Traefik, optional public Grafana, and local state under
`.akernel/<env>/`.

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
make token VENDOR=huaweicloud TTL=24h
make print-env VENDOR=huaweicloud
```

The generated profile contains Terraform variables, state, kubeconfig, IAM
seed, and Grafana administrator password under `.akernel/default/`. Use
`ENV=<name>` for an independent deployment profile.

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
Traefik is exposed through a public ELB with two entrypoints:

- `websecure:443` serves the authenticated frontend API and exec websocket.
- `web:80` serves sandbox port-forwarding traffic.

The SDK therefore needs only the Traefik ELB address:

```bash
export AKERNEL_SERVER_ADDRESS=<traefik-elb-address>
```

Grafana uses a separate public ELB when monitoring and public Grafana access
are enabled. Its generated administrator password is stored at
`.akernel/<env>/grafana-admin-password`.

## Images

Master, frontend, and node use the same configured AKernel all-in-one image.
etcd uses the official `gcr.io/etcd-development/etcd:v3.6.8` image. Its volume
permissions init container and Traefik internal-stats sidecar use pinned
BusyBox. The guided make flow derives sibling etcd and BusyBox mirror
repositories from the all-in-one image repository; direct Terraform users can
set the component image variables or `monitor_image_registry` explicitly.

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
`terraform.tfvars.example`, set a unique `iam_litebus_data_key`, and provide an
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
