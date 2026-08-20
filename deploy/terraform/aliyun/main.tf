# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

provider "alicloud" {
  region = var.region
}

resource "null_resource" "input_validation" {
  lifecycle {
    precondition {
      condition     = var.create_cluster || length(var.kubeconfig_path) > 0
      error_message = "kubeconfig_path must be set when create_cluster=false."
    }
    precondition {
      condition     = length(var.zone_ids) == length(var.vswitch_cidrs)
      error_message = "zone_ids length must match vswitch_cidrs length."
    }
    precondition {
      condition     = !var.create_cluster || length(var.existing_vswitch_ids) >= 3 || length(var.vswitch_cidrs) >= 3
      error_message = "create_cluster=true requires at least 3 vswitch CIDRs/zone IDs (or 3 existing_vswitch_ids) for ACK."
    }
    precondition {
      condition     = length(var.existing_vswitch_ids) == 0 || length(var.vpc_id) > 0
      error_message = "vpc_id must be set when existing_vswitch_ids is provided."
    }
    precondition {
      condition     = length(var.node_pool_zone_id) == 0 || contains(var.zone_ids, var.node_pool_zone_id)
      error_message = "node_pool_zone_id must be one of zone_ids."
    }
    precondition {
      condition     = !(length(var.node_pool_key_name) > 0 && length(var.node_pool_login_password) > 0)
      error_message = "node_pool_key_name conflicts with node_pool_login_password. Set only one."
    }
    precondition {
      condition     = length(var.master_image_tag) > 0 && length(var.node_image_tag) > 0
      error_message = "master_image_tag and node_image_tag must be set to a published AKernel image tag."
    }
    # Skipped: DescribeAvailableResource API is unreliable in some regions (e.g. ap-southeast-3),
    # reporting types as unavailable when they can actually be provisioned via console/ACK.
    # precondition {
    #   condition     = !var.create_cluster || length(local.node_pool_preferred_type) == 0 || length(local.zones_with_preferred_type) > 0
    #   error_message = "Instance type ${local.node_pool_preferred_type} is not available in any of the configured zones (${join(", ", distinct(var.zone_ids))}). Check availability or change node_pool_instance_types."
    # }
  }
}

