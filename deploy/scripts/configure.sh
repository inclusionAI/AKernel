#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/deploy/scripts/common.sh"

vendor="aliyun"
env_name="default"
non_interactive=0
force=0
region_override=""
cluster_name_override=""
zone_ids_override=""
availability_zone_override=""
vswitch_cidrs_override=""
node_pool_size_override=""
node_pool_instance_types_override=""
node_flavor_id_override=""
node_pool_key_name_override=""
node_pool_login_password_override=""
acr_namespace_override=""
monitor_storage_class_override=""
image_repository_override=""
image_tag_override=""
install_monitor_override=""
install_dragonfly_override=""
enable_runc_override=""
grafana_public_access_override=""
grafana_admin_password_override=""
iam_seed_hex_override=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vendor)
      vendor="$2"
      shift 2
      ;;
    --env)
      env_name="$2"
      shift 2
      ;;
    --non-interactive)
      non_interactive=1
      shift
      ;;
    --force)
      force=1
      shift
      ;;
    --region)
      region_override="$2"
      shift 2
      ;;
    --cluster-name)
      cluster_name_override="$2"
      shift 2
      ;;
    --zone-ids)
      zone_ids_override="$2"
      shift 2
      ;;
    --availability-zone)
      availability_zone_override="$2"
      shift 2
      ;;
    --vswitch-cidrs)
      vswitch_cidrs_override="$2"
      shift 2
      ;;
    --node-pool-size)
      node_pool_size_override="$2"
      shift 2
      ;;
    --node-pool-instance-types)
      node_pool_instance_types_override="$2"
      shift 2
      ;;
    --node-flavor-id)
      node_flavor_id_override="$2"
      shift 2
      ;;
    --node-pool-key-name)
      node_pool_key_name_override="$2"
      shift 2
      ;;
    --node-pool-login-password)
      node_pool_login_password_override="$2"
      shift 2
      ;;
    --acr-namespace)
      acr_namespace_override="$2"
      shift 2
      ;;
    --monitor-storage-class)
      monitor_storage_class_override="$2"
      shift 2
      ;;
    --image | --image-repository)
      image_repository_override="$2"
      shift 2
      ;;
    --tag | --image-tag)
      image_tag_override="$2"
      shift 2
      ;;
    --install-monitor)
      install_monitor_override="$2"
      shift 2
      ;;
    --install-dragonfly)
      install_dragonfly_override="$2"
      shift 2
      ;;
    --enable-runc)
      enable_runc_override="$2"
      shift 2
      ;;
    --grafana-public-access)
      grafana_public_access_override="$2"
      shift 2
      ;;
    --grafana-admin-password)
      grafana_admin_password_override="$2"
      shift 2
      ;;
    --iam-seed-hex)
      iam_seed_hex_override="$2"
      shift 2
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

vendor="$(normalize_vendor "${vendor}")"
require_cmd python3 terraform

prompt() {
  local var_name="$1"
  local label="$2"
  local default_value="$3"
  local value
  if [[ "${non_interactive}" -eq 1 ]]; then
    printf -v "${var_name}" '%s' "${default_value}"
    return
  fi
  read -r -p "${label} [${default_value}]: " value
  printf -v "${var_name}" '%s' "${value:-${default_value}}"
}

set_or_prompt() {
  local var_name="$1"
  local label="$2"
  local default_value="$3"
  local override_value="$4"
  if [[ -n "${override_value}" ]]; then
    printf -v "${var_name}" '%s' "${override_value}"
  else
    prompt "${var_name}" "${label}" "${default_value}"
  fi
}

normalize_bool() {
  local raw
  raw="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "${raw}" in
    true | t | yes | y | 1)
      printf 'true'
      ;;
    false | f | no | n | 0)
      printf 'false'
      ;;
    *)
      die "invalid boolean value: $1"
      ;;
  esac
}

default_tag="$(git -C "${AKERNEL_REPO_ROOT}" rev-parse --short HEAD)-$(date +%Y%m%d%H%M%S)"
default_cluster_name="akernel"
if [[ "${env_name}" != "default" ]]; then
  default_cluster_name="akernel-${env_name}"
