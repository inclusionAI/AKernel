# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

variable "region" {
  type        = string
  description = "Aliyun region ID, e.g. cn-hangzhou."
}

variable "acr_region" {
  type        = string
  description = "ACR region for pulling images, e.g. cn-hangzhou. Defaults to var.region."
  default     = ""
}

variable "acr_use_vpc" {
  type        = bool
  description = "Use ACR VPC endpoint (registry-vpc) for faster in-VPC pulls. Defaults to true when acr_region matches cluster region."
  default     = null
}

variable "acr_namespace" {
  type        = string
  description = "ACR namespace (repository group), e.g. akernel. Ignored when acr_registry_url is set."
  default     = "akernel"
}

variable "acr_registry_url" {
  type        = string
  description = "Full ACR enterprise registry URL, e.g. myinstance-registry-vpc.example.cr.aliyuncs.com/akernel. When set, acr_region/acr_use_vpc/acr_namespace are ignored."
  default     = ""
}

variable "acr_username" {
  type        = string
  description = "ACR username for imagePullSecret. Required for enterprise ACR."
  default     = ""
  sensitive   = true
}

variable "acr_password" {
  type        = string
  description = "ACR password for imagePullSecret. Required for enterprise ACR."
  default     = ""
  sensitive   = true
}

variable "akernel_env" {
  type        = string
  description = "Environment label for akernel monitoring (AKERNEL_ENV). Defaults to cluster_name if empty."
  default     = ""
}

variable "cluster_name" {
  type        = string
  description = "ACK cluster name."
  default     = "akernel-ack"
}

variable "create_cluster" {
  type        = bool
  description = "Whether to create ACK cluster and node pool. If false, use kubeconfig_path for an existing cluster."
  default     = true
}

variable "kubeconfig_path" {
  type        = string
  description = "Path to kubeconfig for an existing cluster when create_cluster=false."
  default     = ""
}

variable "kubeconfig_output_path" {
  type        = string
  description = "Path to write the generated kubeconfig when create_cluster=true. Defaults to .kubeconfig under this Terraform module."
  default     = ""
}

variable "tags" {
  type        = map(string)
  description = "Tags to apply to all created resources (VPC, vSwitches, ACK cluster, node pools). Helps identify and protect Terraform-managed resources."
  default     = {}
}

variable "vpc_id" {
  type        = string
  description = "Existing VPC ID to use. When set, vpc_cidr is ignored and no VPC is created."
  default     = ""
}

variable "existing_vswitch_ids" {
  type        = list(string)
  description = "Existing vSwitch IDs to reuse (pass-through, no describe API calls). When set, skips vSwitch creation. Requires vpc_id."
  default     = []
}

variable "security_group_id" {
  type        = string
  description = "Existing security group ID for ACK cluster. When empty, ACK auto-creates a default security group."
  default     = ""
}

variable "node_pool_security_group_ids" {
  type        = list(string)
  description = "Security group IDs for node pool instances (max 5). When empty, uses the cluster's security group."
  default     = []
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR block. Ignored when vpc_id is set."
  default     = "172.16.0.0/12"
}

variable "vswitch_cidrs" {
  type        = list(string)
  description = "CIDR blocks for vswitches. Ignored when vswitch_ids is set. Length should match zone_ids."
  default     = ["172.16.0.0/20", "172.16.16.0/20", "172.16.32.0/20"]
}

variable "zone_ids" {
  type        = list(string)
  description = "Zone IDs for vswitches, e.g. [\"cn-hangzhou-i\", \"cn-hangzhou-j\"]. Length should match vswitch_cidrs."
}

variable "k8s_version" {
  type        = string
  description = "ACK Kubernetes version."
  default     = "1.35.2-aliyun.1"
}

variable "network_addon" {
  type        = string
  description = "ACK network addon. Supported values: flannel, terway-eni, terway-eniip."
  default     = "terway-eniip"

  validation {
    condition     = contains(["flannel", "terway-eni", "terway-eniip"], var.network_addon)
    error_message = "network_addon must be one of: flannel, terway-eni, terway-eniip."
  }
}

variable "pod_cidr" {
  type        = string
  description = "Pod CIDR. Used when network_addon=flannel."
  default     = "10.10.0.0/16"
}

variable "pod_vswitch_ids" {
  type        = list(string)
  description = "Pod vSwitch IDs for Terway network addons. Empty means reusing created worker vSwitches when create_cluster=true."
  default     = []
}

