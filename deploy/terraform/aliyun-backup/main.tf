# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

# Additive backup state. The existing ACK cluster and its RRSA provider are
# prerequisites, never imported or modified by this module.
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    alicloud = { source = "aliyun/alicloud", version = "~> 1.271.0" }
  }
}

provider "alicloud" { region = var.region }
variable "region" { type = string }
variable "bucket_name" { type = string }
variable "role_name" { type = string }
variable "oidc_provider_arn" { type = string }
variable "service_account_subject" {
  type    = string
  default = "system:serviceaccount:akernel-storage:akernel-checkpoint-backup"
}
variable "object_prefix" {
  type    = string
  default = "akernel-develop"
  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9_-]*$", var.object_prefix))
    error_message = "Use one nonempty path component for the backup prefix."
  }
}
variable "tags" { type = map(string) }

resource "alicloud_oss_bucket" "backup" {
  bucket          = var.bucket_name
  acl             = "private"
  storage_class   = "Standard"
  redundancy_type = "ZRS"
  force_destroy   = false
  tags            = var.tags
  lifecycle_rule {
    id      = "completed-sets-seven-days"
    prefix  = "${var.object_prefix}/manifests/"
    enabled = true
    expiration { days = 7 }
  }
  # Jobs have a six-hour deadline. One extra day preserves every payload for
  # the full lifetime of its completion record, including midnight crossings.
  lifecycle_rule {
    id      = "payloads-eight-days"
    prefix  = "${var.object_prefix}/payloads/"
    enabled = true
    expiration { days = 8 }
    abort_multipart_upload { days = 3 }
  }
  lifecycle { prevent_destroy = true }
}

resource "alicloud_oss_bucket_public_access_block" "backup" {
  bucket              = alicloud_oss_bucket.backup.id
  block_public_access = true
}
resource "alicloud_oss_bucket_server_side_encryption" "backup" {
  bucket        = alicloud_oss_bucket.backup.id
  sse_algorithm = "AES256"
}
resource "alicloud_oss_bucket_policy" "tls" {
  bucket = alicloud_oss_bucket.backup.id
  policy = jsonencode({
    Version = "1"
    Statement = [{
      Effect    = "Deny", Principal = ["*"], Action = ["oss:*"],
      Resource  = ["acs:oss:*:*:${var.bucket_name}", "acs:oss:*:*:${var.bucket_name}/*"],
      Condition = { Bool = { "acs:SecureTransport" = ["false"] } }
    }]
  })
}

resource "alicloud_ram_role" "writer" {
  role_name            = var.role_name
  max_session_duration = 3600
  description          = "Daily AKernel checkpoint backup; one ACK ServiceAccount only."
  assume_role_policy_document = jsonencode({
    Version = "1"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole",
      Principal = { Federated = [var.oidc_provider_arn] },
      Condition = { StringEquals = {
        "oidc:aud" = "sts.aliyuncs.com", "oidc:sub" = var.service_account_subject
      } }
    }]
  })
  lifecycle { prevent_destroy = true }
}
resource "alicloud_ram_policy" "writer" {
  policy_name = "${var.role_name}-write"
  description = "Append backup sets without permission to read or delete them."
  policy_document = jsonencode({
    Version = "1"
    Statement = [{
      Effect = "Allow", Action = ["oss:PutObject", "oss:AbortMultipartUpload"],
      Resource = [
        "acs:oss:*:*:${var.bucket_name}/${var.object_prefix}/payloads/*",
        "acs:oss:*:*:${var.bucket_name}/${var.object_prefix}/manifests/*"
      ]
    }]
  })
}
resource "alicloud_ram_role_policy_attachment" "writer" {
  role_name   = alicloud_ram_role.writer.role_name
  policy_name = alicloud_ram_policy.writer.policy_name
  policy_type = "Custom"
}
output "bucket" { value = alicloud_oss_bucket.backup.id }
output "writer_role_arn" { value = alicloud_ram_role.writer.arn }
