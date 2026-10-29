# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.11.0"
    }
  }
}

locals {
  namespace_chart = "${path.module}/../../shared-storage/charts/namespace"
  storage_chart   = "${path.module}/../../shared-storage/charts/juicefs"
  client_node_selector = merge(
    { "kubernetes.io/os" = "linux" },
    var.juicefs.client_node_selector,
    [for path in var.juicefs.values_files : try(yamldecode(file(path)).juicefs.nodeSelector, {})]...
  )
  selected_storage = var.juicefs.enabled ? merge(var.checkpoint_storage, {
    kind           = "distributed_fs"
    host_path      = var.juicefs.host_mount_path
    existing_claim = ""
  }) : var.checkpoint_storage
  checkpoint_values = {
    kind          = local.selected_storage.kind
    alias         = local.selected_storage.alias
    mountPath     = local.selected_storage.mount_path
    hostPath      = local.selected_storage.host_path
    existingClaim = local.selected_storage.existing_claim
    prefix        = local.selected_storage.prefix
    localCache = {
      enabled     = local.selected_storage.local_cache.enabled
      root        = local.selected_storage.local_cache.root
      budgetBytes = local.selected_storage.local_cache.budget_bytes
      maxEntries  = local.selected_storage.local_cache.max_entries
    }
    nodeSelector = var.juicefs.enabled ? local.client_node_selector : {}
  }
}

resource "helm_release" "storage_namespace" {
  count     = var.juicefs.enabled && var.juicefs.create_namespace ? 1 : 0
  name      = "akernel-storage-namespace"
  namespace = var.management_namespace
  chart     = local.namespace_chart
  values    = [yamlencode({ name = var.juicefs.namespace })]
  set {
    name  = "chartContentHash"
    value = sha256(join("", [for path in sort(tolist(fileset(local.namespace_chart, "**"))) : filesha256("${local.namespace_chart}/${path}")]))
  }
}

resource "helm_release" "rook" {
  count      = var.juicefs.enabled && var.juicefs.install_rook ? 1 : 0
  name       = "akernel-rook"
  namespace  = var.juicefs.namespace
  repository = "https://charts.rook.io/release"
  chart      = "rook-ceph"
  version    = "v1.21.0"
  timeout    = 1800
  values = concat([yamlencode({
    currentNamespaceOnly  = true
    enableDiscoveryDaemon = false
    image = {
      repository = "docker.io/rook/ceph"
      tag        = "v1.21.0@sha256:2c3a65786d3cc85ba15d7f258461c0bf392c6a6ee19dde8011259b8d34eeedd0"
    }
  })], [for path in var.juicefs.rook_values_files : file(path)])
  depends_on = [helm_release.storage_namespace]
}

resource "helm_release" "juicefs" {
  count         = var.juicefs.enabled ? 1 : 0
  name          = "akernel-juicefs"
  namespace     = var.juicefs.namespace
  chart         = local.storage_chart
  timeout       = 3600
  wait          = true
  wait_for_jobs = true
  set {
    name  = "chartContentHash"
    value = sha256(join("", [for path in sort(tolist(fileset(local.storage_chart, "**"))) : filesha256("${local.storage_chart}/${path}")]))
  }
  values = concat([yamlencode({
    juicefs = {
      hostMountPath   = var.juicefs.host_mount_path
      nodeSelector    = var.juicefs.client_node_selector
      cacheMiB        = var.juicefs.cache_mib
      cacheHostPath   = var.juicefs.cache_host_path
      cacheLargeWrite = var.juicefs.cache_large_write
      writeback       = false
    }
    ceph = {
      storageClass      = var.juicefs.ceph_storage_class
      osdCount          = var.juicefs.osd_count
      osdSize           = var.juicefs.osd_size
      monCount          = var.juicefs.mon_count
      replicas          = var.juicefs.replicas
      nodeSelector      = var.juicefs.storage_node_selector
      gatewayTcpNoDelay = var.juicefs.gateway_tcp_nodelay
    }
    postgres = {
      storageClass = var.juicefs.postgres_storage_class
      size         = var.juicefs.postgres_size
    }
    })], [for path in var.juicefs.values_files : file(path)], [yamlencode({
    # Keep client and consumer placement identical even with custom overlays.
    juicefs = {
      hostMountPath = var.juicefs.host_mount_path
      nodeSelector  = local.client_node_selector
    }
  })])
  depends_on = [helm_release.storage_namespace, helm_release.rook]
}
