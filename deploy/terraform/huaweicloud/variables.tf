# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

variable "region" {
  type        = string
  description = "Huawei Cloud region ID, e.g. cn-north-4."
}

variable "create_cluster" {
  type        = bool
  description = "Whether to create CCE cluster and node pool. If false, use kubeconfig_path for an existing cluster."
  default     = true
}

variable "kubeconfig_path" {
  type        = string
  description = "Path to kubeconfig for an existing cluster when create_cluster=false."
  default     = ""
}

variable "kubeconfig_output_path" {
  type        = string
  description = "Path for the generated kubeconfig when create_cluster=true. Empty uses the module directory."
  default     = ""
}

variable "cluster_name" {
  type        = string
  description = "CCE cluster name."
  default     = "akernel-cce"
}

variable "cluster_type" {
  type        = string
  description = "CCE cluster type."
  default     = "VirtualMachine"
}

variable "cluster_flavor_id" {
  type        = string
  description = "CCE cluster flavor ID. If empty, Terraform auto-selects the first available flavor for cluster_type."
  default     = "cce.s2.small"
}

variable "k8s_version" {
  type        = string
  description = "CCE Kubernetes version."
  default     = ""
}

variable "container_network_type" {
  type        = string
  description = "CCE container network type. Use 'eni' for CCE Turbo."
  default     = "eni"
}

variable "pod_cidr" {
  type        = string
  description = "Pod CIDR."
  default     = "10.0.0.0/16"
}

variable "service_cidr" {
  type        = string
  description = "Service CIDR."
  default     = "10.247.0.0/16"
}

variable "cluster_api_public_access" {
  type        = bool
  description = "Whether to bind an EIP to CCE API server for public access."
  default     = true
}

variable "cluster_api_eip_type" {
  type        = string
  description = "Public IP type for CCE API server EIP."
  default     = "5_bgp"
}

variable "cluster_api_eip_bandwidth_size" {
  type        = number
  description = "Bandwidth size (Mbit/s) for CCE API server EIP."
  default     = 5
}

variable "cluster_api_eip_bandwidth_share_type" {
  type        = string
  description = "Bandwidth share type for CCE API server EIP."
  default     = "PER"
}

variable "cluster_api_eip_bandwidth_charge_mode" {
  type        = string
  description = "Bandwidth charge mode for CCE API server EIP."
  default     = "traffic"
}

variable "node_subnet_enable_snat" {
  type        = bool
  description = "Whether to enable NAT SNAT for node subnet outbound internet access."
  default     = true
}

variable "node_subnet_nat_spec" {
  type        = string
  description = "NAT gateway spec for node subnet SNAT (1: small, 2: medium, 3: large, 4: xlarge)."
  default     = "1"
}

variable "node_subnet_eip_type" {
  type        = string
  description = "Public IP type for node subnet SNAT EIP."
  default     = "5_bgp"
}

variable "node_subnet_eip_bandwidth_size" {
  type        = number
  description = "Bandwidth size (Mbit/s) for node subnet SNAT EIP."
  default     = 20
}

variable "node_subnet_eip_bandwidth_share_type" {
  type        = string
  description = "Bandwidth share type for node subnet SNAT EIP."
  default     = "PER"
}

variable "node_subnet_eip_bandwidth_charge_mode" {
  type        = string
  description = "Bandwidth charge mode for node subnet SNAT EIP."
  default     = "traffic"
}

variable "eni_subnet_name" {
  type        = string
  description = "ENI subnet name for CCE Turbo pod networking."
  default     = "akernel-eni-subnet"
}

variable "eni_subnet_cidr" {
  type        = string
  description = "ENI subnet CIDR block for CCE Turbo pod networking. Must be within VPC CIDR and not overlap with node subnet."
  default     = "192.168.16.0/20"
}

variable "pod_subnet_enable_snat" {
  type        = bool
  description = "Whether to enable NAT SNAT for pod/ENI subnet outbound internet access (CCE Turbo only)."
  default     = true
}

variable "pod_subnet_eip_bandwidth_size" {
  type        = number
  description = "Bandwidth size (Mbit/s) for pod subnet SNAT EIP."
  default     = 100
}

variable "multi_az" {
  type        = bool
  description = "Whether to enable multi-AZ for CCE cluster."
  default     = false
}

variable "availability_zone" {
  type        = string
  description = "Availability zone for subnet/node pool, e.g. cn-north-4a. Leave empty to auto-pick the first AZ."
  default     = ""
}

variable "vpc_name" {
  type        = string
  description = "VPC name."
  default     = "akernel-vpc"
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR block."
  default     = "192.168.0.0/16"
}

variable "subnet_name" {
  type        = string
  description = "Subnet name."
  default     = "akernel-subnet"
}

variable "subnet_cidr" {
  type        = string
  description = "Subnet CIDR block."
  default     = "192.168.10.0/24"
}

variable "security_group_name" {
  type        = string
  description = "Security group name for CCE nodes."
  default     = "akernel-sg"
}

