# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

provider "huaweicloud" {
  region = var.region
}

resource "null_resource" "input_validation" {
  lifecycle {
    precondition {
      condition     = var.create_cluster || length(var.kubeconfig_path) > 0
      error_message = "kubeconfig_path must be set when create_cluster=false."
    }
    precondition {
      condition     = !var.create_cluster || length(var.node_pool_key_pair) > 0 || length(var.node_pool_login_password) > 0
      error_message = "When create_cluster=true, set either node_pool_key_pair or node_pool_login_password."
    }
    precondition {
      condition     = length(var.master_image_repository) > 0 && length(var.master_image_tag) > 0 && length(var.node_image_repository) > 0 && length(var.node_image_tag) > 0
      error_message = "master and node all-in-one image repositories and tags must be set."
    }
  }
}

data "huaweicloud_availability_zones" "this" {}

locals {
  selected_az = var.availability_zone != "" ? var.availability_zone : data.huaweicloud_availability_zones.this.names[0]
  kubeconfig_path = var.create_cluster ? (
    length(var.kubeconfig_output_path) > 0 ? var.kubeconfig_output_path : "${path.module}/.kubeconfig"
  ) : var.kubeconfig_path
  oss_enabled = length(var.oss_endpoint) > 0 && length(var.oss_access_key_id) > 0 && length(var.oss_access_key_secret) > 0 && length(var.oss_bucket) > 0
  generated_oss_auths = local.oss_enabled ? {
    "${var.oss_endpoint}/${var.oss_bucket}" = {
      access_key_id     = var.oss_access_key_id
      access_key_secret = var.oss_access_key_secret
    }
  } : {}
  oss_auths = merge(local.generated_oss_auths, var.oss_auths)
  registry_auths = {
    auths = { for host, cred in var.registry_auths : host => { username = cred.username, password = cred.password } }
  }

  # When auto-creating ELB on Huawei Cloud CCE, inject required annotations
  # so the cloud-controller-manager provisions the ELB automatically.
  huaweicloud_master_elb_annotations = var.master_public_access_8888 ? {
    "kubernetes.io/elb.class" = "union"
    "kubernetes.io/elb.autocreate" = jsonencode({
      type                 = "public"
      bandwidth_name       = "${var.cluster_name}-master-elb"
      bandwidth_chargemode = var.master_elb_bandwidth_charge_mode
      bandwidth_size       = var.master_elb_bandwidth_size
      bandwidth_sharetype  = "PER"
      eip_type             = var.master_elb_eip_type
    })
  } : {}
  huaweicloud_traefik_elb_annotations = var.traefik_public_access ? {
    "kubernetes.io/elb.class" = "union"
    "kubernetes.io/elb.autocreate" = jsonencode({
      type                 = "public"
      bandwidth_name       = "${var.cluster_name}-traefik-elb"
      bandwidth_chargemode = var.master_elb_bandwidth_charge_mode
      bandwidth_size       = var.master_elb_bandwidth_size
      bandwidth_sharetype  = "PER"
      eip_type             = var.master_elb_eip_type
    })
  } : {}
  huaweicloud_grafana_elb_annotations = var.grafana_public_access ? {
    "kubernetes.io/elb.class" = "union"
    "kubernetes.io/elb.autocreate" = jsonencode({
      type                 = "public"
      bandwidth_name       = "${var.cluster_name}-grafana-elb"
      bandwidth_chargemode = var.master_elb_bandwidth_charge_mode
      bandwidth_size       = var.master_elb_bandwidth_size
      bandwidth_sharetype  = "PER"
      eip_type             = var.master_elb_eip_type
    })
  } : {}

  node_pool_bootstrap_base = templatefile("${path.module}/../shared/node-bootstrap.sh.tftpl", {
    sandboxd_nat_backend = var.sandboxd_nat_backend
    max_user_namespaces  = var.node_pool_max_user_namespaces
  })
  node_pool_bootstrap_script = var.node_pool_extra_data_volume_enabled ? join("\n", [
    local.node_pool_bootstrap_base,
    templatefile("${path.module}/../shared/data-disk-mount.sh.tftpl", {
      mount_path = var.node_pool_extra_data_volume_mount_path
      fs_type    = var.node_pool_extra_data_volume_fs_type
    })
  ]) : local.node_pool_bootstrap_base

  dragonfly_seed_postinstall = var.dragonfly_seed_node_pool.mount_local_nvme ? join("\n", [
    local.node_pool_bootstrap_base,
    templatefile("${path.module}/../shared/local-nvme-mount.sh.tftpl", {
      mount_path = var.dragonfly_seed_node_pool.local_nvme_mount_path
    })
  ]) : local.node_pool_bootstrap_script

  core_values = templatefile("${path.module}/values-akernel.yaml.tmpl", {
    etcd_image_repository          = var.etcd_image_repository
    etcd_image_tag                 = var.etcd_image_tag
    master_image_repository        = var.master_image_repository
    master_image_tag               = var.master_image_tag
    node_image_repository          = var.node_image_repository
    node_image_tag                 = var.node_image_tag
    traefik_image_repository       = var.traefik_image_repository
    traefik_image_tag              = var.traefik_image_tag
    iam_litebus_data_key           = var.iam_litebus_data_key
    enable_kruise                  = var.install_prereqs
    master_service_type            = var.master_public_access_8888 ? var.master_service_type : "ClusterIP"
    master_service_annotations     = merge(local.huaweicloud_master_elb_annotations, var.master_service_annotations)
    master_service_loadbalancer_ip = var.master_public_access_8888 ? var.master_service_loadbalancer_ip : ""
    sandboxd_nat_backend           = var.sandboxd_nat_backend
    enable_runc                    = var.enable_runc
    node_secret_create             = var.node_secret_create
    node_home_use_csi_ephemeral    = var.node_home_use_csi_ephemeral
    node_home_csi_storage_class    = var.node_home_csi_storage_class
    node_home_csi_size             = var.node_home_csi_size
    oss_auths                      = local.oss_auths
    registry_auths                 = local.registry_auths

    etcd_cpu         = var.etcd_resources.cpu
    etcd_memory      = var.etcd_resources.memory
    etcd_ephemeral   = var.etcd_resources.ephemeral_storage
    etcd_pvc_size    = var.etcd_resources.pvc_size
    master_cpu       = var.master_resources.cpu
    master_memory    = var.master_resources.memory
    master_ephemeral = var.master_resources.ephemeral_storage
    node_ephemeral   = var.node_resources.ephemeral_storage

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

    install_traefik                 = var.install_traefik
    traefik_replicas                = var.traefik_replicas
    traefik_tcp_port                = var.traefik_tcp_port
    traefik_enable_web_entrypoint   = var.traefik_enable_web_entrypoint
    traefik_web_port                = var.traefik_web_port
    traefik_websecure_port          = var.traefik_websecure_port
    traefik_service_type            = var.traefik_service_type
    traefik_service_annotations     = local.huaweicloud_traefik_elb_annotations
    traefik_service_loadbalancer_ip = ""
    traefik_tls_enabled             = var.traefik_tls_enabled
    traefik_tls_create_secret       = var.traefik_tls_create_secret
    traefik_tls_cert                = var.traefik_tls_cert
    traefik_tls_key                 = var.traefik_tls_key
    traefik_internal_stats          = var.traefik_internal_stats_enabled
    traefik_internal_stats_image    = var.traefik_internal_stats_image
    traefik_grafana_enabled         = var.install_monitor
    traefik_grafana_url             = var.install_monitor ? "http://grafana.${var.monitor_namespace}.svc:3000" : ""
  })

  monitor_values = templatefile("${path.module}/values-monitor.yaml.tmpl", {
    image_registry              = var.monitor_image_registry
    enable_kruise               = var.install_prereqs
    monitor_storage_class       = var.monitor_storage_class
    grafana_admin_password      = var.grafana_admin_password
    grafana_public_access       = var.grafana_public_access
    grafana_service_annotations = local.huaweicloud_grafana_elb_annotations
    install_dragonfly           = var.install_dragonfly
    dragonfly_namespace         = var.dragonfly_namespace

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
    flavor_id         = var.dragonfly_server_node_pool.flavor_id
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

data "huaweicloud_cce_flavor_specifications" "this" {
  count        = var.create_cluster && var.cluster_flavor_id == "" ? 1 : 0
  cluster_type = var.cluster_type
}

data "huaweicloud_compute_flavors" "this" {
  count = var.create_cluster && var.node_flavor_id == "" ? 1 : 0

  availability_zone = local.selected_az
  # s6 (normal) flavors have subeni quota 0 and cannot be used with ENI networking.
  # Override to computingv3 (c6/c6s series) which supports ENI.
  performance_type = var.container_network_type == "eni" && var.node_flavor_performance_type == "normal" ? "computingv3" : var.node_flavor_performance_type
  cpu_core_count   = var.node_flavor_cpu
  memory_size      = var.node_flavor_memory
}

locals {
  selected_cluster_flavor_id = var.cluster_flavor_id != "" ? var.cluster_flavor_id : try(data.huaweicloud_cce_flavor_specifications.this[0].cluster_flavor_specs[0].name, "cce.s2.small")
  selected_node_flavor_id    = var.node_flavor_id != "" ? var.node_flavor_id : data.huaweicloud_compute_flavors.this[0].flavors[0].id
}

resource "huaweicloud_vpc" "this" {
  count = var.create_cluster ? 1 : 0

  name = var.vpc_name
  cidr = var.vpc_cidr

  depends_on = [null_resource.input_validation]
}

resource "huaweicloud_vpc_subnet" "this" {
  count = var.create_cluster ? 1 : 0

  name              = var.subnet_name
  cidr              = var.subnet_cidr
  gateway_ip        = cidrhost(var.subnet_cidr, 1)
  vpc_id            = huaweicloud_vpc.this[0].id
  availability_zone = local.selected_az
}

resource "huaweicloud_vpc_subnet" "eni" {
  count = var.create_cluster && var.container_network_type == "eni" ? 1 : 0

  name              = var.eni_subnet_name
  cidr              = var.eni_subnet_cidr
  gateway_ip        = cidrhost(var.eni_subnet_cidr, 1)
  vpc_id            = huaweicloud_vpc.this[0].id
  availability_zone = local.selected_az
}

resource "huaweicloud_networking_secgroup" "this" {
  count = var.create_cluster ? 1 : 0

  name                 = var.security_group_name
  delete_default_rules = true
}

resource "huaweicloud_networking_secgroup_rule" "ssh" {
  for_each = var.create_cluster ? toset(var.allowed_ssh_cidrs) : toset([])

  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = each.value
  security_group_id = huaweicloud_networking_secgroup.this[0].id
}

resource "huaweicloud_networking_secgroup_rule" "internal_all" {
  count = var.create_cluster ? 1 : 0

  direction         = "ingress"
  ethertype         = "IPv4"
  remote_ip_prefix  = var.vpc_cidr
  security_group_id = huaweicloud_networking_secgroup.this[0].id
}

resource "huaweicloud_networking_secgroup_rule" "egress_all" {
  count = var.create_cluster ? 1 : 0

  direction         = "egress"
  ethertype         = "IPv4"
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = huaweicloud_networking_secgroup.this[0].id
}

resource "huaweicloud_vpc_eip" "cluster_api" {
  count = var.create_cluster && var.cluster_api_public_access ? 1 : 0

  publicip {
    type = var.cluster_api_eip_type
  }

  bandwidth {
    name        = "${var.cluster_name}-api-eip-bw"
    size        = var.cluster_api_eip_bandwidth_size
    share_type  = var.cluster_api_eip_bandwidth_share_type
    charge_mode = var.cluster_api_eip_bandwidth_charge_mode
  }
}

resource "huaweicloud_vpc_eip" "node_subnet_snat" {
  count = var.create_cluster && var.node_subnet_enable_snat ? 1 : 0

  publicip {
    type = var.node_subnet_eip_type
  }

  bandwidth {
    name        = "${var.cluster_name}-node-snat-eip-bw"
    size        = var.node_subnet_eip_bandwidth_size
    share_type  = var.node_subnet_eip_bandwidth_share_type
    charge_mode = var.node_subnet_eip_bandwidth_charge_mode
  }
}

resource "huaweicloud_nat_gateway" "node_subnet" {
  count = var.create_cluster && var.node_subnet_enable_snat ? 1 : 0

  name      = "${var.cluster_name}-node-nat"
  spec      = var.node_subnet_nat_spec
  vpc_id    = huaweicloud_vpc.this[0].id
  subnet_id = huaweicloud_vpc_subnet.this[0].id
}

resource "huaweicloud_nat_snat_rule" "node_subnet" {
  count = var.create_cluster && var.node_subnet_enable_snat ? 1 : 0

  nat_gateway_id = huaweicloud_nat_gateway.node_subnet[0].id
  floating_ip_id = huaweicloud_vpc_eip.node_subnet_snat[0].id
  subnet_id      = huaweicloud_vpc_subnet.this[0].id
}

resource "huaweicloud_vpc_eip" "pod_subnet_snat" {
  count = var.create_cluster && var.node_subnet_enable_snat && var.pod_subnet_enable_snat && var.container_network_type == "eni" ? 1 : 0

  publicip {
    type = var.node_subnet_eip_type
  }

  bandwidth {
    name        = "${var.cluster_name}-pod-snat-eip-bw"
    size        = var.pod_subnet_eip_bandwidth_size
    share_type  = var.node_subnet_eip_bandwidth_share_type
    charge_mode = var.node_subnet_eip_bandwidth_charge_mode
  }
}

resource "huaweicloud_nat_snat_rule" "pod_subnet" {
  count = var.create_cluster && var.node_subnet_enable_snat && var.pod_subnet_enable_snat && var.container_network_type == "eni" ? 1 : 0

  nat_gateway_id = huaweicloud_nat_gateway.node_subnet[0].id
  floating_ip_id = huaweicloud_vpc_eip.pod_subnet_snat[0].id
  subnet_id      = huaweicloud_vpc_subnet.eni[0].id
}

resource "huaweicloud_cce_cluster" "this" {
  count = var.create_cluster ? 1 : 0

  name                   = var.cluster_name
  cluster_type           = var.cluster_type
  flavor_id              = local.selected_cluster_flavor_id
  vpc_id                 = huaweicloud_vpc.this[0].id
  subnet_id              = huaweicloud_vpc_subnet.this[0].id
  container_network_type = var.container_network_type
  cluster_version        = var.k8s_version
  container_network_cidr = var.container_network_type != "eni" ? var.pod_cidr : null
  service_network_cidr   = var.service_cidr
  multi_az               = var.multi_az
  eip                    = var.cluster_api_public_access ? huaweicloud_vpc_eip.cluster_api[0].address : null

  # CCE Turbo ENI networking
  eni_subnet_id   = var.container_network_type == "eni" ? huaweicloud_vpc_subnet.eni[0].ipv4_subnet_id : null
  eni_subnet_cidr = var.container_network_type == "eni" ? var.eni_subnet_cidr : null
}

resource "huaweicloud_cce_node_pool" "default" {
  count = var.create_cluster ? 1 : 0
  lifecycle {
    # Huawei CCE does not allow updating postinstall on an existing node pool.
    # Keep this ignored so removing old bootstrap scripts won't trigger update failures.
    ignore_changes = [postinstall]
  }

  cluster_id         = huaweicloud_cce_cluster.this[0].id
  name               = "${var.cluster_name}-default"
  flavor_id          = local.selected_node_flavor_id
  initial_node_count = var.node_pool_size
  availability_zone  = local.selected_az
  os                 = var.node_pool_os
  key_pair           = length(var.node_pool_key_pair) > 0 ? var.node_pool_key_pair : null
  password           = length(var.node_pool_key_pair) > 0 ? null : (length(var.node_pool_login_password) > 0 ? var.node_pool_login_password : null)

  min_node_count = var.node_pool_min_size
  max_node_count = var.node_pool_max_size

  subnet_id                = huaweicloud_vpc_subnet.this[0].id
  security_groups          = [huaweicloud_networking_secgroup.this[0].id]
  max_pods                 = var.node_pool_max_pods
  scale_down_cooldown_time = var.node_pool_scale_down_cooldown_time

  root_volume {
    size       = var.node_pool_root_volume_size
    volumetype = var.node_pool_root_volume_type
  }

  dynamic "data_volumes" {
    for_each = var.node_pool_data_volume_enabled ? [1] : []
    content {
      size       = var.node_pool_data_volume_size
      volumetype = var.node_pool_data_volume_type
    }
  }
  dynamic "data_volumes" {
    for_each = var.node_pool_extra_data_volume_enabled ? [1] : []
    content {
      size       = var.node_pool_extra_data_volume_size
      volumetype = var.node_pool_extra_data_volume_type
    }
  }

  postinstall = local.node_pool_bootstrap_script
}

resource "huaweicloud_cce_node_pool" "extra" {
  for_each = {
    for pool in local.all_extra_node_pools : pool.name => pool
    if var.create_cluster
  }

  cluster_id         = huaweicloud_cce_cluster.this[0].id
  name               = "${var.cluster_name}-${each.key}"
  flavor_id          = coalesce(each.value.flavor_id, local.selected_node_flavor_id)
  initial_node_count = each.value.size
  availability_zone  = local.selected_az
  os                 = var.node_pool_os
  key_pair           = length(var.node_pool_key_pair) > 0 ? var.node_pool_key_pair : null
  password           = length(var.node_pool_key_pair) > 0 ? null : (length(var.node_pool_login_password) > 0 ? var.node_pool_login_password : null)

  min_node_count           = 0
  max_node_count           = each.value.size
  subnet_id                = huaweicloud_vpc_subnet.this[0].id
  security_groups          = [huaweicloud_networking_secgroup.this[0].id]
  max_pods                 = var.node_pool_max_pods
  scale_down_cooldown_time = var.node_pool_scale_down_cooldown_time

  root_volume {
    size       = coalesce(each.value.system_disk_size, var.node_pool_root_volume_size)
    volumetype = var.node_pool_root_volume_type
  }

  dynamic "data_volumes" {
    for_each = coalesce(each.value.data_disk_enabled, true) ? [1] : []
    content {
      size       = coalesce(each.value.data_disk_size, var.node_pool_data_volume_size)
      volumetype = var.node_pool_data_volume_type
    }
  }

  labels = each.value.labels

  dynamic "taints" {
    for_each = each.value.taints
    content {
      key    = taints.value.key
      value  = taints.value.value
      effect = taints.value.effect
    }
  }
}

resource "huaweicloud_cce_node_pool" "dragonfly_seed" {
  count = var.install_dragonfly && var.create_cluster && var.dragonfly_seed_node_pool.enabled ? 1 : 0

  cluster_id         = huaweicloud_cce_cluster.this[0].id
  name               = "${var.cluster_name}-${var.dragonfly_seed_node_pool.name}"
  flavor_id          = coalesce(var.dragonfly_seed_node_pool.flavor_id, local.selected_node_flavor_id)
  initial_node_count = var.dragonfly_seed_node_pool.size
  availability_zone  = local.selected_az
  os                 = var.node_pool_os
  key_pair           = length(var.node_pool_key_pair) > 0 ? var.node_pool_key_pair : null
  password           = length(var.node_pool_key_pair) > 0 ? null : (length(var.node_pool_login_password) > 0 ? var.node_pool_login_password : null)

  min_node_count           = var.dragonfly_seed_node_pool.min_size
  max_node_count           = var.dragonfly_seed_node_pool.max_size
  subnet_id                = huaweicloud_vpc_subnet.this[0].id
  security_groups          = [huaweicloud_networking_secgroup.this[0].id]
  max_pods                 = var.node_pool_max_pods
  scale_down_cooldown_time = var.node_pool_scale_down_cooldown_time

  root_volume {
    size       = coalesce(var.dragonfly_seed_node_pool.system_disk_size, var.node_pool_root_volume_size)
    volumetype = var.node_pool_root_volume_type
  }

  data_volumes {
    size       = var.dragonfly_seed_node_pool.data_disk_size
    volumetype = var.node_pool_data_volume_type
  }

  labels = {
    (var.dragonfly_seed_node_pool.node_label_key) = var.dragonfly_seed_node_pool.node_label_value
  }

  taints {
    key    = var.dragonfly_seed_node_pool.taint_key
    value  = var.dragonfly_seed_node_pool.taint_value
    effect = var.dragonfly_seed_node_pool.taint_effect
  }

  postinstall = local.dragonfly_seed_postinstall
}

data "huaweicloud_cce_cluster_certificate" "this" {
  count = var.create_cluster ? 1 : 0

  cluster_id = huaweicloud_cce_cluster.this[0].id
  duration   = var.kubeconfig_duration
}

locals {
  kubeconfig_raw_internal       = var.create_cluster ? data.huaweicloud_cce_cluster_certificate.this[0].kube_config_raw : ""
  kubeconfig_server_line        = can(regex("server:\\s+https://[^\\n]+", local.kubeconfig_raw_internal)) ? regex("server:\\s+https://[^\\n]+", local.kubeconfig_raw_internal) : ""
  kubeconfig_public_server_line = var.create_cluster && var.cluster_api_public_access ? "server: https://${huaweicloud_vpc_eip.cluster_api[0].address}:5443" : ""
  kubeconfig_raw = var.create_cluster && var.cluster_api_public_access && local.kubeconfig_server_line != "" ? replace(
    local.kubeconfig_raw_internal,
    local.kubeconfig_server_line,
    local.kubeconfig_public_server_line
  ) : local.kubeconfig_raw_internal
}

resource "local_sensitive_file" "kubeconfig" {
  count = var.create_cluster ? 1 : 0

  filename = local.kubeconfig_path
  content  = local.kubeconfig_raw
}

provider "helm" {
  kubernetes {
    config_path = local.kubeconfig_path
  }
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

  depends_on = [local_sensitive_file.kubeconfig, huaweicloud_cce_node_pool.default, huaweicloud_nat_snat_rule.node_subnet, huaweicloud_nat_snat_rule.pod_subnet]
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
  atomic           = false
  cleanup_on_fail  = false

  version = var.prereq_kruise_chart_version

  depends_on = [local_sensitive_file.kubeconfig, huaweicloud_cce_node_pool.default, huaweicloud_nat_snat_rule.node_subnet, huaweicloud_nat_snat_rule.pod_subnet, null_resource.ensure_prereq_namespace]
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
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.core_namespace}" meta.helm.sh/release-name=akernel-core --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.core_namespace}" meta.helm.sh/release-namespace="${var.core_namespace}" --overwrite
    EOT
  }

  depends_on = [local_sensitive_file.kubeconfig, huaweicloud_cce_node_pool.default, huaweicloud_nat_snat_rule.node_subnet, huaweicloud_nat_snat_rule.pod_subnet]
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
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.monitor_namespace}" meta.helm.sh/release-name=akernel-monitor --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.monitor_namespace}" meta.helm.sh/release-namespace="${var.monitor_namespace}" --overwrite
    EOT
  }

  depends_on = [local_sensitive_file.kubeconfig, huaweicloud_cce_node_pool.default, huaweicloud_nat_snat_rule.node_subnet, huaweicloud_nat_snat_rule.pod_subnet]
}

