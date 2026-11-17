# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

# Additive entry point for existing ACK clusters. This state owns only these
# node pools; it must not import the cluster or its existing pools.
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 1.271.0"
    }
  }
}

provider "alicloud" {
  region = var.region
}

variable "region" { type = string }
variable "cluster_id" { type = string }
variable "name_prefix" { type = string }
variable "security_group_ids" { type = list(string) }
variable "key_name" {
  type    = string
  default = null
}
variable "image_id" { type = string }
variable "zones" {
  description = "An explicit zone-to-vSwitch map; one dedicated storage host per zone."
  type        = map(string)
  validation {
    condition     = length(var.zones) >= 3 && length(distinct(values(var.zones))) == length(var.zones)
    error_message = "At least three separate zone/vSwitch pairs are required."
  }
}
variable "instance_type" {
  type    = string
  default = "ecs.g7.2xlarge"
}
variable "tags" {
  type    = map(string)
  default = {}
}

resource "alicloud_cs_kubernetes_node_pool" "storage" {
  for_each = var.zones

  cluster_id           = var.cluster_id
  node_pool_name       = "${var.name_prefix}-storage-${each.key}"
  vswitch_ids          = [each.value]
  security_group_ids   = var.security_group_ids
  instance_types       = [var.instance_type]
  image_id             = var.image_id
  desired_size         = 1
  system_disk_category = "cloud_essd"
  system_disk_size     = 200
  key_name             = var.key_name
  # OSDs use independent CSI Block PVCs. No host data disk is selected,
  # formatted or shared with the business or image-cache pools.
  labels {
    key   = "node-role"
    value = "akernel-storage"
  }
  taints {
    key    = "dedicated"
    value  = "akernel-storage"
    effect = "NoSchedule"
  }
  tags = var.tags

  lifecycle {
    prevent_destroy = true
  }
}

output "node_pool_ids" {
  value = { for zone, pool in alicloud_cs_kubernetes_node_pool.storage : zone => pool.id }
}