locals {
  oos_lifecycle_role_name   = "AliyunOOSLifecycleHook4CSRole"
  oos_lifecycle_policy_name = "AliyunOOSLifecycleHook4CSRolePolicy"
  use_terway_network        = var.network_addon != "flannel"
  effective_vpc_id          = length(var.vpc_id) > 0 ? var.vpc_id : try(alicloud_vpc.this[0].id, "")
  # distinct() because callers commonly pass existing_vswitch_ids with positional
  # duplicates to match zone_ids length (e.g. ["vsw-A", "vsw-A", "vsw-B"] when
  # zone_ids=["az-a","az-a","az-b"]). ACK dedupes server-side, which then drifts
  # every plan into a no-op "update in place". Dedupe at the source.
  effective_vswitch_ids    = length(var.existing_vswitch_ids) > 0 ? distinct(var.existing_vswitch_ids) : alicloud_vswitch.this[*].id
  node_pool_preferred_type = length(var.node_pool_instance_types) > 0 ? var.node_pool_instance_types[0] : ""
  # Zones where the preferred instance type is actually available
  zones_with_preferred_type = var.create_cluster && length(local.node_pool_preferred_type) > 0 ? [
    for zone in distinct(var.zone_ids) :
    zone if contains(try(data.alicloud_instance_types.zone_scan[zone].ids, []), local.node_pool_preferred_type)
  ] : distinct(var.zone_ids)
  # Auto-select: prefer user override, then first zone with availability, then fall back to first configured zone
  node_pool_zone_id = length(var.node_pool_zone_id) > 0 ? var.node_pool_zone_id : (
    length(local.zones_with_preferred_type) > 0 ? local.zones_with_preferred_type[0] : var.zone_ids[0]
  )
  # No silent fallback — use the requested instance type directly.
  # node_pool_preferred_type is the first entry, used for zone availability scan.
  # The full list is passed to the default node pool for multi-type fallback.
  node_pool_effective_instance_type = local.node_pool_preferred_type
  node_pool_matched_vswitch_ids = var.create_cluster && length(var.existing_vswitch_ids) == 0 ? [
    for idx, zone in var.zone_ids : alicloud_vswitch.this[idx].id if zone == local.node_pool_zone_id
  ] : []
  node_pool_vswitch_ids = length(var.existing_vswitch_ids) > 0 ? distinct(var.existing_vswitch_ids) : (var.create_cluster ? (
    length(local.node_pool_matched_vswitch_ids) > 0 ? local.node_pool_matched_vswitch_ids : alicloud_vswitch.this[*].id
  ) : [])

  # ACR registry: enterprise URL takes precedence, otherwise auto-detect personal ACR VPC endpoint
  acr_region   = length(var.acr_region) > 0 ? var.acr_region : var.region
  acr_use_vpc  = var.acr_use_vpc != null ? var.acr_use_vpc : (local.acr_region == var.region)
  acr_registry = length(var.acr_registry_url) > 0 ? var.acr_registry_url : "registry${local.acr_use_vpc ? "-vpc" : ""}.${local.acr_region}.aliyuncs.com/${var.acr_namespace}"
  # Extract registry host for imagePullSecret (strip namespace path)
  acr_host           = split("/", local.acr_registry)[0]
  acr_secret_enabled = length(var.acr_username) > 0 && length(var.acr_password) > 0
  node_pool_bootstrap_base = templatefile("${path.module}/../shared/node-bootstrap.sh.tftpl", {
    sandboxd_nat_backend = var.sandboxd_nat_backend
    max_user_namespaces  = var.node_pool_max_user_namespaces
  })
  node_pool_bootstrap_user_data = var.node_pool_extra_data_disk_enabled ? join("\n", [
    local.node_pool_bootstrap_base,
    <<-EOT
      # Mount extra data disk
      set -euo pipefail
      MOUNT_PATH="${var.node_pool_extra_data_disk_mount_path}"
      FS_TYPE="${var.node_pool_extra_data_disk_fs_type}"
      MARKER_PATH="${var.node_pool_extra_data_disk_mount_path}/.storage-ready"

      if mountpoint -q "$MOUNT_PATH" 2>/dev/null; then
        echo "[data-disk] $MOUNT_PATH is already mounted, skipping."
        exit 0
      fi

      ROOT_SOURCE=$(findmnt -n -o SOURCE /)
      ROOT_DEV=""
      if [ -n "$ROOT_SOURCE" ]; then
        ROOT_DEV=$(lsblk -ndo PKNAME "$ROOT_SOURCE" 2>/dev/null || true)
      fi

      TARGET_DEV=""
      for dev in /dev/vd[b-z] /dev/xvd[b-z] /dev/sd[b-z] /dev/nvme[0-9]n1 /dev/nvme[1-9][0-9]n1; do
        [ -b "$dev" ] || continue
        dev_name=$(basename "$dev")
        if [ -n "$ROOT_DEV" ] && [ "$dev_name" = "$ROOT_DEV" ]; then
          continue
        fi
        if lsblk -ln -o MOUNTPOINT "$dev" 2>/dev/null | grep -q '/'; then
          continue
        fi
        if pvs "$dev" &>/dev/null 2>&1; then
          continue
        fi
        if blkid "$dev" &>/dev/null; then
          EXISTING_FS=$(blkid -s TYPE -o value "$dev" 2>/dev/null || true)
          if [ "$EXISTING_FS" = "$FS_TYPE" ]; then
            TARGET_DEV="$dev"
            break
          fi
          continue
        fi
        TARGET_DEV="$dev"
        break
      done

      if [ -z "$TARGET_DEV" ]; then
        echo "[data-disk] No available data disk found, skipping."
        exit 0
      fi

      echo "[data-disk] Using device: $TARGET_DEV"
      mkdir -p "$MOUNT_PATH"

      if ! blkid "$TARGET_DEV" &>/dev/null; then
        echo "[data-disk] Formatting $TARGET_DEV with $FS_TYPE..."
        if [ "$FS_TYPE" = "xfs" ]; then
          mkfs.xfs -f "$TARGET_DEV"
        else
          mkfs.ext4 -F "$TARGET_DEV"
        fi
      fi

      mount -o defaults,noatime "$TARGET_DEV" "$MOUNT_PATH"
      UUID=$(blkid -s UUID -o value "$TARGET_DEV")
      if ! grep -q "$UUID" /etc/fstab 2>/dev/null; then
        echo "UUID=$UUID $MOUNT_PATH $FS_TYPE defaults,noatime 0 0" >> /etc/fstab
      fi
      echo "[data-disk] Mounted $TARGET_DEV at $MOUNT_PATH ($FS_TYPE) successfully."
      touch "$MARKER_PATH"
      echo "[data-disk] Created readiness marker: $MARKER_PATH"
    EOT
  ]) : local.node_pool_bootstrap_base
  dragonfly_seed_user_data = var.dragonfly_seed_node_pool.mount_local_nvme ? join("\n", [
    local.node_pool_bootstrap_base,
    templatefile("${path.module}/../shared/local-nvme-mount.sh.tftpl", {
      mount_path = var.dragonfly_seed_node_pool.local_nvme_mount_path
    })
  ]) : local.node_pool_bootstrap_base

  slb_security_group_annotations = length(var.security_group_id) > 0 ? {
    "service.beta.kubernetes.io/alibaba-cloud-loadbalancer-security-group-ids" = var.security_group_id
  } : {}
  effective_traefik_service_annotations = merge(local.slb_security_group_annotations, var.traefik_service_annotations)

  effective_storage_class           = var.storage_class
  effective_monitor_storage_class   = length(var.monitor_storage_class) > 0 ? var.monitor_storage_class : local.effective_storage_class
  effective_node_home_csi_sc        = length(var.node_home_csi_storage_class) > 0 ? var.node_home_csi_storage_class : local.effective_storage_class
  effective_dragonfly_storage_class = length(var.dragonfly_storage_class) > 0 ? var.dragonfly_storage_class : local.effective_storage_class

  oos_lifecycle_role_exists = var.create_cluster && var.auto_authorize_oos_lifecycle_role ? length(try(data.alicloud_ram_roles.oos_lifecycle_hook_for_ack[0].ids, [])) > 0 : false
  oos_lifecycle_policy_attached = var.create_cluster && var.auto_authorize_oos_lifecycle_role && local.oos_lifecycle_role_exists ? contains([
    for a in try(data.alicloud_ram_role_policy_attachments.oos_lifecycle_hook_for_ack[0].attachments, []) : a.policy_name
  ], local.oos_lifecycle_policy_name) : false
  oss_enabled = length(var.oss_endpoint) > 0 && length(var.oss_access_key_id) > 0 && length(var.oss_access_key_secret) > 0 && length(var.oss_bucket) > 0
  kubeconfig_path = var.create_cluster ? (
    length(var.kubeconfig_output_path) > 0 ? var.kubeconfig_output_path : "${path.module}/.kubeconfig"
  ) : var.kubeconfig_path
  generated_oss_auths = local.oss_enabled ? {
    "${var.oss_endpoint}/${var.oss_bucket}" = {
      access_key_id     = var.oss_access_key_id
      access_key_secret = var.oss_access_key_secret
    }
  } : {}
  oss_auths = merge(local.generated_oss_auths, var.oss_auths)
  registry_auths = {
    auths = { for host, cred in var.registry_auths : host => { auth = base64encode("${cred.username}:${cred.password}") } }
  }

  etcd_image_repo              = length(var.etcd_image_repository) > 0 ? var.etcd_image_repository : "public.ecr.aws/bitnami/etcd"
  master_image_repo            = length(var.master_image_repository) > 0 ? var.master_image_repository : "${local.acr_registry}/all-in-one"
  node_image_repo              = length(var.node_image_repository) > 0 ? var.node_image_repository : "${local.acr_registry}/all-in-one"
  traefik_image_repo           = length(var.traefik_image_repository) > 0 ? var.traefik_image_repository : "traefik"
  traefik_internal_stats_image = length(var.traefik_internal_stats_image) > 0 ? var.traefik_internal_stats_image : "${local.acr_registry}/busybox:1.37.0-musl"

  core_values = templatefile("${path.module}/values-akernel.yaml.tmpl", {
    acr_registry                      = local.acr_registry
    acr_secret_enabled                = local.acr_secret_enabled
    acr_host                          = local.acr_host
    acr_username                      = var.acr_username
    acr_password                      = var.acr_password
    etcd_image_repository             = local.etcd_image_repo
    etcd_image_tag                    = var.etcd_image_tag
    master_image_repository           = local.master_image_repo
    master_image_tag                  = var.master_image_tag
    node_image_repository             = local.node_image_repo
    node_image_tag                    = var.node_image_tag
    traefik_image_repository          = local.traefik_image_repo
    traefik_image_tag                 = var.traefik_image_tag
    iam_litebus_data_key              = var.iam_litebus_data_key
    enable_kruise                     = var.install_prereqs
    master_service_type               = (var.master_public_access_8888 && !var.traefik_enabled) ? var.master_service_type : "ClusterIP"
    traefik_enabled                   = var.traefik_enabled
    sandboxd_nat_backend              = var.sandboxd_nat_backend
    enable_runc                       = var.enable_runc
    node_secret_create                = var.node_secret_create
    node_home_use_csi_ephemeral       = var.node_home_use_csi_ephemeral
    node_home_csi_storage_class       = local.effective_node_home_csi_sc
    node_home_csi_size                = var.node_home_csi_size
    node_pool_extra_data_disk_enabled = var.node_pool_extra_data_disk_enabled
    node_storage_init_image           = var.node_storage_init_image
    node_storage_init_mount_path      = var.node_pool_extra_data_disk_mount_path
    oss_auths                         = local.oss_auths
    registry_auths                    = local.registry_auths

    etcd_storage_class = local.effective_storage_class
    etcd_cpu           = var.etcd_resources.cpu
    etcd_memory        = var.etcd_resources.memory
    etcd_ephemeral     = var.etcd_resources.ephemeral_storage
    etcd_pvc_size      = var.etcd_resources.pvc_size
    master_cpu         = var.master_resources.cpu
    master_memory      = var.master_resources.memory
    master_ephemeral   = var.master_resources.ephemeral_storage
    node_ephemeral     = var.node_resources.ephemeral_storage

    oss_endpoint          = var.oss_endpoint
    oss_access_key_id     = var.oss_access_key_id
    oss_access_key_secret = var.oss_access_key_secret
    oss_bucket            = var.oss_bucket
    oss_object_prefix     = var.oss_object_prefix
    oss_proxy_url         = length(var.oss_proxy_url) > 0 ? var.oss_proxy_url : (var.install_dragonfly ? "http://dragonfly-seed-client.${var.dragonfly_namespace}.svc.cluster.local:4001" : "")

    install_monitor   = var.install_monitor
    monitor_namespace = var.monitor_namespace
    akernel_env       = length(var.akernel_env) > 0 ? var.akernel_env : var.cluster_name

    master_replicas   = var.master_replicas
    frontend_enabled  = var.frontend_enabled
    frontend_replicas = var.frontend_replicas
    frontend_cpu      = var.frontend_cpu
    frontend_memory   = var.frontend_memory

    install_traefik               = var.install_traefik
    traefik_replicas              = var.traefik_replicas
    traefik_tcp_port              = var.traefik_tcp_port
    traefik_enable_web_entrypoint = var.traefik_enable_web_entrypoint
    traefik_web_port              = var.traefik_web_port
    traefik_websecure_port        = var.traefik_websecure_port
    traefik_service_type          = var.traefik_service_type
    traefik_service_annotations   = local.effective_traefik_service_annotations
    traefik_tls_enabled           = var.traefik_tls_enabled
    traefik_tls_create_secret     = var.traefik_tls_create_secret
    traefik_tls_cert              = var.traefik_tls_cert
    traefik_tls_key               = var.traefik_tls_key
    traefik_internal_stats        = var.traefik_internal_stats_enabled
    traefik_internal_stats_image  = local.traefik_internal_stats_image
    traefik_grafana_enabled       = var.install_monitor
    traefik_grafana_url           = var.install_monitor ? "http://grafana.${var.monitor_namespace}.svc:3000" : ""

  })

  monitor_image_registry = var.monitor_image_registry

  monitor_values = templatefile("${path.module}/values-monitor.yaml.tmpl", {
    image_registry         = local.monitor_image_registry
    acr_secret_enabled     = local.acr_secret_enabled
    acr_host               = local.acr_host
    acr_username           = var.acr_username
    acr_password           = var.acr_password
    enable_kruise          = var.install_prereqs
    monitor_storage_class  = local.effective_monitor_storage_class
    grafana_admin_password = var.grafana_admin_password
    grafana_public_access  = var.grafana_public_access
    grafana_acl_id         = var.grafana_acl_id
    core_namespace         = var.core_namespace
    akernel_env            = var.akernel_env
    install_dragonfly      = var.install_dragonfly
    dragonfly_namespace    = var.dragonfly_namespace

    grafana_cpu          = var.grafana_resources.cpu
    grafana_memory       = var.grafana_resources.memory
    grafana_ephemeral    = var.grafana_resources.ephemeral_storage
    grafana_pvc_size     = var.grafana_resources.pvc_size
    prometheus_cpu       = var.prometheus_resources.cpu
    prometheus_memory    = var.prometheus_resources.memory
    prometheus_ephemeral = var.prometheus_resources.ephemeral_storage
    prometheus_pvc_size  = var.prometheus_resources.pvc_size
    loki_cpu             = var.loki_resources.cpu
    loki_memory          = var.loki_resources.memory
    loki_ephemeral       = var.loki_resources.ephemeral_storage
    loki_pvc_size        = var.loki_resources.pvc_size
    tempo_cpu            = var.tempo_resources.cpu
    tempo_memory         = var.tempo_resources.memory
    tempo_ephemeral      = var.tempo_resources.ephemeral_storage
    tempo_pvc_size       = var.tempo_resources.pvc_size
  })

  # Dragonfly server pool (manager + scheduler) — fixed size, goes through extra node pools
  dragonfly_server_pool = var.install_dragonfly && var.dragonfly_server_node_pool.enabled ? [{
    name              = var.dragonfly_server_node_pool.name
    size              = var.dragonfly_server_node_pool.size
    instance_types    = var.dragonfly_server_node_pool.instance_types
    system_disk_size  = var.dragonfly_server_node_pool.system_disk_size
    data_disk_enabled = true
    data_disk_size    = var.dragonfly_server_node_pool.data_disk_size
    labels = {
      (var.dragonfly_server_node_pool.node_label_key) = var.dragonfly_server_node_pool.node_label_value
    }
    taints = [{
      key    = var.dragonfly_server_node_pool.taint_key
      value  = var.dragonfly_server_node_pool.taint_value
      effect = var.dragonfly_server_node_pool.taint_effect
    }]
  }] : []

  # Seed pool is created as a dedicated resource (with autoscaling), not via extra
  all_extra_node_pools = concat(var.extra_node_pools, local.dragonfly_server_pool)

  dragonfly_values = var.install_dragonfly ? templatefile("${path.module}/values-dragonfly.yaml.tmpl", {
    storage_class              = local.effective_storage_class
    dragonfly_storage_class    = local.effective_dragonfly_storage_class
    seed_client_pvc_size       = var.dragonfly_seed_client_pvc_size
    image_registry             = var.dragonfly_image_registry
    manager_image_tag          = var.dragonfly_manager_image_tag
    scheduler_image_tag        = var.dragonfly_scheduler_image_tag
    seed_client_image_tag      = var.dragonfly_seed_client_image_tag
    client_image_tag           = var.dragonfly_client_image_tag
    injector_image_tag         = var.dragonfly_injector_image_tag
    dfinit_image_tag           = var.dragonfly_dfinit_image_tag
    manager_replicas           = var.dragonfly_manager_resources.replicas
    manager_cpu_request        = var.dragonfly_manager_resources.cpu_request
    manager_memory_request     = var.dragonfly_manager_resources.memory_request
    manager_cpu_limit          = var.dragonfly_manager_resources.cpu_limit
    manager_memory_limit       = var.dragonfly_manager_resources.memory_limit
    scheduler_replicas         = var.dragonfly_scheduler_resources.replicas
    scheduler_cpu_request      = var.dragonfly_scheduler_resources.cpu_request
    scheduler_memory_request   = var.dragonfly_scheduler_resources.memory_request
    scheduler_cpu_limit        = var.dragonfly_scheduler_resources.cpu_limit
    scheduler_memory_limit     = var.dragonfly_scheduler_resources.memory_limit
    seed_client_cpu_request    = var.dragonfly_seed_client_resources.cpu_request
    seed_client_memory_request = var.dragonfly_seed_client_resources.memory_request
    seed_client_cpu_limit      = var.dragonfly_seed_client_resources.cpu_limit
    seed_client_memory_limit   = var.dragonfly_seed_client_resources.memory_limit
    seed_replicas              = var.dragonfly_seed_node_pool.size
    seed_label_key             = var.dragonfly_seed_node_pool.node_label_key
    seed_label_value           = var.dragonfly_seed_node_pool.node_label_value
    seed_taint_key             = var.dragonfly_seed_node_pool.taint_key
    seed_taint_value           = var.dragonfly_seed_node_pool.taint_value
    seed_taint_effect          = var.dragonfly_seed_node_pool.taint_effect
    server_label_key           = var.dragonfly_server_node_pool.node_label_key
    server_label_value         = var.dragonfly_server_node_pool.node_label_value
    server_taint_key           = var.dragonfly_server_node_pool.taint_key
    server_taint_value         = var.dragonfly_server_node_pool.taint_value
    server_taint_effect        = var.dragonfly_server_node_pool.taint_effect
  }) : ""



}