# On re-apply, OpenKruise webhooks may exist from a previous run but the
# kruise-manager pods may be unreachable (crash, node drain, etc.). The API
# server still routes DaemonSet/StatefulSet mutations through these stale
# webhooks, causing "no route to host" errors. This resource waits for the
# kruise-controller-manager to become ready; if it doesn't recover in time,
# the stale webhook configurations are removed so they don't block subsequent
# Helm installs.
resource "null_resource" "ensure_kruise_webhooks_healthy" {
  count = var.install_prereqs ? 1 : 0

  triggers = {
    kubeconfig_path  = local.kubeconfig_path
    prereq_namespace = var.prereq_namespace
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      export KUBECONFIG="${local.kubeconfig_path}"

      # Check if any kruise mutating/validating webhooks exist
      if ! kubectl get mutatingwebhookconfiguration -l app.kubernetes.io/name=kruise -o name 2>/dev/null | grep -q .; then
        echo "No kruise webhooks found, skipping health check."
        exit 0
      fi

      # Wait for kruise-controller-manager to be ready (up to 120s)
      if kubectl rollout status deployment kruise-controller-manager \
           -n "${var.prereq_namespace}" --timeout=120s 2>/dev/null; then
        echo "kruise-controller-manager is ready."
      else
        echo "WARNING: kruise-controller-manager not ready after 120s, removing stale webhooks to unblock deployment."
        kubectl delete mutatingwebhookconfiguration -l app.kubernetes.io/name=kruise --ignore-not-found || true
        kubectl delete validatingwebhookconfiguration -l app.kubernetes.io/name=kruise --ignore-not-found || true
      fi
    EOT
  }

  depends_on = [helm_release.prereq_openkruise]
}