variable "service_cidr" {
  type        = string
  description = "Service CIDR."
  default     = "10.20.0.0/20"
}

variable "api_server_public_access" {
  type        = bool
  description = "Whether to enable ACK API server public endpoint for access outside VPC."
  default     = true
}

variable "auto_authorize_oos_lifecycle_role" {
  type        = bool
  description = "Whether to create/authorize AliyunOOSLifecycleHook4CSRole automatically for ACK node pool lifecycle hooks."
  default     = true
}

variable "node_pool_exclusive_eni" {
  type        = bool
  description = "Whether to enable exclusive ENI mode for the node pool (each Pod gets a dedicated ENI)."
  default     = true
}

variable "node_pool_instance_types" {
  type        = list(string)
  description = "ACK node pool instance types."
  default     = ["ecs.c7.3xlarge"]
}

variable "node_pool_image_type" {
  type        = string
  description = "ACK node pool OS image type."
  default     = "AliyunLinux3"
}

variable "node_pool_image_id" {
  type        = string
  description = "Optional ACK node pool image ID. When set, image_type is ignored."
  default     = ""
}

variable "node_pool_zone_id" {
  type        = string
  description = "Optional zone for node pool placement. Empty means using the first zone in zone_ids."
  default     = ""
}

variable "node_pool_size" {
  type        = number
  description = "Desired node count in the default node pool."
  default     = 3
}

variable "node_pool_system_disk_category" {
  type        = string
  description = "System disk category for nodes."
  default     = "cloud_essd"
}

variable "node_pool_system_disk_size" {
  type        = number
  description = "System disk size (GiB) for nodes."
  default     = 300
}

variable "node_pool_data_disk_enabled" {
  type        = bool
  description = "Whether to attach a data disk to ACK node pool instances."
  default     = true
}

variable "node_pool_data_disk_category" {
  type        = string
  description = "Data disk category for nodes."
  default     = "cloud_essd"
}

variable "node_pool_data_disk_size" {
  type        = number
  description = "Data disk size (GiB) for nodes."
  default     = 300
}

variable "node_pool_extra_data_disk_enabled" {
  type        = bool
  description = "Whether to attach and mount a dedicated data disk for AKernel hostPath storage, separate from the container runtime data disk."
  default     = true
}

variable "node_pool_extra_data_disk_category" {
  type        = string
  description = "Extra data disk category."
  default     = "cloud_essd"
}

variable "node_pool_extra_data_disk_size" {
  type        = number
  description = "Extra data disk size (GiB)."
  default     = 300
}

variable "node_pool_extra_data_disk_mount_path" {
  type        = string
  description = "Path where ACK formats and mounts the dedicated AKernel data disk."
  default     = "/home/akernel"

  validation {
    condition     = startswith(var.node_pool_extra_data_disk_mount_path, "/") && var.node_pool_extra_data_disk_mount_path != "/"
    error_message = "node_pool_extra_data_disk_mount_path must be an absolute path other than /."
  }
}

variable "node_pool_extra_data_disk_fs_type" {
  type        = string
  description = "Filesystem type for the dedicated AKernel data disk. XFS enables the high-performance reflink checkpoint path; ext4 is retained for compatibility."
  default     = "xfs"

  validation {
    condition     = contains(["ext4", "xfs"], var.node_pool_extra_data_disk_fs_type)
    error_message = "node_pool_extra_data_disk_fs_type must be ext4 or xfs."
  }
}

variable "node_storage_init_image" {
  type        = string
  description = "Deprecated compatibility variable. ACK now formats and mounts the dedicated data disk before joining the node."
  default     = "busybox:1.36"
}

variable "node_pool_login_password" {
  type        = string
  description = "Login password for nodes (ignored if key_name is set)."
  default     = ""
  sensitive   = true
}

variable "node_pool_key_name" {
  type        = string
  description = "SSH key name for nodes."
  default     = ""
}

variable "node_pool_max_user_namespaces" {
  type        = number
  description = "Value for sysctl user.max_user_namespaces on each node. Required by gVisor/runsc."
  default     = 65536
}

variable "extra_node_pools" {
  type = list(object({
    name              = string
    size              = number
    instance_types    = optional(list(string))
    system_disk_size  = optional(number)
    data_disk_enabled = optional(bool, true)
    data_disk_size    = optional(number)
    labels            = optional(map(string), {})
    taints = optional(list(object({
      key    = string
      value  = string
      effect = string
    })), [])
  }))
  description = "Additional node pools with custom instance types, labels, and taints."
  default     = []
}