resource "alicloud_vpc" "this" {
  count      = var.create_cluster && length(var.vpc_id) == 0 ? 1 : 0
  vpc_name   = "${var.cluster_name}-vpc"
  cidr_block = var.vpc_cidr
  tags       = var.tags
  depends_on = [null_resource.input_validation]
}

resource "alicloud_vswitch" "this" {
  count        = var.create_cluster && length(var.existing_vswitch_ids) == 0 ? length(var.vswitch_cidrs) : 0
  vpc_id       = local.effective_vpc_id
  zone_id      = var.zone_ids[count.index]
  cidr_block   = var.vswitch_cidrs[count.index]
  vswitch_name = "${var.cluster_name}-vsw-${count.index + 1}"
  tags         = var.tags
}

data "alicloud_ack_service" "open" {
  count  = var.create_cluster ? 1 : 0
  enable = "On"
  type   = "propayasgo"
}

# Scan instance type availability across all configured zones.
# This allows auto-selecting a zone that has the requested type,
# instead of silently falling back to a smaller type.
data "alicloud_instance_types" "zone_scan" {
  for_each             = var.create_cluster ? toset(var.zone_ids) : toset([])
  availability_zone    = each.value
  kubernetes_node_role = "Worker"
  instance_charge_type = "PostPaid"
  system_disk_category = var.node_pool_system_disk_category
  instance_type_family = length(local.node_pool_preferred_type) > 0 ? join(".", slice(split(".", local.node_pool_preferred_type), 0, 2)) : null
}

