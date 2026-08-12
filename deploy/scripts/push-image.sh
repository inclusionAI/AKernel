#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/deploy/scripts/common.sh"

vendor="aliyun"
env_name="default"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vendor)
      vendor="$2"
      shift 2
      ;;
    --env)
      env_name="$2"
      shift 2
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

vendor="$(normalize_vendor "${vendor}")"
vendor_dir "${vendor}" >/dev/null
load_env_config "${env_name}"
require_cmd docker

missing_profile_vars=()
for var_name in ETCD_IMAGE_REPOSITORY ETCD_IMAGE_TAG TRAEFIK_INTERNAL_STATS_IMAGE; do
  if [[ -z "${!var_name:-}" ]]; then
    missing_profile_vars+=("${var_name}")
  fi
done
if [[ "${#missing_profile_vars[@]}" -gt 0 ]]; then
  die "deployment profile ${env_name} is missing dependency image settings (${missing_profile_vars[*]}); rerun make config ENV=${env_name} and review the generated Terraform plan"
fi

# Pinned dependency image versions. Keep these in sync with the chart defaults
# in deploy/akernel/charts/core/values.yaml (etcd.image.tag and the busybox tag
# under traefik.internalStats.image). configure.sh mirrors the same values into
# the deployment profile, so an override arriving via env vars must match a
# chart that the caller has actually updated accordingly.
readonly etcd_version="3.6.8"
readonly busybox_tag="1.37.0-musl"

all_in_one_image="${IMAGE_REPOSITORY}:${IMAGE_TAG}"
etcd_source_image="akerneldev/etcd:${etcd_version}"
busybox_source_image="busybox:${busybox_tag}"
etcd_image="${ETCD_IMAGE_REPOSITORY}:${ETCD_IMAGE_TAG}"
busybox_image="${TRAEFIK_INTERNAL_STATS_IMAGE}"

# Warn if an override disagrees with the chart default, so a stale pin does not
# silently push a tag the bundled chart will not request.
[[ "${ETCD_IMAGE_TAG}" == "${etcd_version}" ]] || \
  warn "ETCD_IMAGE_TAG=${ETCD_IMAGE_TAG} overrides the chart default ${etcd_version}; ensure the chart's etcd.image.tag matches"
[[ "${TRAEFIK_INTERNAL_STATS_IMAGE}" == *":${busybox_tag}" ]] || \
  warn "TRAEFIK_INTERNAL_STATS_IMAGE=${TRAEFIK_INTERNAL_STATS_IMAGE} does not use the chart default tag ${busybox_tag}; ensure the chart's traefik.internalStats.image matches"

docker image inspect "${all_in_one_image}" >/dev/null 2>&1 || \
  die "missing local image ${all_in_one_image}; run make build ENV=${env_name} first"

# Push the primary artifact first. It is already built locally, so pushing it
# must not depend on pulling etcd/busybox from upstream registries that may be
# unreachable on the build host (e.g. cross-border network restrictions).
info "pushing ${all_in_one_image}"
docker push "${all_in_one_image}"

info "preparing deployment dependency images (etcd, BusyBox)"
docker build \
  --build-arg "ETCD_VERSION=${etcd_version}" \
  -f "${ROOT}/builder/etcd.Dockerfile" \
  -t "${etcd_source_image}" \
  "${ROOT}" || \
  die "failed to build etcd image; build host may be unable to reach gcr.io for the upstream etcd binaries"
docker pull "${busybox_source_image}" || \
  die "failed to pull ${busybox_source_image}; build host may be unable to reach Docker Hub"

if [[ "${etcd_source_image}" != "${etcd_image}" ]]; then
  docker tag "${etcd_source_image}" "${etcd_image}"
fi
if [[ "${busybox_source_image}" != "${busybox_image}" ]]; then
  docker tag "${busybox_source_image}" "${busybox_image}"
fi

for image in "${etcd_image}" "${busybox_image}"; do
  info "pushing ${image}"
  docker push "${image}"
done