variable "sandboxd_nat_backend" {
  type        = string
  description = "Sandboxd NAT backend. When set to 'iptables', ip_tables kernel module will be loaded at boot."
  default     = "iptables"
}

variable "enable_runc" {
  type        = bool
  description = "Request the optional runc runtime; the selected node image must be built with AKERNEL_ENABLE_RUNC=true."
  default     = false
}

variable "node_home_use_csi_ephemeral" {
  type        = bool
  description = "Mount /home/akernel from a per-node CSI ephemeral volume instead of hostPath /home/akernel."
  default     = false
}

variable "node_home_csi_storage_class" {
  type        = string
  description = "StorageClass for node /home/akernel CSI ephemeral volume."
  default     = ""
}

variable "node_home_csi_size" {
  type        = string
  description = "Requested size for node /home/akernel CSI ephemeral volume."
  default     = "300Gi"
}

variable "core_namespace" {
  type        = string
  description = "Namespace for akernel core release."
  default     = "akernel"
}

variable "iam_litebus_data_key" {
  type        = string
  description = "Hex-encoded IAM JWT signing seed passed to the core chart as auth.litebusDataKey. Leave empty to let the chart generate or reuse a seed."
  default     = ""
  sensitive   = true
}

variable "etcd_image_repository" {
  type        = string
  description = "Image repository for etcd."
  default     = ""
}

variable "etcd_image_tag" {
  type        = string
  description = "Image tag for etcd."
  default     = "3.6.8"
}

variable "master_image_repository" {
  type        = string
  description = "All-in-one image repository for akernel-master. Defaults to '<acr_registry>/all-in-one'."
  default     = ""
}

variable "master_image_tag" {
  type        = string
  description = "Image tag for akernel-master."
  default     = ""
}

variable "master_replicas" {
  type        = number
  description = "Number of master replicas (for HA when frontend is enabled)."
  default     = 1
}

variable "frontend_enabled" {
  type        = bool
  description = "Whether to enable frontend Deployment (splits from master for independent scaling)."
  default     = true
}

variable "frontend_replicas" {
  type        = number
  description = "Number of frontend replicas."
  default     = 1
}

variable "frontend_cpu" {
  type        = string
  description = "CPU request/limit for frontend pods."
  default     = "1"
}

variable "frontend_memory" {
  type        = string
  description = "Memory request/limit for frontend pods."
  default     = "2Gi"
}

variable "node_image_repository" {
  type        = string
  description = "All-in-one image repository for akernel-node. Defaults to '<acr_registry>/all-in-one'."
  default     = ""
}

variable "node_image_tag" {
  type        = string
  description = "Image tag for akernel-node."
  default     = ""
}

variable "traefik_image_repository" {
  type        = string
  description = "Image repository for Traefik. Empty uses the official traefik image."
  default     = ""
}

variable "traefik_image_tag" {
  type        = string
  description = "Image tag for Traefik."
  default     = "v3.6.8"
}

variable "monitor_image_registry" {
  type        = string
  description = "Optional mirror registry prefix for Grafana, Prometheus, Loki, Tempo, and BusyBox. Empty uses their official public images."
  default     = ""
}

variable "dragonfly_image_registry" {
  type        = string
  description = "Global image registry for Dragonfly components (maps to global.imageRegistry in Dragonfly Helm chart)."
  default     = ""
}

variable "dragonfly_manager_image_tag" {
  type        = string
  description = "Image tag for Dragonfly manager. Empty means chart default."
  default     = ""
}

variable "dragonfly_scheduler_image_tag" {
  type        = string
  description = "Image tag for Dragonfly scheduler. Empty means chart default."
  default     = ""
}

variable "dragonfly_seed_client_image_tag" {
  type        = string
  description = "Image tag for Dragonfly seed-client. Empty means chart default."
  default     = ""
}

variable "dragonfly_client_image_tag" {
  type        = string
  description = "Image tag for Dragonfly client. Empty means chart default."
  default     = ""
}

variable "dragonfly_injector_image_tag" {
  type        = string
  description = "Image tag for Dragonfly injector. Empty means chart default."
  default     = ""
}

variable "dragonfly_dfinit_image_tag" {
  type        = string
  description = "Image tag for Dragonfly dfinit. Empty means chart default."
  default     = ""
}

variable "master_service_type" {
  type        = string
  description = "Service type for akernel-master. Use 'LoadBalancer' for public access (ACK auto-creates SLB)."
  default     = "LoadBalancer"
}