data "alicloud_ram_roles" "oos_lifecycle_hook_for_ack" {
  count      = var.create_cluster && var.auto_authorize_oos_lifecycle_role ? 1 : 0
  name_regex = "^${local.oos_lifecycle_role_name}$"
}

resource "alicloud_ram_role" "oos_lifecycle_hook_for_ack" {
  count     = var.create_cluster && var.auto_authorize_oos_lifecycle_role && !local.oos_lifecycle_role_exists ? 1 : 0
  role_name = local.oos_lifecycle_role_name
  assume_role_policy_document = jsonencode({
    Version = "1"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = ["oos.aliyuncs.com"]
        }
      }
    ]
  })
  description = "Role for OOS lifecycle hook used by ACK node pool."
  force       = true
}

data "alicloud_ram_role_policy_attachments" "oos_lifecycle_hook_for_ack" {
  count     = var.create_cluster && var.auto_authorize_oos_lifecycle_role && local.oos_lifecycle_role_exists ? 1 : 0
  role_name = local.oos_lifecycle_role_name
}

resource "alicloud_ram_role_policy_attachment" "oos_lifecycle_hook_for_ack" {
  count       = var.create_cluster && var.auto_authorize_oos_lifecycle_role && !local.oos_lifecycle_policy_attached ? 1 : 0
  policy_name = local.oos_lifecycle_policy_name
  policy_type = "System"
  role_name   = local.oos_lifecycle_role_name

  depends_on = [alicloud_ram_role.oos_lifecycle_hook_for_ack]
}

