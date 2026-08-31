# Aliyun One-Click Deployment (ACK + Helm)

This Terraform module deploys AKernel on Alibaba Cloud ACK. It can either:
- Create a new ACK cluster and node pool, then install Helm charts, or
- Reuse an existing Kubernetes cluster via kubeconfig.

It installs:
- `akernel-core` to `core_namespace`
- Optional `akernel-monitor` to `monitor_namespace` (controlled by `install_monitor`)
- Optional Dragonfly to `dragonfly_namespace` (controlled by `install_dragonfly`)
- Optional prereq `openkruise` to `prereq_namespace` (controlled by `install_prereqs`)

## Prerequisites
- Terraform >= 1.5
- Alibaba Cloud account permissions for VPC/VSwitch/ACK/RAM resources
- Aliyun credentials available to Terraform provider (for example via environment variables):

```bash
export ALICLOUD_ACCESS_KEY="<your-access-key-id>"
export ALICLOUD_SECRET_KEY="<your-access-key-secret>"
export ALICLOUD_REGION="cn-hangzhou"
```

## Quick Start (Create ACK + Install Helm)

The repository-level Makefile wraps this Terraform module and is the preferred
path for normal deployments:

```bash
make config VENDOR=aliyun
make plan
make deploy
make print-env
```

The helper keeps generated local files under `.akernel/default/`, including
`terraform.tfvars`, `iam-seed`, `terraform.tfstate`, `terraform.tfplan`, and
the generated kubeconfig. Pass `ENV=<name>` for multiple independent deployment
profiles.

For non-interactive agent usage:

```bash
make config \
  VENDOR=aliyun \
  NON_INTERACTIVE=1 \
  FORCE=1 \
  REGION=cn-hangzhou \
  ZONE_IDS=cn-hangzhou-j,cn-hangzhou-j,cn-hangzhou-j \
  NODE_POOL_KEY_NAME=<your-ecs-key-name> \
  IMAGE_REPOSITORY=registry.example.com/akernel/all-in-one \
  IMAGE_TAG=<existing-image-tag>
```

If you want to run Terraform directly:

```bash
cd deploy/terraform/aliyun
terraform init
terraform apply \
  -var 'region=cn-hangzhou' \
  -var 'zone_ids=["cn-hangzhou-i","cn-hangzhou-j","cn-hangzhou-k"]' \
  -var 'kubeconfig_output_path=/abs/path/to/generated/kubeconfig' \
  -var 'node_pool_key_name=<your-ssh-key-name>'
```

If you prefer login password instead of SSH key:

```bash
terraform apply \
  -var 'region=cn-hangzhou' \
  -var 'zone_ids=["cn-hangzhou-i","cn-hangzhou-j","cn-hangzhou-k"]' \
  -var 'node_pool_login_password=<your-strong-password>' \
  -var 'node_pool_key_name='
```

## Use Existing Cluster
When `create_cluster=false`, this module skips ACK/VPC creation and installs Helm releases to an existing cluster.

```bash
cd deploy/terraform/aliyun
terraform init
terraform apply \
  -var 'create_cluster=false' \
  -var 'region=cn-hangzhou' \
  -var 'zone_ids=["cn-hangzhou-i","cn-hangzhou-j","cn-hangzhou-k"]' \
  -var 'kubeconfig_path=/abs/path/to/kubeconfig'
```

## Common Options
Enable ACK API server public endpoint:

```bash
terraform apply -var 'api_server_public_access=true'
```

Install OpenKruise before AKernel:

```bash
terraform apply \
  -var 'install_prereqs=true' \
  -var 'prereq_namespace=kruise-system' \
  -var 'prereq_kruise_chart_version=1.8.3'
```

Install monitor chart:

```bash
terraform apply \
  -var 'install_monitor=true' \
  -var 'grafana_public_access=true' \
  -var 'monitor_storage_class=alicloud-disk-essd'
```

## AKernel node storage

New Terraform-managed ACK node pools attach two data disks by default. The
first remains available to ACK for the container runtime. The second is a
dedicated 300 GiB ESSD that ACK formats as XFS and mounts at
`/home/akernel` before the node joins the cluster. The same storage layout is
applied to user-defined `extra_node_pools`; dedicated Dragonfly pools retain
their own storage configuration.

AKernel stores both `/home/akernel/filestore` and
`/home/akernel/checkpoints` on that native XFS filesystem. The generated
sandboxd configuration intentionally leaves `filestore_dir_size` unset, so
sandboxd uses the directory directly instead of creating a loop-backed ext4
or XFS filesystem. Keeping the writable layer and checkpoint artifacts on
the same reflink-capable filesystem enables the fast Firecracker checkpoint
and restore path.

