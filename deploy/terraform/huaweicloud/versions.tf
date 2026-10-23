# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    huaweicloud = {
      source = "huaweicloud/huaweicloud"
      # cce_flavor_specifications was introduced in 1.83.0.
      version = ">= 1.83.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.11.0"
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 2.4.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.2.0"
    }
  }
}