# Network note:
# This module does not define explicit node security-group rules for control-plane -> node access.
# It currently relies on ACK managed-cluster defaults (auto-assigned default security group),
# and based on current ACK docs this default behavior supports control-plane/node connectivity.
resource "alicloud_cs_managed_kubernetes" "ack" {
  count        = var.create_cluster ? 1 : 0
  name         = var.cluster_name
  cluster_spec = "ack.pro.small"
  version      = var.k8s_version

  vswitch_ids       = local.effective_vswitch_ids
  security_group_id = length(var.security_group_id) > 0 ? var.security_group_id : null

  new_nat_gateway      = true
  pod_cidr             = local.use_terway_network ? null : var.pod_cidr
  pod_vswitch_ids      = local.use_terway_network ? (length(var.pod_vswitch_ids) > 0 ? var.pod_vswitch_ids : local.effective_vswitch_ids) : null
  service_cidr         = var.service_cidr
  slb_internet_enabled = var.api_server_public_access

  dynamic "addons" {
    for_each = local.use_terway_network ? ["terway-controlplane", var.network_addon] : []
    content {
      name = addons.value
    }
  }

  tags = var.tags

  depends_on = [data.alicloud_ack_service.open, alicloud_ram_role_policy_attachment.oos_lifecycle_hook_for_ack]
}