variable "master_public_access_8888" {
  type        = bool
  description = "Whether to expose akernel-master port 8888 publicly. Ignored (forced ClusterIP) when traefik_enabled=true."
  default     = false
}


variable "traefik_enabled" {
  type        = bool
  description = "Whether to deploy Traefik as the cluster ingress. When true, akernel-master is kept ClusterIP and Traefik exposes port 8888 via LoadBalancer."
  default     = true
}

variable "install_traefik" {
  type        = bool
  description = "Whether to enable Traefik ingress controller in the core chart."
  default     = true
}

variable "traefik_replicas" {
  type        = number
  description = "Number of Traefik replicas."
  default     = 1
}

variable "traefik_tcp_port" {
  type        = number
  description = "TCP port for Traefik websecure entrypoint (legacy single-entrypoint mode; ignored when traefik_enable_web_entrypoint=true)."
  default     = 8888
}

variable "traefik_enable_web_entrypoint" {
  type        = bool
  description = "Enable dual entrypoints: 'websecure' (TLS, frontend API) on traefik_websecure_port and 'web' (plain HTTP, port forwarding) on traefik_web_port. When false, falls back to legacy single-entrypoint mode using traefik_tcp_port."
  default     = true
}

variable "traefik_web_port" {
  type        = number
  description = "Port for Traefik 'web' (plain HTTP) entrypoint. Only used when traefik_enable_web_entrypoint=true."
  default     = 80
}

variable "traefik_websecure_port" {
  type        = number
  description = "Port for Traefik 'websecure' (TLS) entrypoint. Only used when traefik_enable_web_entrypoint=true."
  default     = 443
}

variable "traefik_service_type" {
  type        = string
  description = "Service type for Traefik. Use 'LoadBalancer' for cloud deployments."
  default     = "LoadBalancer"
}

variable "traefik_tls_enabled" {
  type        = bool
  description = "Whether to mount a custom default certificate for Traefik. The websecure router still serves TLS when this is false."
  default     = false
}

variable "traefik_tls_cert" {
  type        = string
  description = "TLS certificate content (PEM) for Traefik. Only used when traefik_tls_enabled=true and traefik_tls_create_secret=true."
  default     = ""
  sensitive   = true
}

variable "traefik_tls_key" {
  type        = string
  description = "TLS private key content (PEM) for Traefik. Only used when traefik_tls_enabled=true and traefik_tls_create_secret=true."
  default     = ""
  sensitive   = true
}

variable "traefik_tls_create_secret" {
  type        = bool
  description = "Whether to create TLS secret from traefik_tls_cert/traefik_tls_key."
  default     = false
}

variable "traefik_service_annotations" {
  type        = map(string)
  description = "Extra annotations for Traefik LoadBalancer Service."
  default     = {}
}

variable "traefik_internal_stats_enabled" {
  type        = bool
  description = "Whether to enable the /internal-stats endpoint on Traefik."
  default     = true
}

variable "traefik_internal_stats_image" {
  type        = string
  description = "Optional BusyBox image for the Traefik /internal-stats sidecar. Empty uses the deployment ACR VPC endpoint."
  default     = ""
}

variable "monitor_namespace" {
  type        = string
  description = "Namespace for monitor resources."
  default     = "akernel-monitor"
}

variable "install_monitor" {
  type        = bool
  description = "Whether to install akernel monitor chart."
  default     = false
}

variable "storage_class" {
  type        = string
  description = "Default StorageClass for all PVCs (etcd, monitor, node homeDisk, dragonfly). Uses WaitForFirstConsumer binding mode to avoid cross-zone PVC/Pod scheduling issues."
  default     = "alicloud-disk-essd-wffc"
}

variable "create_storage_class" {
  type        = bool
  description = "Whether to create the WaitForFirstConsumer StorageClass. Set false if it already exists."
  default     = true
}

variable "monitor_storage_class" {
  type        = string
  description = "StorageClass for Grafana/Prometheus PVCs. Defaults to var.storage_class."
  default     = ""
}

variable "dragonfly_storage_class" {
  type        = string
  description = "StorageClass for Dragonfly seedClient PVCs. Empty means use var.storage_class."
  default     = ""
}

variable "dragonfly_storage_class_pl" {
  type        = string
  description = "ESSD performance level for Dragonfly StorageClass (PL0/PL1/PL2/PL3). Only used when create_dragonfly_storage_class=true."
  default     = "PL1"
}

