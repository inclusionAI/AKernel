#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

AKERNEL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

info() {
  echo "==> $*"
}

warn() {
  echo "WARNING: $*" >&2
}

require_cmd() {
  local missing=0
  for cmd in "$@"; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
      echo "missing command: ${cmd}" >&2
      missing=1
    fi
  done
  if [[ "${missing}" -ne 0 ]]; then
    exit 1
  fi
}

state_dir() {
  local env_name="$1"
  printf '%s/.akernel/%s\n' "${AKERNEL_REPO_ROOT}" "${env_name}"
}

terraform_plugin_cache_dir() {
  printf '%s/.akernel/terraform-plugin-cache\n' "${AKERNEL_REPO_ROOT}"
}

setup_terraform_env() {
  local env_name="$1"
  local tf_dir="$2"
  local dir
  dir="$(state_dir "${env_name}")"

  local terraform_data_dir="${TERRAFORM_DATA_DIR:-${dir}/.terraform}"
  local plugin_cache_dir="${TF_PLUGIN_CACHE_DIR:-${TERRAFORM_PLUGIN_CACHE_DIR:-$(terraform_plugin_cache_dir)}}"

  mkdir -p "${terraform_data_dir}" "${plugin_cache_dir}"

  local legacy_provider_dir="${tf_dir}/.terraform/providers"
  if [[ -d "${legacy_provider_dir}" && ! -e "${plugin_cache_dir}/registry.terraform.io" ]]; then
    info "seeding Terraform plugin cache from ${legacy_provider_dir}"
    cp -a "${legacy_provider_dir}/." "${plugin_cache_dir}/" 2>/dev/null || true
  fi

  export TF_DATA_DIR="${terraform_data_dir}"
  export TF_PLUGIN_CACHE_DIR="${plugin_cache_dir}"
}

vendor_dir() {
  local vendor="$1"
  case "${vendor}" in
    aliyun | huaweicloud)
      printf '%s/deploy/terraform/%s\n' "${AKERNEL_REPO_ROOT}" "${vendor}"
      ;;
    *)
      die "unsupported vendor: ${vendor}"
      ;;
  esac
}

normalize_vendor() {
  local vendor
  vendor="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "${vendor}" in
    aliyun | alicloud)
      printf 'aliyun'
      ;;
    huaweicloud | huawei)
      printf 'huaweicloud'
      ;;
    *)
      die "unsupported vendor: $1"
      ;;
  esac
}

load_env_config() {
  local env_name="$1"
  local dir
  dir="$(state_dir "${env_name}")"
  local file="${dir}/config.env"
  [[ -f "${file}" ]] || die "missing ${file}; run make config ENV=${env_name} first"
  set -a
  # shellcheck source=/dev/null
  source "${file}"
  set +a
}

shell_quote() {
  printf '%q' "$1"
}

csv_to_hcl_list() {
  local csv="$1"
  local out="["
  local first=1
  local item
  IFS=',' read -ra items <<<"${csv}"
  for item in "${items[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [[ -n "${item}" ]] || continue
    if [[ "${first}" -eq 0 ]]; then
      out+=", "
    fi
    out+="\"${item}\""
    first=0
  done
  out+="]"
  printf '%s\n' "${out}"
}

generate_hex_seed() {
  python3 - <<'PY'
import secrets
print(secrets.token_hex(32).upper())
PY
}

generate_password() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
}