variable "allowed_ssh_cidrs" {
  type        = list(string)
  description = "CIDR list allowed to SSH into CCE nodes (port 22)."
  default     = ["0.0.0.0/0"]
}

variable "node_flavor_id" {
  type        = string
  description = "CCE node flavor ID. If empty, Terraform auto-selects one by CPU/memory filters."
  default     = ""
}

variable "node_flavor_cpu" {
  type        = number
  description = "Node flavor CPU core count when node_flavor_id is empty."
  default     = 4
}

variable "node_flavor_memory" {
  type        = number
  description = "Node flavor memory size in GiB when node_flavor_id is empty."
  default     = 8
}

variable "node_flavor_performance_type" {
  type        = string
  description = "Node flavor performance type when node_flavor_id is empty."
  default     = "normal"
}

variable "node_pool_size" {
  type        = number
  description = "Initial node count in default CCE node pool."
  default     = 3
}

variable "node_pool_min_size" {
  type        = number
  description = "Minimum node count for autoscaling node pool."
  default     = 1
}

variable "node_pool_max_size" {
  type        = number
  description = "Maximum node count for autoscaling node pool."
  default     = 5
}

variable "node_pool_max_pods" {
  type        = number
  description = "Maximum pod count per node."
  default     = 110
}

variable "node_pool_root_volume_type" {
  type        = string
  description = "Node root volume type."
  default     = "SSD"
}

variable "node_pool_root_volume_size" {
  type        = number
  description = "Node root volume size in GiB."
  default     = 100
}

variable "node_pool_data_volume_enabled" {
  type        = bool
  description = "Whether to attach a node data volume. Some non-local-disk flavors require this to be true."
  default     = true
}

variable "node_pool_data_volume_type" {
  type        = string
  description = "Node data volume type."
  default     = "SSD"
}

variable "node_pool_data_volume_size" {
  type        = number
  description = "Node data volume size in GiB."
  default     = 300
}

variable "node_pool_extra_data_volume_enabled" {
  type        = bool
  description = "Whether to attach an extra data volume for akernel hostPath storage (separate from the container runtime data volume)."
  default     = false
}

variable "node_pool_extra_data_volume_type" {
  type        = string
  description = "Extra data volume type."
  default     = "SSD"
}

variable "node_pool_extra_data_volume_size" {
  type        = number
  description = "Extra data volume size (GiB)."
  default     = 300
}

variable "node_pool_extra_data_volume_mount_path" {
  type        = string
  description = "Path to auto-format and mount the extra data volume. Typically /home/akernel."
  default     = "/home/akernel"
}

variable "node_pool_extra_data_volume_fs_type" {
  type        = string
  description = "Filesystem type for the extra data volume (ext4 or xfs)."
  default     = "ext4"
}

variable "node_pool_scale_down_cooldown_time" {
  type        = number
  description = "Node pool scale-down cooldown time in minutes."
  default     = 10
}

variable "node_pool_os" {
  type        = string
  description = "Node OS image name for CCE node pool. Use 'Huawei Cloud EulerOS 2.0' for 5.10 kernel."
  default     = "Huawei Cloud EulerOS 2.0"
}

variable "node_pool_login_password" {
  type        = string
  description = "Login password for CCE nodes (ignored if key pair is set)."
  default     = ""
  sensitive   = true
}

variable "node_pool_key_pair" {
  type        = string
  description = "SSH key pair name for CCE nodes."
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
    flavor_id         = optional(string)
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
  description = "Additional node pools with custom flavors, labels, and taints."
  default     = []
}

variable "sandboxd_nat_backend" {
  type        = string
  description = "Sandboxd NAT backend. The iptables mode loads IPv4/IPv6 bridge-netfilter modules at boot."
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
  default     = "csi-disk"
}

variable "node_home_csi_size" {
  type        = string
  description = "Requested size for node /home/akernel CSI ephemeral volume."
  default     = "300Gi"
}

variable "kubeconfig_duration" {
  type        = number
  description = "Kubeconfig certificate duration in minutes when create_cluster=true."
  default     = 1440
}

variable "core_namespace" {
  type        = string
  description = "Namespace for akernel core release."
  default     = "akernel"
}

variable "iam_litebus_data_key" {
  type        = string
  description = "Hex-encoded IAM JWT signing seed passed to the core chart. Leave empty to let the chart generate or reuse a seed."
  default     = ""
  sensitive   = true
}

variable "etcd_image_repository" {
  type        = string
  description = "Image repository for etcd."
  default     = "public.ecr.aws/bitnami/etcd"
}

variable "etcd_image_tag" {
  type        = string
  description = "Image tag for etcd."
  default     = "3.6.8"
}

variable "master_image_repository" {
  type        = string
  description = "Image repository for akernel-master."
  default     = ""
}

variable "master_image_tag" {
  type        = string
  description = "Image tag for akernel-master."
  default     = ""
}

variable "node_image_repository" {
  type        = string
  description = "Image repository for akernel-node."
  default     = ""
}

variable "node_image_tag" {
  type        = string
  description = "Image tag for akernel-node."
  default     = ""
}