fi

case "${vendor}" in
  aliyun)
    set_or_prompt region "Alibaba Cloud region" "cn-hangzhou" "${region_override}"
    set_or_prompt cluster_name "ACK cluster name" "${default_cluster_name}" "${cluster_name_override}"
    set_or_prompt zone_ids_csv "Zone IDs, comma separated" "${region}-j,${region}-j,${region}-j" "${zone_ids_override}"
    set_or_prompt vswitch_cidrs_csv "vSwitch CIDRs, comma separated" "192.168.0.0/20,192.168.16.0/20,192.168.32.0/20" "${vswitch_cidrs_override}"
    set_or_prompt node_pool_size "Node count" "3" "${node_pool_size_override}"
    set_or_prompt node_pool_instance_types_csv "Node instance types, comma separated" "ecs.c7.3xlarge" "${node_pool_instance_types_override}"
    set_or_prompt node_pool_key_name "ECS key pair name, empty to skip" "" "${node_pool_key_name_override}"
    set_or_prompt acr_namespace "ACR namespace" "akernel" "${acr_namespace_override}"
    set_or_prompt monitor_storage_class "Monitor StorageClass" "alicloud-disk-essd" "${monitor_storage_class_override}"
    set_or_prompt image_repository "All-in-one image repository" "registry.${region}.aliyuncs.com/${acr_namespace}/all-in-one" "${image_repository_override}"
    ;;
  huaweicloud)
    set_or_prompt region "Huawei Cloud region" "cn-north-4" "${region_override}"
    set_or_prompt cluster_name "CCE cluster name" "${default_cluster_name}" "${cluster_name_override}"
    set_or_prompt availability_zone "Availability zone" "${region}a" "${availability_zone_override}"
    set_or_prompt node_pool_size "Node count" "3" "${node_pool_size_override}"
    set_or_prompt node_flavor_id "Node flavor ID, empty to auto-select" "" "${node_flavor_id_override}"
    set_or_prompt node_pool_key_pair "ECS key pair name, empty to use a password" "" "${node_pool_key_name_override}"
    set_or_prompt node_pool_login_password "Node login password, empty when using a key pair" "" "${node_pool_login_password_override}"
    if [[ -z "${node_pool_key_pair}" && -z "${node_pool_login_password}" ]]; then
      die "Huawei Cloud requires NODE_POOL_KEY_NAME or NODE_POOL_LOGIN_PASSWORD"
    fi
    set_or_prompt monitor_storage_class "Monitor StorageClass" "csi-disk" "${monitor_storage_class_override}"
    set_or_prompt image_repository "All-in-one image repository" "swr.${region}.myhuaweicloud.com/akernel/all-in-one" "${image_repository_override}"
    ;;
esac
set_or_prompt image_tag "All-in-one image tag" "${default_tag}" "${image_tag_override}"
set_or_prompt install_monitor "Install monitor chart (true/false)" "true" "${install_monitor_override}"
set_or_prompt install_dragonfly "Install Dragonfly and dedicated node pools (true/false)" "false" "${install_dragonfly_override}"
set_or_prompt enable_runc "Enable the optional runc runtime (true/false)" "false" "${enable_runc_override}"
set_or_prompt grafana_public_access "Expose Grafana LoadBalancer (true/false)" "true" "${grafana_public_access_override}"
set_or_prompt grafana_admin_password \
  "Grafana admin password (empty to generate)" "" \
  "${grafana_admin_password_override}"
if [[ -z "${grafana_admin_password}" ]]; then
  grafana_admin_password="$(generate_password)"
fi
install_monitor="$(normalize_bool "${install_monitor}")"
install_dragonfly="$(normalize_bool "${install_dragonfly}")"
enable_runc="$(normalize_bool "${enable_runc}")"
grafana_public_access="$(normalize_bool "${grafana_public_access}")"

dir="$(state_dir "${env_name}")"
mkdir -p "${dir}"
chmod 700 "${dir}"

