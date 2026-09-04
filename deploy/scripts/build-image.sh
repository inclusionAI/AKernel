#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/deploy/scripts/common.sh"

repository=""
tag=""
env_name=""
runtime_image=""
runtime_profile="${RUNTIME_PROFILE:-rrt}"
runtime_versions_file="${ROOT}/src/sandboxd/third_party/runtime-versions.env"
if [[ ! -f "${runtime_versions_file}" ]]; then
  die "missing runtime version manifest: ${runtime_versions_file}"
fi
# shellcheck source=/dev/null
source "${runtime_versions_file}"
gvisor_release="${GVISOR_RELEASE:-}"
gvisor_amd64_sha512="${GVISOR_AMD64_SHA512:-}"
gvisor_amd64_url="${GVISOR_AMD64_URL:-}"
firecracker_release="${FIRECRACKER_RELEASE:-}"
firecracker_amd64_sha256="${FIRECRACKER_AMD64_SHA256:-}"
firecracker_amd64_url="${FIRECRACKER_AMD64_URL:-}"
open_yr_core_wheel_url="${OPEN_YR_CORE_WHEEL_URL:-}"
open_yr_core_wheel_sha256="${OPEN_YR_CORE_WHEEL_SHA256:-}"
rrt_runtime_url="${RRT_RUNTIME_URL:-}"
rrt_runtime_sha256="${RRT_RUNTIME_SHA256:-}"
open_yr_data_plane_wheel_path="${OPEN_YR_DATA_PLANE_WHEEL_PATH:-}"
open_yr_data_plane_wheel_url="${OPEN_YR_DATA_PLANE_WHEEL_URL:-}"
open_yr_data_plane_wheel_sha256="${OPEN_YR_DATA_PLANE_WHEEL_SHA256:-}"
print_component_versions=0

component_revision() {
  local source_dir="$1"
  local component="$2"
  local revision

  if ! git -C "${source_dir}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    die "${component} source is not initialized: ${source_dir}; run git submodule update --init --recursive"
  fi
  revision="$(git -C "${source_dir}" rev-parse HEAD)"
  if [[ -n "$(git -C "${source_dir}" status --porcelain --untracked-files=normal)" ]]; then
    revision+=".dirty"
  fi
  printf '%s\n' "${revision}"
}

component_version() {
  local source_dir="$1"
  local version

  version="$(git -C "${source_dir}" describe --match 'v[0-9]*' --always 2>/dev/null)"
  if [[ -n "$(git -C "${source_dir}" status --porcelain --untracked-files=normal)" ]]; then
    version+=".dirty"
  fi
  printf '%s\n' "${version}"
}

package_version() {
  local manifest="$1"
  awk '
    /^\[package\]$/ { in_package = 1; next }
    /^\[/ { in_package = 0 }
    in_package && $1 == "version" {
      value = $3
      gsub(/^"|"$/, "", value)
      print value
      exit
    }
  ' "${manifest}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      env_name="$2"
      shift 2
      ;;
    --repository)
      repository="$2"
      shift 2
      ;;
    --tag)
      tag="$2"
      shift 2
      ;;
    --runtime-image)
      runtime_image="$2"
      shift 2
      ;;
    --runtime-profile)
      runtime_profile="$2"
      shift 2
      ;;
    --open-yr-core-wheel-url)
      open_yr_core_wheel_url="$2"
      shift 2
      ;;
    --open-yr-core-wheel-sha256)
      open_yr_core_wheel_sha256="$2"
      shift 2
      ;;
    --rrt-runtime-url)
      rrt_runtime_url="$2"
      shift 2
      ;;
    --rrt-runtime-sha256)
      rrt_runtime_sha256="$2"
      shift 2
      ;;
    --open-yr-data-plane-wheel-path)
      open_yr_data_plane_wheel_path="$2"
      shift 2
      ;;
    --open-yr-data-plane-wheel-url)
      open_yr_data_plane_wheel_url="$2"
      shift 2
      ;;
    --open-yr-data-plane-wheel-sha256)
      open_yr_data_plane_wheel_sha256="$2"
      shift 2
      ;;
    --print-component-versions)
      print_component_versions=1
      shift
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

case "${runtime_profile}" in
  rrt|python) ;;
  *) die "unsupported runtime profile: ${runtime_profile}; expected rrt or python" ;;
esac

case "${AKERNEL_ENABLE_KATA:-true}" in
  true|false) ;;
  *) die "AKERNEL_ENABLE_KATA must be true or false" ;;
esac

case "${AKERNEL_ENABLE_FIRECRACKER:-true}" in
  true|false) ;;
  *) die "AKERNEL_ENABLE_FIRECRACKER must be true or false" ;;
esac

require_cmd docker