variable "traefik_image_repository" {
  type        = string
  description = "Image repository for Traefik."
  default     = "traefik"
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
  description = "Service type for akernel-master. Use 'LoadBalancer' for public access (CCE auto-creates ELB)."
  default     = "LoadBalancer"
}

variable "master_public_access_8888" {
  type        = bool
  description = "Whether to expose akernel-master port 8888 to public network via LoadBalancer."
  default     = false
}


variable "master_service_loadbalancer_ip" {
  type        = string
  description = "Optional Service loadBalancerIP (EIP) for akernel-master when master_public_access_8888=true."
  default     = ""
}

variable "master_service_annotations" {
  type        = map(string)
  description = "Extra annotations for akernel-master Service."
  default     = {}
}

variable "master_elb_bandwidth_size" {
  type        = number
  description = "Bandwidth size (Mbit/s) for auto-created ELB when master_public_access_8888=true."
  default     = 5
}

variable "master_elb_bandwidth_charge_mode" {
  type        = string
  description = "Bandwidth charge mode for auto-created ELB."
  default     = "traffic"
}

variable "master_elb_eip_type" {
  type        = string
  description = "EIP type for auto-created ELB."
  default     = "5_bgp"
}

variable "master_replicas" {
  type        = number
  description = "Number of master replicas (for HA when frontend is enabled)."
  default     = 1
}

variable "frontend_enabled" {
  type        = bool
  description = "Whether to enable frontend Deployment (splits from master for independent scaling)."
  default     = false
}

variable "frontend_replicas" {
  type        = number
  description = "Number of frontend replicas."
  default     = 2
}

variable "frontend_cpu" {
  type        = string
  description = "CPU request/limit for frontend pods."
  default     = "4"
}

variable "frontend_memory" {
  type        = string
  description = "Memory request/limit for frontend pods."
  default     = "8Gi"
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
  default     = false
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

variable "traefik_public_access" {
  type        = bool
  description = "Whether Traefik receives a public Huawei Cloud LoadBalancer."
  default     = true
}

variable "traefik_tls_enabled" {
  type        = bool
  description = "Whether to enable TLS for Traefik."
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

variable "traefik_internal_stats_enabled" {
  type        = bool
  description = "Whether to enable the /internal-stats endpoint on Traefik."
  default     = false
}

variable "traefik_internal_stats_image" {
  type        = string
  description = "BusyBox image for the Traefik /internal-stats sidecar."
  default     = "busybox:1.37.0-musl"
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

variable "grafana_public_access" {
  type        = bool
  description = "Whether Grafana receives a public Huawei Cloud LoadBalancer."
  default     = true
}

variable "monitor_storage_class" {
  type        = string
  description = "StorageClass for Grafana/Prometheus PVCs."
  default     = "csi-disk"
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
  description = "Seed node pool for Dragonfly seed-client (storage-intensive, autoscaling)."
  type = object({
    enabled               = optional(bool, true)
    name                  = optional(string, "dragonfly-seed")
    size                  = optional(number, 3)
    min_size              = optional(number, 1)
    max_size              = optional(number, 10)
    flavor_id             = optional(string)
    system_disk_size      = optional(number)
    data_disk_enabled     = optional(bool, true)
    data_disk_size        = optional(number, 500)
    node_label_key        = optional(string, "node-role")
    node_label_value      = optional(string, "dragonfly-seed")
    taint_key             = optional(string, "dedicated")
    taint_value           = optional(string, "dragonfly-seed")
    taint_effect          = optional(string, "NoSchedule")
    mount_local_nvme      = optional(bool, false)
    local_nvme_mount_path = optional(string, "/mnt/dragonfly-seed")
  })
  default = {}
}

variable "dragonfly_server_node_pool" {
  description = "Server node pool for Dragonfly manager and scheduler."
  type = object({
    enabled          = optional(bool, true)
    name             = optional(string, "dragonfly-server")
    size             = optional(number, 1)
    flavor_id        = optional(string)
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
  description = "Helm chart version for OpenKruise."
  default     = "1.8.3"
}

variable "core_values_override_files" {
  type        = list(string)
  description = "Additional YAML values files for core chart overrides. Each file content is appended to Helm values."
  default     = []
}

variable "monitor_values_override_files" {
  type        = list(string)
  description = "Additional YAML values files for monitor chart overrides. Each file content is appended to Helm values."
  default     = []
}

variable "grafana_admin_password" {
  type        = string
  description = "Grafana admin password. Leave empty to let the Helm chart generate one."
  default     = ""
  sensitive   = true
}

variable "akernel_env" {
  type        = string
  description = "Environment label for akernel monitoring (AKERNEL_ENV). Defaults to cluster_name if empty."
  default     = ""
}

variable "node_secret_create" {
  type        = bool
  description = "Whether to create akernel-secrets for node daemonset. Set false only if an existing secret already exists."
  default     = true
}

variable "oss_endpoint" {
  type        = string
  description = "OSS endpoint."
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
    pvc_size          = optional(string, "10Gi")
  })
  default = {}
}