seed_file="${dir}/iam-seed"
tfvars_file="${dir}/terraform.tfvars"
config_file="${dir}/config.env"
kubeconfig_file="${dir}/kubeconfig"
grafana_password_file="${dir}/grafana-admin-password"

if [[ "${force}" -ne 1 && ( -f "${tfvars_file}" || -f "${config_file}" ) ]]; then
  if [[ "${non_interactive}" -eq 1 ]]; then
    die "local profile ${dir} already exists; rerun with --force to overwrite generated config files"
  fi
  echo "Local AKernel profile already exists: ${dir}"
  echo "This will overwrite config.env and terraform.tfvars."
  if [[ -f "${seed_file}" ]]; then
    echo "The existing iam-seed will be reused so previously generated tokens stay compatible."
  fi
  read -r -p "Overwrite this profile? [y/N]: " overwrite
  case "$(printf '%s' "${overwrite}" | tr '[:upper:]' '[:lower:]')" in
    y | yes)
      ;;
    *)
      die "not overwriting ${dir}"
      ;;
  esac
fi

if [[ -n "${iam_seed_hex_override}" ]]; then
  iam_seed_hex_override="$(printf '%s' "${iam_seed_hex_override}" | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]')"
  if [[ ! "${iam_seed_hex_override}" =~ ^[0-9A-F]+$ || $(( ${#iam_seed_hex_override} % 2 )) -ne 0 ]]; then
    die "--iam-seed-hex must be an even-length hex string"
  fi
  printf '%s\n' "${iam_seed_hex_override}" > "${seed_file}"
  chmod 600 "${seed_file}"
elif [[ ! -s "${seed_file}" ]]; then
  generate_hex_seed > "${seed_file}"
  chmod 600 "${seed_file}"
fi
iam_seed="$(tr -d '[:space:]' < "${seed_file}" | tr '[:lower:]' '[:upper:]')"
if [[ ! "${iam_seed}" =~ ^[0-9A-F]+$ || $(( ${#iam_seed} % 2 )) -ne 0 ]]; then
  die "${seed_file} must contain an even-length hexadecimal seed"
fi
printf '%s\n' "${iam_seed}" > "${seed_file}"
chmod 600 "${seed_file}"
printf '%s\n' "${grafana_admin_password}" > "${grafana_password_file}"
chmod 600 "${grafana_password_file}"

case "${vendor}" in
  aliyun)
    zone_ids="$(csv_to_hcl_list "${zone_ids_csv}")"
    vswitch_cidrs="$(csv_to_hcl_list "${vswitch_cidrs_csv}")"
    node_pool_instance_types="$(csv_to_hcl_list "${node_pool_instance_types_csv}")"

    cat > "${tfvars_file}" <<EOF
region       = "${region}"
acr_region   = "${region}"
acr_namespace = "${acr_namespace}"
cluster_name = "${cluster_name}"

zone_ids      = ${zone_ids}
vpc_cidr      = "192.168.0.0/16"
vswitch_cidrs = ${vswitch_cidrs}

create_cluster           = true
api_server_public_access = true
kubeconfig_output_path   = "${kubeconfig_file}"
k8s_version              = "1.34.3-aliyun.1"
network_addon            = "terway-eniip"
service_cidr             = "10.20.0.0/20"

node_pool_instance_types       = ${node_pool_instance_types}
node_pool_size                 = ${node_pool_size}
node_pool_system_disk_category = "cloud_essd"
node_pool_system_disk_size     = 100
node_pool_data_disk_enabled    = true
node_pool_data_disk_category   = "cloud_essd"
node_pool_data_disk_size       = 100
node_pool_extra_data_disk_enabled    = true
node_pool_extra_data_disk_category   = "cloud_essd"
node_pool_extra_data_disk_size       = 300
node_pool_extra_data_disk_mount_path = "/home/akernel"
node_pool_extra_data_disk_fs_type    = "xfs"
node_pool_key_name             = "${node_pool_key_name}"

core_namespace = "akernel"

master_image_repository = "${image_repository}"
master_image_tag        = "${image_tag}"
node_image_repository   = "${image_repository}"
node_image_tag          = "${image_tag}"
iam_litebus_data_key    = "${iam_seed}"

frontend_enabled  = true
frontend_replicas = 1
frontend_cpu      = "1"
frontend_memory   = "2Gi"

traefik_enabled               = true
install_traefik               = true
traefik_service_type          = "LoadBalancer"
traefik_enable_web_entrypoint = true
traefik_websecure_port        = 443
traefik_web_port              = 80
traefik_tls_enabled           = false
traefik_tls_create_secret     = false
traefik_internal_stats_enabled = true

install_prereqs = false

install_monitor        = ${install_monitor}
monitor_namespace      = "akernel-monitor"
monitor_storage_class  = "${monitor_storage_class}"
grafana_public_access  = ${grafana_public_access}
grafana_admin_password = "${grafana_admin_password}"

install_dragonfly = ${install_dragonfly}
enable_runc       = ${enable_runc}
EOF
    ;;
  huaweicloud)
    cat > "${tfvars_file}" <<EOF
region       = "${region}"
cluster_name = "${cluster_name}"

create_cluster            = true
cluster_api_public_access = true
kubeconfig_output_path    = "${kubeconfig_file}"
availability_zone         = "${availability_zone}"

node_pool_size             = ${node_pool_size}
node_pool_min_size         = ${node_pool_size}
node_pool_max_size         = ${node_pool_size}
node_flavor_id             = "${node_flavor_id}"
node_pool_data_volume_enabled = true
node_pool_data_volume_type    = "SSD"
node_pool_data_volume_size    = 100
node_pool_key_pair             = "${node_pool_key_pair}"
node_pool_login_password       = "${node_pool_login_password}"

core_namespace = "akernel"

master_image_repository = "${image_repository}"
master_image_tag        = "${image_tag}"
node_image_repository   = "${image_repository}"
node_image_tag          = "${image_tag}"
iam_litebus_data_key    = "${iam_seed}"

frontend_enabled  = true
frontend_replicas = 1
frontend_cpu      = "1"
frontend_memory   = "2Gi"

install_traefik                  = true
traefik_public_access            = true
traefik_enable_web_entrypoint    = true
traefik_websecure_port           = 443
traefik_web_port                 = 80
traefik_tls_enabled              = false
traefik_tls_create_secret        = false
traefik_internal_stats_enabled   = true

install_prereqs = false

install_monitor        = ${install_monitor}
monitor_namespace      = "akernel-monitor"
monitor_storage_class  = "${monitor_storage_class}"
grafana_public_access  = ${grafana_public_access}
grafana_admin_password = "${grafana_admin_password}"

install_dragonfly = ${install_dragonfly}
enable_runc       = ${enable_runc}
EOF
    ;;
esac

terraform fmt "${tfvars_file}" >/dev/null

cat > "${config_file}" <<EOF
VENDOR=${vendor}
ENV_NAME=${env_name}
REGION=${region}
CLUSTER_NAME=${cluster_name}
TFVARS_FILE=${tfvars_file}
IAM_SEED_FILE=${seed_file}
GRAFANA_PASSWORD_FILE=${grafana_password_file}
KUBECONFIG_PATH=${kubeconfig_file}
IMAGE_REPOSITORY=${image_repository}
IMAGE_TAG=${image_tag}
CORE_NAMESPACE=akernel
MONITOR_NAMESPACE=akernel-monitor
INSTALL_DRAGONFLY=${install_dragonfly}
AKERNEL_ENABLE_RUNC=${enable_runc}
EOF

chmod 600 "${tfvars_file}" "${config_file}"

env_arg=""
if [[ "${env_name}" != "default" ]]; then
  env_arg=" ENV=${env_name}"
fi

info "wrote ${config_file}"
info "wrote ${tfvars_file}"
info "wrote ${seed_file}"
info "wrote ${grafana_password_file}"
echo
echo "Next:"
echo "  make plan${env_arg}"
echo
echo "If ${image_repository}:${image_tag} has not been built and pushed yet:"
echo "  make build${env_arg}"
echo "  make push${env_arg}"