docker_build_common_args=()
if [[ -n "${AKERNEL_BUILD_NETWORK:-}" ]]; then
  case "${AKERNEL_BUILD_NETWORK}" in
    default|host|none) ;;
    *) die "AKERNEL_BUILD_NETWORK must be default, host, or none" ;;
  esac
  docker_build_common_args+=(--network "${AKERNEL_BUILD_NETWORK}")
  if [[ "${AKERNEL_BUILD_NETWORK}" == "host" ]]; then
    for proxy_name in \
      HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY \
      http_proxy https_proxy all_proxy no_proxy; do
      proxy_value="${!proxy_name:-}"
      if [[ -n "${proxy_value}" ]]; then
        docker_build_common_args+=(--build-arg "${proxy_name}=${proxy_value}")
      fi
    done
  fi
fi

if [[ -n "${env_name}" && -f "$(state_dir "${env_name}")/config.env" ]]; then
  load_env_config "${env_name}"
  repository="${repository:-${IMAGE_REPOSITORY}}"
  tag="${tag:-${IMAGE_TAG}}"
fi

case "${AKERNEL_ENABLE_RUNC:-false}" in
  true|false) ;;
  *) die "AKERNEL_ENABLE_RUNC must be true or false" ;;
esac

repository="${repository:-akernel-all-in-one}"
tag="${tag:-$(git -C "${AKERNEL_REPO_ROOT}" rev-parse --short HEAD)-$(date +%Y%m%d%H%M%S)}"

runtime_image="${runtime_image:-akernel-runtime:${tag}}"
all_in_one_image="${repository}:${tag}"

cd "${AKERNEL_REPO_ROOT}"

sandboxd_source="${AKERNEL_REPO_ROOT}/src/sandboxd"
distill_fs_source="${AKERNEL_REPO_ROOT}/src/distill-fs"
akernel_version="$(component_version "${AKERNEL_REPO_ROOT}")"
akernel_revision="$(component_revision "${AKERNEL_REPO_ROOT}" akernel)"
sandboxd_version="$(sed -n '1p' "${sandboxd_source}/version/VERSION")"
sandboxd_revision="$(component_revision "${sandboxd_source}" sandboxd)"
distill_fs_version="$(package_version "${distill_fs_source}/Cargo.toml")"
distill_fs_revision="$(component_revision "${distill_fs_source}" distill-fs)"

if [[ -z "${sandboxd_version}" ]]; then
  die "failed to read sandboxd version from ${sandboxd_source}/version/VERSION"
fi
if [[ -z "${distill_fs_version}" ]]; then
  die "failed to read distill-fs package version from ${distill_fs_source}/Cargo.toml"
fi

info "component versions: akernel=${akernel_version} sandboxd=${sandboxd_version} distill-fs=${distill_fs_version}"

if [[ "${print_component_versions}" == "1" ]]; then
  printf '%-12s %-24s %s\n' COMPONENT VERSION REVISION
  printf '%-12s %-24s %s\n' akernel "${akernel_version}" "${akernel_revision}"
  printf '%-12s %-24s %s\n' sandboxd "${sandboxd_version}" "${sandboxd_revision}"
  printf '%-12s %-24s %s\n' distill-fs "${distill_fs_version}" "${distill_fs_revision}"
  exit 0
fi

runtime_build_args=()
if [[ -n "${rrt_runtime_url}" || -n "${rrt_runtime_sha256}" ]]; then
  if [[ -z "${rrt_runtime_url}" || -z "${rrt_runtime_sha256}" ]]; then
    die "RRT_RUNTIME_URL and RRT_RUNTIME_SHA256 must be set together"
  fi
  runtime_build_args+=(
    --build-arg "RRT_RUNTIME_URL=${rrt_runtime_url}"
    --build-arg "RRT_RUNTIME_SHA256=${rrt_runtime_sha256}"
  )
fi

if [[ -n "${open_yr_data_plane_wheel_path}" && -n "${open_yr_data_plane_wheel_url}" ]]; then
  die "OPEN_YR_DATA_PLANE_WHEEL_PATH and OPEN_YR_DATA_PLANE_WHEEL_URL are mutually exclusive"
fi
if [[ -z "${open_yr_data_plane_wheel_path}" && -z "${open_yr_data_plane_wheel_url}" ]]; then
  die "the Rust data-plane wheel is required; set OPEN_YR_DATA_PLANE_WHEEL_PATH or OPEN_YR_DATA_PLANE_WHEEL_URL"
fi
if [[ -z "${open_yr_data_plane_wheel_sha256}" ]]; then
  die "OPEN_YR_DATA_PLANE_WHEEL_SHA256 is required with the Rust data-plane wheel"
fi