Change the dedicated disk capacity or category with
`node_pool_extra_data_disk_size` and
`node_pool_extra_data_disk_category`. Set
`node_pool_extra_data_disk_enabled=false` to opt out, for example when an
existing cluster already provisions `/home/akernel` itself. The option has no
effect when `create_cluster=false`; existing-cluster users must mount a
suitable host filesystem before deploying the chart. An explicit ext4
override remains supported for compatibility, but it cannot provide the XFS
reflink checkpoint path.

Changing these settings does not migrate live sandbox data on existing
nodes. Before replacing an existing node pool, drain its sandboxes and remove
or relocate lifecycle-bound checkpoints. Review the Terraform plan as the
default dedicated disk adds one cloud disk per AKernel node.

After provisioning a node, verify the effective layout with:

```bash
findmnt -no SOURCE,FSTYPE,TARGET /home/akernel
xfs_info /home/akernel | grep 'reflink=1'
```

Install Dragonfly P2P distribution:

```bash
terraform apply \
  -var 'install_dragonfly=true'
```

The module pins the public `dragonfly/dragonfly` chart at version `1.6.21`,
creates dedicated seed and server node pools by default, and injects
`http://dragonfly-seed-client.<namespace>.svc.cluster.local:4001` into the
AKernel node configuration. Set `oss_proxy_url` to override that address.
Review `dragonfly_seed_node_pool`, `dragonfly_server_node_pool`, storage, and
image registry settings before applying the plan.

## Public endpoint model

The default cloud deployment uses a split frontend plus a two-entrypoint
Traefik LoadBalancer:

- `websecure:443` routes frontend API and exec websocket traffic over TLS.
- `web:80` routes function port-forwarding traffic over plain HTTP/WS.

Use the Traefik LoadBalancer host or IP directly with the SDK:

```bash
export AKERNEL_SERVER_ADDRESS=<traefik-load-balancer-ip>
```

`traefik_tls_enabled` is only for mounting a custom default certificate. It is
not required for the `websecure` router on port 443; Traefik serves its default
certificate when the variable is `false`.

To use the legacy single-entrypoint mode, set
`traefik_enable_web_entrypoint=false` and configure `traefik_tcp_port`. In that
mode SDK clients must include the port explicitly:

```bash
export AKERNEL_SERVER_ADDRESS=<traefik-load-balancer-ip>:<port>
```

Enable OSS auth injection for AKernel node secret:

```bash
terraform apply \
  -var 'oss_endpoint=oss-cn-hangzhou.aliyuncs.com' \
  -var 'oss_access_key_id=<oss-ak>' \
  -var 'oss_access_key_secret=<oss-sk>' \
  -var 'oss_bucket=<bucket-name>'
```

## Key Variable Notes
- `zone_ids` is required and should match `vswitch_cidrs` length when `create_cluster=true`.
- `create_cluster=true` requires at least 3 `vswitch_cidrs/zone_ids` (zones can repeat).
- `node_pool_key_name` and `node_pool_login_password` are mutually exclusive. Set only one.
- If neither SSH key nor login password is set, Terraform passes `password = null` to node pool creation.
- `kubeconfig_output_path` controls where the generated kubeconfig is written when `create_cluster=true`.
- `iam_litebus_data_key` passes a stable JWT signing seed to the AKernel core chart. Set it when you want to generate SDK tokens locally from the same seed.
- `master_public_access_8888` controls whether akernel-master is exposed via LoadBalancer; when `false` the service is kept as `ClusterIP`.
- `oss_auths` and `registry_auths` accept strongly typed credential maps; keep
  their generated `terraform.tfvars` private.
- `dragonfly_chart_repository` and `dragonfly_chart_version` select the
  public pinned chart; override the repository when using a private mirror.

## Outputs and Verification
Get kubeconfig path:

```bash
terraform output -raw kubeconfig_path
```

Basic checks:

```bash
KUBECONFIG=$(terraform output -raw kubeconfig_path)
kubectl --kubeconfig "${KUBECONFIG}" get ns
kubectl --kubeconfig "${KUBECONFIG}" -n akernel get pods
kubectl --kubeconfig "${KUBECONFIG}" -n akernel-monitor get pods
```

## Uninstall
```bash
cd deploy/terraform/aliyun
terraform destroy
```