resource "alicloud_cs_kubernetes_node_pool" "default_with_key" {
  count          = var.create_cluster && length(var.node_pool_key_name) > 0 ? 1 : 0
  cluster_id     = alicloud_cs_managed_kubernetes.ack[0].id
  node_pool_name = "${var.cluster_name}-default"

  vswitch_ids        = local.node_pool_vswitch_ids
  security_group_ids = length(var.node_pool_security_group_ids) > 0 ? var.node_pool_security_group_ids : null
  instance_types     = var.node_pool_instance_types
  image_type         = length(var.node_pool_image_id) > 0 ? null : var.node_pool_image_type
  image_id           = length(var.node_pool_image_id) > 0 ? var.node_pool_image_id : null
  desired_size       = var.node_pool_size

  system_disk_category = var.node_pool_system_disk_category
  system_disk_size     = var.node_pool_system_disk_size
  dynamic "data_disks" {
    for_each = var.node_pool_data_disk_enabled ? [1] : []
    content {
      category = var.node_pool_data_disk_category
      size     = var.node_pool_data_disk_size
    }
  }
  dynamic "data_disks" {
    for_each = var.node_pool_extra_data_disk_enabled ? [1] : []
    content {
      category = var.node_pool_extra_data_disk_category
      size     = var.node_pool_extra_data_disk_size
    }
  }

  key_name  = var.node_pool_key_name
  user_data = local.node_pool_bootstrap_user_data
  tags      = var.tags

  dynamic "labels" {
    for_each = var.node_pool_exclusive_eni ? [1] : []
    content {
      key   = "k8s.aliyun.com/exclusive-mode-eni-type"
      value = "eniOnly"
    }
  }
}

resource "alicloud_cs_kubernetes_node_pool" "default_with_password" {
  count          = var.create_cluster && length(var.node_pool_key_name) == 0 ? 1 : 0
  cluster_id     = alicloud_cs_managed_kubernetes.ack[0].id
  node_pool_name = "${var.cluster_name}-default"

  vswitch_ids        = local.node_pool_vswitch_ids
  security_group_ids = length(var.node_pool_security_group_ids) > 0 ? var.node_pool_security_group_ids : null
  instance_types     = var.node_pool_instance_types
  image_type         = length(var.node_pool_image_id) > 0 ? null : var.node_pool_image_type
  image_id           = length(var.node_pool_image_id) > 0 ? var.node_pool_image_id : null
  desired_size       = var.node_pool_size

  system_disk_category = var.node_pool_system_disk_category
  system_disk_size     = var.node_pool_system_disk_size
  dynamic "data_disks" {
    for_each = var.node_pool_data_disk_enabled ? [1] : []
    content {
      category = var.node_pool_data_disk_category
      size     = var.node_pool_data_disk_size
    }
  }
  dynamic "data_disks" {
    for_each = var.node_pool_extra_data_disk_enabled ? [1] : []
    content {
      category = var.node_pool_extra_data_disk_category
      size     = var.node_pool_extra_data_disk_size
    }
  }

  password  = length(var.node_pool_login_password) > 0 ? var.node_pool_login_password : null
  user_data = local.node_pool_bootstrap_user_data
  tags      = var.tags

  dynamic "labels" {
    for_each = var.node_pool_exclusive_eni ? [1] : []
    content {
      key   = "k8s.aliyun.com/exclusive-mode-eni-type"
      value = "eniOnly"
    }
  }
}

resource "alicloud_cs_kubernetes_node_pool" "extra" {
  for_each = {
    for pool in local.all_extra_node_pools : pool.name => pool
    if var.create_cluster
  }

  cluster_id     = alicloud_cs_managed_kubernetes.ack[0].id
  node_pool_name = "${var.cluster_name}-${each.key}"

  vswitch_ids        = local.node_pool_vswitch_ids
  security_group_ids = length(var.node_pool_security_group_ids) > 0 ? var.node_pool_security_group_ids : null
  instance_types     = coalesce(each.value.instance_types, [local.node_pool_effective_instance_type])
  image_type         = length(var.node_pool_image_id) > 0 ? null : var.node_pool_image_type
  image_id           = length(var.node_pool_image_id) > 0 ? var.node_pool_image_id : null
  desired_size       = each.value.size

  system_disk_category = var.node_pool_system_disk_category
  system_disk_size     = coalesce(each.value.system_disk_size, var.node_pool_system_disk_size)
  dynamic "data_disks" {
    for_each = coalesce(each.value.data_disk_enabled, true) ? [1] : []
    content {
      category = var.node_pool_data_disk_category
      size     = coalesce(each.value.data_disk_size, var.node_pool_data_disk_size)
    }
  }

  key_name = length(var.node_pool_key_name) > 0 ? var.node_pool_key_name : null
  password = length(var.node_pool_key_name) == 0 && length(var.node_pool_login_password) > 0 ? var.node_pool_login_password : null
  tags     = var.tags

  dynamic "labels" {
    for_each = each.value.labels
    content {
      key   = labels.key
      value = labels.value
    }
  }

  dynamic "taints" {
    for_each = each.value.taints
    content {
      key    = taints.value.key
      value  = taints.value.value
      effect = taints.value.effect
    }
  }
}