node_build_contexts=(--build-context "open_yr_data_plane_wheel=.")
data_plane_context_dir=""
open_yr_data_plane_wheel_name=""
if [[ -n "${open_yr_data_plane_wheel_path}" ]]; then
  if [[ ! -f "${open_yr_data_plane_wheel_path}" ]]; then
    die "Rust data-plane wheel does not exist: ${open_yr_data_plane_wheel_path}"
  fi
  actual_data_plane_sha256="$(sha256sum "${open_yr_data_plane_wheel_path}" | awk '{print $1}')"
  if [[ "${actual_data_plane_sha256}" != "${open_yr_data_plane_wheel_sha256}" ]]; then
    die "Rust data-plane wheel checksum mismatch: expected ${open_yr_data_plane_wheel_sha256}, got ${actual_data_plane_sha256}"
  fi
  open_yr_data_plane_wheel_name="$(basename "${open_yr_data_plane_wheel_path}")"
  case "${open_yr_data_plane_wheel_name}" in
    openyuanrong_data_plane-*.whl) ;;
    *) die "Rust data-plane path must retain its openyuanrong_data_plane wheel filename" ;;
  esac
  data_plane_context_dir="$(mktemp -d)"
  trap 'rm -rf -- "${data_plane_context_dir}"' EXIT
  ln "${open_yr_data_plane_wheel_path}" \
    "${data_plane_context_dir}/${open_yr_data_plane_wheel_name}"
  node_build_contexts=(
    --build-context "open_yr_data_plane_wheel=${data_plane_context_dir}"
  )
fi

info "building ${runtime_image} with runtime profile ${runtime_profile}"
docker build \
  -f builder/runtime.Dockerfile \
  "${docker_build_common_args[@]}" \
  --target "runtime-${runtime_profile}" \
  "${runtime_build_args[@]}" \
  -t "${runtime_image}" \
  .

info "building ${all_in_one_image}"
node_build_args=(
  --build-arg "AKERNEL_RUNTIME_IMAGE=${runtime_image}"
  --build-arg "AKERNEL_RUNTIME_PROFILE=${runtime_profile}"
  --build-arg "AKERNEL_ENABLE_KATA=${AKERNEL_ENABLE_KATA:-true}"
  --build-arg "AKERNEL_ENABLE_RUNC=${AKERNEL_ENABLE_RUNC:-false}"
  --build-arg "AKERNEL_ENABLE_FIRECRACKER=${AKERNEL_ENABLE_FIRECRACKER:-true}"
  --build-arg "AKERNEL_VERSION=${akernel_version}"
  --build-arg "AKERNEL_REVISION=${akernel_revision}"
)
if [[ -z "${gvisor_release}" ||
      -z "${gvisor_amd64_sha512}" ||
      -z "${gvisor_amd64_url}" ]]; then
  die "GVISOR_RELEASE, GVISOR_AMD64_URL, and GVISOR_AMD64_SHA512 must be set together"
fi
node_build_args+=(
  --build-arg "GVISOR_RELEASE=${gvisor_release}"
  --build-arg "GVISOR_AMD64_URL=${gvisor_amd64_url}"
  --build-arg "GVISOR_AMD64_SHA512=${gvisor_amd64_sha512}"
)
if [[ -z "${firecracker_release}" ||
      -z "${firecracker_amd64_sha256}" ||
      -z "${firecracker_amd64_url}" ]]; then
  die "FIRECRACKER_RELEASE, FIRECRACKER_AMD64_URL, and FIRECRACKER_AMD64_SHA256 must be set together"
fi
node_build_args+=(
  --build-arg "FIRECRACKER_RELEASE=${firecracker_release}"
  --build-arg "FIRECRACKER_AMD64_URL=${firecracker_amd64_url}"
  --build-arg "FIRECRACKER_AMD64_SHA256=${firecracker_amd64_sha256}"
)
if [[ -n "${open_yr_core_wheel_url}" || -n "${open_yr_core_wheel_sha256}" ]]; then
  if [[ -z "${open_yr_core_wheel_url}" || -z "${open_yr_core_wheel_sha256}" ]]; then
    die "OPEN_YR_CORE_WHEEL_URL and OPEN_YR_CORE_WHEEL_SHA256 must be set together"
  fi
  node_build_args+=(
    --build-arg "OPEN_YR_CORE_WHEEL_URL=${open_yr_core_wheel_url}"
    --build-arg "OPEN_YR_CORE_WHEEL_SHA256=${open_yr_core_wheel_sha256}"
  )
fi

node_build_args+=(
  --build-arg "OPEN_YR_DATA_PLANE_WHEEL_URL=${open_yr_data_plane_wheel_url}"
  --build-arg "OPEN_YR_DATA_PLANE_WHEEL_SHA256=${open_yr_data_plane_wheel_sha256}"
  --build-arg "OPEN_YR_DATA_PLANE_WHEEL_NAME=${open_yr_data_plane_wheel_name}"
)
docker build \
  -f builder/node.Dockerfile \
  "${docker_build_common_args[@]}" \
  "${node_build_contexts[@]}" \
  "${node_build_args[@]}" \
  -t "${all_in_one_image}" \
  .

info "built ${all_in_one_image}"