variable "create_dragonfly_storage_class" {
  type        = bool
  description = "Whether to create a dedicated StorageClass for Dragonfly with specific performance level."
  default     = false
}

variable "dragonfly_seed_client_pvc_size" {
  type        = string
  description = "PVC size for Dragonfly seedClient persistent storage."
  default     = "500Gi"
}

variable "install_dragonfly" {
  type        = bool
  description = "Whether to install Dragonfly P2P image distribution system."
  default     = false
}

variable "dragonfly_chart_repository" {
  type        = string
  description = "Helm repository containing the Dragonfly chart."
  default     = "https://dragonflyoss.github.io/helm-charts/"
}

variable "dragonfly_chart_name" {
  type        = string
  description = "Dragonfly Helm chart name."
  default     = "dragonfly"
}

variable "dragonfly_chart_version" {
  type        = string
  description = "Pinned Dragonfly Helm chart version."
  default     = "1.6.21"
}

variable "dragonfly_namespace" {
  type        = string
  description = "Namespace for Dragonfly components."
  default     = "dragonfly-system"
}

variable "dragonfly_seed_node_pool" {
  description = "Seed node pool for Dragonfly seed-client. Autoscaling requires the ACK autoscaler RAM role."
  type = object({
    enabled                     = optional(bool, true)
    name                        = optional(string, "dragonfly-seed")
    size                        = optional(number, 3)
    auto_scaling_enabled        = optional(bool, false)
    min_size                    = optional(number, 1)
    max_size                    = optional(number, 10)
    instance_types              = optional(list(string))
    system_disk_category        = optional(string)
    system_disk_size            = optional(number)
    data_disk_enabled           = optional(bool, true)
    data_disk_category          = optional(string)
    data_disk_size              = optional(number, 500)
    data_disk_performance_level = optional(string, "PL1")
    node_label_key              = optional(string, "node-role")
    node_label_value            = optional(string, "dragonfly-seed")
    taint_key                   = optional(string, "dedicated")
    taint_value                 = optional(string, "dragonfly-seed")
    taint_effect                = optional(string, "NoSchedule")
    mount_local_nvme            = optional(bool, false)
    local_nvme_mount_path       = optional(string, "/mnt/dragonfly-seed")
  })
  default = {}
}

variable "dragonfly_server_node_pool" {
  description = "Server node pool for Dragonfly manager and scheduler."
  type = object({
    enabled          = optional(bool, true)
    name             = optional(string, "dragonfly-server")
    size             = optional(number, 1)
    instance_types   = optional(list(string))
    system_disk_size = optional(number)
    data_disk_size   = optional(number, 200)
    node_label_key   = optional(string, "node-role")
    node_label_value = optional(string, "dragonfly-server")
    taint_key        = optional(string, "dedicated")
    taint_value      = optional(string, "dragonfly-server")
    taint_effect     = optional(string, "NoSchedule")
  })
  default = {}
}

variable "dragonfly_manager_resources" {
  description = "Resource requests/limits and replicas for Dragonfly manager."
  type = object({
    replicas       = optional(number, 1)
    cpu_request    = optional(string, "500m")
    memory_request = optional(string, "512Mi")
    cpu_limit      = optional(string, "2")
    memory_limit   = optional(string, "4Gi")
  })
  default = {}
}

variable "dragonfly_scheduler_resources" {
  description = "Resource requests/limits and replicas for Dragonfly scheduler."
  type = object({
    replicas       = optional(number, 1)
    cpu_request    = optional(string, "500m")
    memory_request = optional(string, "512Mi")
    cpu_limit      = optional(string, "2")
    memory_limit   = optional(string, "4Gi")
  })
  default = {}
}

variable "dragonfly_seed_client_resources" {
  description = "Resource requests/limits for Dragonfly seed-client."
  type = object({
    cpu_request    = optional(string, "500m")
    memory_request = optional(string, "512Mi")
    cpu_limit      = optional(string, "2")
    memory_limit   = optional(string, "4Gi")
  })
  default = {}
}

variable "install_prereqs" {
  type        = bool
  description = "Whether to install prerequisite cluster dependencies (currently OpenKruise) via Helm."
  default     = false
}

variable "prereq_namespace" {
  type        = string
  description = "Namespace for prerequisite components."
  default     = "kruise-system"
}

variable "prereq_kruise_repo" {
  type        = string
  description = "Helm repository URL for OpenKruise."
  default     = "https://openkruise.github.io/charts/"
}

