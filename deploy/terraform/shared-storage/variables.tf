# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

variable "management_namespace" {
  type        = string
  description = "Existing AKernel namespace used to track the storage namespace release."
}

variable "checkpoint_storage" {
  type        = any
  description = "Normalized checkpoint_storage object from the provider module."
}

variable "juicefs" {
  type        = any
  description = "Normalized optional JuiceFS deployment object from the provider module."
}