# If a previous "terraform apply" failed mid-install, the Helm release may
# exist in Terraform state but not in the cluster.  On the next apply the
# Helm provider tries to uninstall the ghost release and fails with
# "release: not found".  Cleaning up orphaned Helm release secrets
# (where Helm stores its metadata) before the helm_release resource runs
# lets the provider do a fresh install instead.
resource "null_resource" "cleanup_orphaned_helm_releases" {
  triggers = {
    kubeconfig_path   = local.kubeconfig_path
    core_namespace    = var.core_namespace
    monitor_namespace = var.monitor_namespace
    install_monitor   = tostring(var.install_monitor)
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      export KUBECONFIG="${local.kubeconfig_path}"

      # akernel-core
      if ! helm status akernel-core -n "${var.core_namespace}" >/dev/null 2>&1; then
        kubectl delete secret -n "${var.core_namespace}" \
          -l "owner=helm,name=akernel-core" --ignore-not-found || true
      fi

      # akernel-monitor
      if [ "${var.install_monitor}" = "true" ]; then
        if ! helm status akernel-monitor -n "${var.monitor_namespace}" >/dev/null 2>&1; then
          kubectl delete secret -n "${var.monitor_namespace}" \
            -l "owner=helm,name=akernel-monitor" --ignore-not-found || true
        fi
      fi
    EOT
  }

  depends_on = [null_resource.ensure_core_namespace, null_resource.ensure_monitor_namespace]
}