variable "prereq_kruise_chart" {
  type        = string
  description = "Helm chart name for OpenKruise."
  default     = "kruise"
}

variable "prereq_kruise_chart_version" {
  type        = string
  description = "Helm chart version for OpenKruise. Empty means latest from repo."
  default     = ""
}

variable "grafana_public_access" {
  type        = bool
  description = "Whether to expose Grafana via its own LoadBalancer. When true, Grafana is served at root path (/) instead of /grafana subpath."
  default     = true
}

variable "grafana_acl_id" {
  type        = string
  description = "Alibaba Cloud ACL ID for Grafana LoadBalancer. Only effective when grafana_public_access=true."
  default     = ""
}

variable "grafana_admin_password" {
  type        = string
  description = "Grafana admin password. Leave empty to let the Helm chart generate one."
  default     = ""
  sensitive   = true
}

variable "node_secret_create" {
  type        = bool
  description = "Whether to create akernel-secrets for node daemonset. Set false only if an existing secret already exists."
  default     = true
}

variable "oss_endpoint" {
  type        = string
  description = "OSS endpoint, e.g. oss-cn-hangzhou.aliyuncs.com."
  default     = ""
}

variable "oss_access_key_id" {
  type        = string
  description = "OSS access key ID for akernel node secret."
  default     = ""
  sensitive   = true
}

variable "oss_access_key_secret" {
  type        = string
  description = "OSS access key secret for akernel node secret."
  default     = ""
  sensitive   = true
}

variable "oss_bucket" {
  type        = string
  description = "OSS bucket name for akernel node secret."
  default     = ""
}

variable "oss_object_prefix" {
  type        = string
  description = "OSS object prefix."
  default     = "/"
}

variable "oss_proxy_url" {
  type        = string
  description = "Optional proxy URL for OSS access."
  default     = ""
}

variable "oss_auths" {
  type = map(object({
    access_key_id     = string
    access_key_secret = string
  }))
  description = "OSS auth credentials keyed by 'endpoint/bucket'. Merges with auto-generated entry from oss_endpoint/oss_bucket vars."
  default     = {}
}

variable "registry_auths" {
  type = map(object({
    username = string
    password = string
  }))
  description = "Container registry auth credentials keyed by registry host (e.g. 'registry.example.com')."
  default     = {}
  sensitive   = true
}

# --- Component resource specifications ---

variable "etcd_resources" {
  description = "Resource requests/limits and PVC size for etcd."
  type = object({
    cpu               = optional(string, "1")
    memory            = optional(string, "2Gi")
    ephemeral_storage = optional(string, "1Gi")
    pvc_size          = optional(string, "50Gi")
  })
  default = {}
}

variable "master_resources" {
  description = "Resource requests/limits for akernel-master."
  type = object({
    cpu               = optional(string, "1")
    memory            = optional(string, "2Gi")
    ephemeral_storage = optional(string, "1Gi")
  })
  default = {}
}

variable "node_resources" {
  description = "Resource requests/limits for akernel-node."
  type = object({
    ephemeral_storage = optional(string, "1Gi")
  })
  default = {}
}

variable "grafana_resources" {
  description = "Resource requests/limits and PVC size for Grafana."
  type = object({
    cpu               = optional(string, "1")
    memory            = optional(string, "2Gi")
    ephemeral_storage = optional(string, "1Gi")
    pvc_size          = optional(string, "20Gi")
  })
  default = {}
}

variable "prometheus_resources" {
  description = "Resource requests/limits and PVC size for Prometheus."
  type = object({
    cpu               = optional(string, "1")
    memory            = optional(string, "2Gi")
    ephemeral_storage = optional(string, "1Gi")
    pvc_size          = optional(string, "20Gi")
  })
  default = {}
}

variable "loki_resources" {
  description = "Resource requests/limits and PVC size for Loki."
  type = object({
    cpu               = optional(string, "1")
    memory            = optional(string, "2Gi")
    ephemeral_storage = optional(string, "1Gi")
    pvc_size          = optional(string, "20Gi")
  })
  default = {}
}

variable "tempo_resources" {
  description = "Resource requests/limits and PVC size for Tempo."
  type = object({
    cpu               = optional(string, "1")
    memory            = optional(string, "2Gi")
    ephemeral_storage = optional(string, "1Gi")
    pvc_size          = optional(string, "20Gi")
  })
  default = {}
}
