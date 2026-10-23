# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

# Exercise the complete module and provider schemas without cloud access.
# Every run is plan-only; no provisioner or resource creation is executed.
mock_provider "huaweicloud" {
  override_during = plan

  mock_data "huaweicloud_cce_cluster_certificate" {
    defaults = { kube_config_raw = "apiVersion: v1\nkind: Config\nclusters: []\n" }
  }
}
mock_provider "helm" { override_during = plan }
mock_provider "local" { override_during = plan }
mock_provider "null" { override_during = plan }

variables {
  region                    = "cn-north-4"
  availability_zone         = "cn-north-4a"
  node_flavor_id            = "c6.large.2"
  node_pool_key_pair        = "offline-test"
  master_image_repository   = "example.invalid/akernel/all-in-one"
  master_image_tag          = "offline"
  node_image_repository     = "example.invalid/akernel/all-in-one"
  node_image_tag            = "offline"
  install_monitor           = false
  install_prereqs           = false
  install_dragonfly         = false
  cluster_api_public_access = false
  node_subnet_enable_snat   = false
}

run "control_keeps_default_runtime_disk" {
  command = plan

  variables {
    control_node_pool                      = { enabled = true }
    node_pool_extra_data_volume_enabled    = true
    node_pool_extra_data_volume_size       = 400
    node_pool_extra_data_volume_mount_path = "/srv/akernel"
  }

  assert {
    condition = (
      length(huaweicloud_cce_node_pool.extra["control"].data_volumes) == 1 &&
      try(one(huaweicloud_cce_node_pool.extra["control"].data_volumes).size, 0) == 300 &&
      try(one(huaweicloud_cce_node_pool.extra["control"].data_volumes).volumetype, "") == "SSD"
    )
    error_message = "The CCE control pool must keep one default runtime disk, without the compute pool's extra sandbox disk."
  }

  assert {
    condition = (
      length(huaweicloud_cce_node_pool.default[0].data_volumes) == 2 &&
      (huaweicloud_cce_node_pool.extra["control"].postinstall == null ||
      huaweicloud_cce_node_pool.extra["control"].postinstall == "")
    )
    error_message = "Control-pool isolation must preserve compute disks and omit compute postinstall/bootstrap configuration."
  }

  assert {
    condition = (
      strcontains(huaweicloud_cce_node_pool.default[0].postinstall, "MIN_SIZE_GB=\"400\"") &&
      strcontains(huaweicloud_cce_node_pool.default[0].postinstall, "STORAGE_MARKER_PATH=\"/srv/akernel/.akernel-storage-ready\"") &&
      !strcontains(huaweicloud_cce_node_pool.default[0].postinstall, "$$")
    )
    error_message = "Compute postinstall must receive the extra disk capacity and mount-derived readiness marker, with shell escaping resolved."
  }
}

run "custom_runtime_disk_preserves_existing_extra_pool" {
  command = plan

  variables {
    control_node_pool          = { enabled = true }
    node_pool_data_volume_size = 160
    node_pool_data_volume_type = "SAS"
    extra_node_pools = [{
      name              = "existing-control"
      size              = 2
      flavor_id         = "c6.xlarge.2"
      system_disk_size  = 120
      data_disk_enabled = true
      data_disk_size    = 240
      labels            = { "node-role" = "control", "preserved" = "yes" }
      taints            = [{ key = "dedicated", value = "existing", effect = "NoSchedule" }]
    }]
  }

  assert {
    condition = (
      length(huaweicloud_cce_node_pool.extra["control"].data_volumes) == 1 &&
      try(one(huaweicloud_cce_node_pool.extra["control"].data_volumes).size, 0) == 160 &&
      try(one(huaweicloud_cce_node_pool.extra["control"].data_volumes).volumetype, "") == "SAS"
    )
    error_message = "The CCE control runtime disk must inherit the configured base disk capacity and type."
  }

  assert {
    condition = (
      toset(keys(huaweicloud_cce_node_pool.extra)) == toset(["control", "existing-control"]) &&
      huaweicloud_cce_node_pool.extra["existing-control"].initial_node_count == 2 &&
      huaweicloud_cce_node_pool.extra["existing-control"].flavor_id == "c6.xlarge.2" &&
      one(huaweicloud_cce_node_pool.extra["existing-control"].root_volume).size == 120 &&
      length(huaweicloud_cce_node_pool.extra["existing-control"].data_volumes) == 1 &&
      one(huaweicloud_cce_node_pool.extra["existing-control"].data_volumes).size == 240 &&
      one(huaweicloud_cce_node_pool.extra["existing-control"].data_volumes).volumetype == "SAS" &&
      huaweicloud_cce_node_pool.extra["existing-control"].labels == tomap({ "node-role" = "control", "preserved" = "yes" }) &&
      one(huaweicloud_cce_node_pool.extra["existing-control"].taints).key == "dedicated" &&
      one(huaweicloud_cce_node_pool.extra["existing-control"].taints).value == "existing" &&
      one(huaweicloud_cce_node_pool.extra["existing-control"].taints).effect == "NoSchedule"
    )
    error_message = "Adding the generated control pool must not rewrite a user-defined extra pool, including one with a control label."
  }
}

run "legacy_profile_creates_no_control_pool" {
  command = plan

  assert {
    condition     = length(huaweicloud_cce_node_pool.extra) == 0
    error_message = "Legacy defaults must not create a control pool."
  }
}

run "existing_pool_selector_creates_no_control_pool" {
  command = plan

  variables {
    control_node_pool = {
      node_selector = { "node-role" = "existing-control" }
      tolerations   = [{ key = "dedicated", value = "existing", effect = "NoSchedule" }]
    }
  }

  assert {
    condition     = length(huaweicloud_cce_node_pool.extra) == 0
    error_message = "Selecting an existing control pool must not create another pool."
  }
}
