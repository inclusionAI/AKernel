# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

output "helm_values" {
  description = "ADX checkpointStorage configuration for both execution nodes and Coordinator."
  value       = local.checkpoint_values
  precondition {
    condition     = !local.selected_storage.local_cache.enabled || local.selected_storage.kind == "distributed_fs"
    error_message = "local_cache requires distributed_fs checkpoint storage."
  }
  precondition {
    condition = local.selected_storage.kind == "local" || (
      local.selected_storage.alias != "local" &&
      can(regex("^[A-Za-z0-9-]+$", local.selected_storage.alias))
    )
    error_message = "distributed_fs alias must contain only ASCII letters, digits or hyphens and must not be local."
  }
  precondition {
    condition     = !var.juicefs.enabled || local.client_node_selector["kubernetes.io/os"] == "linux"
    error_message = "Managed JuiceFS clients and consumers require Linux nodes."
  }
  precondition {
    condition = (
      local.selected_storage.kind == "local" ||
      ((local.selected_storage.host_path != "") != (local.selected_storage.existing_claim != ""))
    )
    error_message = "distributed_fs requires exactly one host_path or existing_claim."
  }
  precondition {
    condition = !var.juicefs.enabled || (
      var.juicefs.ceph_storage_class != "" && var.juicefs.postgres_storage_class != "" &&
      var.checkpoint_storage.existing_claim == "" &&
      (var.checkpoint_storage.host_path == "" || var.checkpoint_storage.host_path == var.juicefs.host_mount_path)
    )
    error_message = "Managed JuiceFS requires explicit Ceph/PostgreSQL StorageClasses and must not conflict with an external checkpoint mount or claim."
  }
}