resource "alicloud_cs_kubernetes_node_pool" "dragonfly_seed" {
  count = var.install_dragonfly && var.create_cluster && var.dragonfly_seed_node_pool.enabled ? 1 : 0

  cluster_id     = alicloud_cs_managed_kubernetes.ack[0].id
  node_pool_name = "${var.cluster_name}-${var.dragonfly_seed_node_pool.name}"

  vswitch_ids        = local.node_pool_vswitch_ids
  security_group_ids = length(var.node_pool_security_group_ids) > 0 ? var.node_pool_security_group_ids : null
  instance_types     = coalesce(var.dragonfly_seed_node_pool.instance_types, [local.node_pool_effective_instance_type])
  image_type         = length(var.node_pool_image_id) > 0 ? null : var.node_pool_image_type
  image_id           = length(var.node_pool_image_id) > 0 ? var.node_pool_image_id : null
  desired_size       = var.dragonfly_seed_node_pool.auto_scaling_enabled ? null : var.dragonfly_seed_node_pool.size

  system_disk_category = coalesce(var.dragonfly_seed_node_pool.system_disk_category, var.node_pool_system_disk_category)
  system_disk_size     = coalesce(var.dragonfly_seed_node_pool.system_disk_size, var.node_pool_system_disk_size)
  dynamic "data_disks" {
    for_each = var.dragonfly_seed_node_pool.data_disk_enabled ? [1] : []
    content {
      category          = coalesce(var.dragonfly_seed_node_pool.data_disk_category, var.node_pool_data_disk_category)
      size              = var.dragonfly_seed_node_pool.data_disk_size
      performance_level = var.dragonfly_seed_node_pool.data_disk_performance_level
    }
  }

  key_name  = length(var.node_pool_key_name) > 0 ? var.node_pool_key_name : null
  password  = length(var.node_pool_key_name) == 0 && length(var.node_pool_login_password) > 0 ? var.node_pool_login_password : null
  user_data = local.dragonfly_seed_user_data
  tags      = var.tags

  dynamic "scaling_config" {
    for_each = var.dragonfly_seed_node_pool.auto_scaling_enabled ? [1] : []
    content {
      enable   = true
      min_size = var.dragonfly_seed_node_pool.min_size
      max_size = var.dragonfly_seed_node_pool.max_size
    }
  }

  labels {
    key   = var.dragonfly_seed_node_pool.node_label_key
    value = var.dragonfly_seed_node_pool.node_label_value
  }

  taints {
    key    = var.dragonfly_seed_node_pool.taint_key
    value  = var.dragonfly_seed_node_pool.taint_value
    effect = var.dragonfly_seed_node_pool.taint_effect
  }
}

data "alicloud_cs_cluster_credential" "ack" {
  count       = var.create_cluster ? 1 : 0
  cluster_id  = alicloud_cs_managed_kubernetes.ack[0].id
  output_file = local.kubeconfig_path
}

provider "helm" {
  kubernetes {
    config_path = local.kubeconfig_path
  }
}

resource "null_resource" "create_storage_class" {
  count = var.create_storage_class ? 1 : 0

  triggers = {
    storage_class = local.effective_storage_class
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      cat <<'YAML' | KUBECONFIG="${local.kubeconfig_path}" kubectl apply -f -
      apiVersion: storage.k8s.io/v1
      kind: StorageClass
      metadata:
        name: ${local.effective_storage_class}
      provisioner: diskplugin.csi.alibabacloud.com
      parameters:
        type: cloud_essd
      reclaimPolicy: Delete
      volumeBindingMode: WaitForFirstConsumer
      allowVolumeExpansion: true
      YAML
    EOT
  }

  depends_on = [data.alicloud_cs_cluster_credential.ack]
}

resource "null_resource" "create_dragonfly_storage_class" {
  count = var.create_dragonfly_storage_class ? 1 : 0

  triggers = {
    storage_class     = local.effective_dragonfly_storage_class
    performance_level = var.dragonfly_storage_class_pl
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      cat <<'YAML' | KUBECONFIG="${local.kubeconfig_path}" kubectl apply -f -
      apiVersion: storage.k8s.io/v1
      kind: StorageClass
      metadata:
        name: ${local.effective_dragonfly_storage_class}
      provisioner: diskplugin.csi.alibabacloud.com
      parameters:
        type: cloud_essd
        performanceLevel: ${var.dragonfly_storage_class_pl}
      reclaimPolicy: Delete
      volumeBindingMode: WaitForFirstConsumer
      allowVolumeExpansion: true
      YAML
    EOT
  }

  depends_on = [data.alicloud_cs_cluster_credential.ack]
}

resource "null_resource" "ensure_prereq_namespace" {
  count = var.install_prereqs ? 1 : 0

  triggers = {
    always = timestamp()
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      KUBECONFIG="${local.kubeconfig_path}" kubectl create namespace "${var.prereq_namespace}" --dry-run=client -o yaml | KUBECONFIG="${local.kubeconfig_path}" kubectl apply -f -
      KUBECONFIG="${local.kubeconfig_path}" kubectl label namespace "${var.prereq_namespace}" app.kubernetes.io/managed-by=Helm --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.prereq_namespace}" meta.helm.sh/release-name=openkruise --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.prereq_namespace}" meta.helm.sh/release-namespace="${var.prereq_namespace}" --overwrite
    EOT
  }

  depends_on = [data.alicloud_cs_cluster_credential.ack, alicloud_cs_kubernetes_node_pool.default_with_key, alicloud_cs_kubernetes_node_pool.default_with_password]
}

resource "null_resource" "ensure_core_namespace" {
  triggers = {
    always = timestamp()
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      KUBECONFIG="${local.kubeconfig_path}" kubectl create namespace "${var.core_namespace}" --dry-run=client -o yaml | KUBECONFIG="${local.kubeconfig_path}" kubectl apply -f -
      KUBECONFIG="${local.kubeconfig_path}" kubectl label namespace "${var.core_namespace}" app.kubernetes.io/managed-by=Helm --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl label namespace "${var.core_namespace}" k8s.aliyun.com/image-accelerate-mode=p2p --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.core_namespace}" meta.helm.sh/release-name=akernel-core --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.core_namespace}" meta.helm.sh/release-namespace="${var.core_namespace}" --overwrite
    EOT
  }

  depends_on = [data.alicloud_cs_cluster_credential.ack, alicloud_cs_kubernetes_node_pool.default_with_key, alicloud_cs_kubernetes_node_pool.default_with_password]
}