resource "helm_release" "akernel_core" {
  name             = "akernel-core"
  chart            = "${path.module}/../../akernel/charts/core"
  namespace        = var.core_namespace
  create_namespace = false
  timeout          = 900

  values = concat([local.core_values], [for f in var.core_values_override_files : file(f)])

  # Force Terraform to detect changes in chart files (templates, configs, etc.)
  set {
    name  = "chartContentHash"
    value = sha256(join("", [for f in fileset("${path.module}/../../akernel/charts/core", "**") : filesha256("${path.module}/../../akernel/charts/core/${f}")]))
  }

  depends_on = [local_sensitive_file.kubeconfig, helm_release.prereq_openkruise, null_resource.ensure_core_namespace, null_resource.ensure_kruise_webhooks_healthy, null_resource.cleanup_orphaned_helm_releases]
}

resource "helm_release" "akernel_monitor" {
  count = var.install_monitor ? 1 : 0

  name             = "akernel-monitor"
  chart            = "${path.module}/../../akernel/charts/monitor"
  namespace        = var.monitor_namespace
  create_namespace = false
  timeout          = 900

  values = concat([local.monitor_values], [for f in var.monitor_values_override_files : file(f)])

  # Force Terraform to detect changes in chart files (dashboards, templates, etc.)
  set {
    name  = "chartContentHash"
    value = sha256(join("", [for f in fileset("${path.module}/../../akernel/charts/monitor", "**") : filesha256("${path.module}/../../akernel/charts/monitor/${f}")]))
  }

  depends_on = [local_sensitive_file.kubeconfig, helm_release.prereq_openkruise, null_resource.ensure_monitor_namespace, null_resource.ensure_kruise_webhooks_healthy, null_resource.cleanup_orphaned_helm_releases]
}