resource "null_resource" "ensure_monitor_namespace" {
  count = var.install_monitor ? 1 : 0

  triggers = {
    always = timestamp()
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      KUBECONFIG="${local.kubeconfig_path}" kubectl create namespace "${var.monitor_namespace}" --dry-run=client -o yaml | KUBECONFIG="${local.kubeconfig_path}" kubectl apply -f -
      KUBECONFIG="${local.kubeconfig_path}" kubectl label namespace "${var.monitor_namespace}" app.kubernetes.io/managed-by=Helm --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl label namespace "${var.monitor_namespace}" k8s.aliyun.com/image-accelerate-mode=p2p --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.monitor_namespace}" meta.helm.sh/release-name=akernel-monitor --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.monitor_namespace}" meta.helm.sh/release-namespace="${var.monitor_namespace}" --overwrite
    EOT
  }

  depends_on = [data.alicloud_cs_cluster_credential.ack, alicloud_cs_kubernetes_node_pool.default_with_key, alicloud_cs_kubernetes_node_pool.default_with_password]
}

resource "helm_release" "prereq_openkruise" {
  count = var.install_prereqs ? 1 : 0

  name             = "openkruise"
  repository       = var.prereq_kruise_repo
  chart            = var.prereq_kruise_chart
  namespace        = var.prereq_namespace
  create_namespace = false
  timeout          = 1800
  wait             = true
  atomic           = true
  cleanup_on_fail  = true

  version = length(var.prereq_kruise_chart_version) > 0 ? var.prereq_kruise_chart_version : null

  depends_on = [data.alicloud_cs_cluster_credential.ack, alicloud_cs_kubernetes_node_pool.default_with_key, alicloud_cs_kubernetes_node_pool.default_with_password, null_resource.ensure_prereq_namespace]
}

resource "helm_release" "akernel_core" {
  name                       = "akernel-core"
  chart                      = "${path.module}/../../akernel/charts/core"
  namespace                  = var.core_namespace
  create_namespace           = false
  timeout                    = 900
  disable_openapi_validation = true

  values = [local.core_values]

  # Force Terraform to detect changes in chart files (templates, configs, etc.)
  set {
    name  = "chartContentHash"
    value = sha256(join("", [for f in fileset("${path.module}/../../akernel/charts/core", "**") : filesha256("${path.module}/../../akernel/charts/core/${f}")]))
  }

  depends_on = [data.alicloud_cs_cluster_credential.ack, helm_release.prereq_openkruise, null_resource.ensure_core_namespace, null_resource.create_storage_class]
}

resource "helm_release" "akernel_monitor" {
  count = var.install_monitor ? 1 : 0

  name                       = "akernel-monitor"
  chart                      = "${path.module}/../../akernel/charts/monitor"
  namespace                  = var.monitor_namespace
  create_namespace           = false
  timeout                    = 900
  disable_openapi_validation = true

  values = [local.monitor_values]

  # Force Terraform to detect changes in chart files (dashboards, templates, etc.)
  set {
    name  = "chartContentHash"
    value = sha256(join("", [for f in fileset("${path.module}/../../akernel/charts/monitor", "**") : filesha256("${path.module}/../../akernel/charts/monitor/${f}")]))
  }

  depends_on = [data.alicloud_cs_cluster_credential.ack, helm_release.prereq_openkruise, null_resource.ensure_monitor_namespace, null_resource.create_storage_class]
}

resource "null_resource" "ensure_dragonfly_namespace" {
  count = var.install_dragonfly ? 1 : 0

  triggers = {
    always = timestamp()
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      KUBECONFIG="${local.kubeconfig_path}" kubectl create namespace "${var.dragonfly_namespace}" --dry-run=client -o yaml | KUBECONFIG="${local.kubeconfig_path}" kubectl apply -f -
      KUBECONFIG="${local.kubeconfig_path}" kubectl label namespace "${var.dragonfly_namespace}" app.kubernetes.io/managed-by=Helm --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl label namespace "${var.dragonfly_namespace}" k8s.aliyun.com/image-accelerate-mode=p2p --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.dragonfly_namespace}" meta.helm.sh/release-name=dragonfly --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.dragonfly_namespace}" meta.helm.sh/release-namespace="${var.dragonfly_namespace}" --overwrite
    EOT
  }

  depends_on = [data.alicloud_cs_cluster_credential.ack, alicloud_cs_kubernetes_node_pool.default_with_key, alicloud_cs_kubernetes_node_pool.default_with_password, alicloud_cs_kubernetes_node_pool.extra, alicloud_cs_kubernetes_node_pool.dragonfly_seed]
}

resource "helm_release" "dragonfly" {
  count = var.install_dragonfly ? 1 : 0

  name                       = "dragonfly"
  repository                 = var.dragonfly_chart_repository
  chart                      = var.dragonfly_chart_name
  version                    = var.dragonfly_chart_version
  namespace                  = var.dragonfly_namespace
  create_namespace           = false
  timeout                    = 900
  disable_openapi_validation = true

  values = [local.dragonfly_values]

  depends_on = [data.alicloud_cs_cluster_credential.ack, null_resource.ensure_dragonfly_namespace, alicloud_cs_kubernetes_node_pool.extra, alicloud_cs_kubernetes_node_pool.dragonfly_seed, null_resource.create_storage_class, null_resource.create_dragonfly_storage_class]
}