# During "terraform destroy", this resource depends on the akernel helm releases,
# so Terraform destroys it BEFORE them (reverse dependency order). The destroy-time
# provisioner removes OpenKruise webhooks and CRD finalizers, which unblocks:
#   1. akernel_core / akernel_monitor uninstall (no webhook timeouts)
#   2. prereq_openkruise uninstall (no CRD finalizer hangs)
#
# Destroy order: cleanup → akernel_core/akernel_monitor → prereq_openkruise → infra
resource "null_resource" "cleanup_openkruise_before_destroy" {
  count = var.install_prereqs ? 1 : 0

  triggers = {
    kubeconfig_path = local.kubeconfig_path
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    when        = destroy
    command     = <<-EOT
      set -euo pipefail
      export KUBECONFIG="${self.triggers.kubeconfig_path}"

      # Remove webhook configurations so the API server stops routing to the
      # (potentially unreachable) kruise-manager pods. Without this, every
      # delete of a kruise-managed resource (DaemonSet, StatefulSet, etc.)
      # blocks for 30s per webhook call.
      kubectl delete validatingwebhookconfiguration -l app.kubernetes.io/name=kruise --ignore-not-found || true
      kubectl delete mutatingwebhookconfiguration -l app.kubernetes.io/name=kruise --ignore-not-found || true

      # Strip finalizers from OpenKruise CRDs so they can be deleted without
      # the controller running.
      for crd in $(kubectl get crd -o name 2>/dev/null | grep kruise.io || true); do
        kubectl patch "$crd" -p '{"metadata":{"finalizers":null}}' --type=merge || true
      done
    EOT
  }

  # This resource must depend on the helm releases that use OpenKruise resources.
  # In Terraform destroy, dependencies are reversed: dependents are destroyed first.
  # So this cleanup will run BEFORE the helm releases are uninstalled.
  depends_on = [helm_release.akernel_core, helm_release.akernel_monitor]
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
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.dragonfly_namespace}" meta.helm.sh/release-name=dragonfly --overwrite
      KUBECONFIG="${local.kubeconfig_path}" kubectl annotate namespace "${var.dragonfly_namespace}" meta.helm.sh/release-namespace="${var.dragonfly_namespace}" --overwrite
    EOT
  }

  depends_on = [local_sensitive_file.kubeconfig, huaweicloud_cce_node_pool.default, huaweicloud_cce_node_pool.extra, huaweicloud_cce_node_pool.dragonfly_seed, huaweicloud_nat_snat_rule.node_subnet, huaweicloud_nat_snat_rule.pod_subnet]
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

  depends_on = [local_sensitive_file.kubeconfig, null_resource.ensure_dragonfly_namespace, huaweicloud_cce_node_pool.extra, huaweicloud_cce_node_pool.dragonfly_seed]
}
