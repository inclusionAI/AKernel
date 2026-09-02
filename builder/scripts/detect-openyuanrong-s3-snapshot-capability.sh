#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

yr_root="${1:?usage: $0 YR_ROOT}"
marker="${yr_root}/.akernel-s3-snapshot-capable"
config="${yr_root}/deploy/process/config.sh"
install="${yr_root}/functionsystem/deploy/install.sh"
agent="${yr_root}/functionsystem/bin/function_agent"

rm -f "${marker}"

contains_all_text() {
  local file=$1
  shift
  local evidence
  for evidence in "$@"; do
    grep -Fq -- "${evidence}" "${file}" || return 1
  done
}

contains_all_binary() {
  local file=$1
  shift
  local evidence
  for evidence in "$@"; do
    grep -aFq -- "${evidence}" "${file}" || return 1
  done
}

extract_install_function_body() {
  local file=$1
  local function_header=$2
  awk -v function_header="${function_header}" '
    $0 == function_header {
      in_function = 1
      next
    }
    in_function && $0 == "}" {
      found_close = 1
      exit
    }
    in_function {
      print
    }
    END {
      if (!in_function || !found_close) {
        exit 1
      }
    }
  ' "${file}"
}

normalize_install_lines() {
  awk '
    {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      if (line ~ /^#/) {
        next
      }
      sub(/[[:space:]]*\\[[:space:]]*$/, "", line)
      sub(/[[:space:]]*$/, "", line)
      if (line != "") {
        print line
      }
    }
  '
}

has_exact_normalized_occurrences() {
  local normalized_text=$1
  local expected_count=$2
  local evidence=$3
  local actual_count
  actual_count=$(printf '%s\n' "${normalized_text}" | grep -Fxc -- "${evidence}" || true)
  [[ "${actual_count}" -eq "${expected_count}" ]]
}

[[ -f "${config}" && -f "${install}" && -x "${agent}" ]] || exit 0

contains_all_text "${config}" \
  '--snapshot_s3_provider) SNAPSHOT_S3_PROVIDER=$2 && shift 2 ;;' \
  '--snapshot_s3_endpoint) SNAPSHOT_S3_ENDPOINT=$2 && shift 2 ;;' \
  '--snapshot_s3_region) SNAPSHOT_S3_REGION=$2 && shift 2 ;;' \
  '--snapshot_s3_bucket) SNAPSHOT_S3_BUCKET=$2 && shift 2 ;;' \
  '--snapshot_s3_use_https) SNAPSHOT_S3_USE_HTTPS=$2 && shift 2 ;;' \
  '--snapshot_s3_path_style) SNAPSHOT_S3_PATH_STYLE=$2 && shift 2 ;;' \
  'if [ "X${SNAPSHOT_STORAGE_MODE}" != "Xlocal_only" ]; then' \
  'case "${SNAPSHOT_STORAGE_BACKEND}" in' \
  '      s3)' \
  'case "${SNAPSHOT_S3_PROVIDER}" in' \
  'generic|obs|oss) ;;' \
  'log_error "snapshot_s3_provider must be generic, obs, or oss"' \
  'if [ -z "${SNAPSHOT_S3_ENDPOINT}" ] || [ -z "${SNAPSHOT_S3_REGION}" ] || [ -z "${SNAPSHOT_S3_BUCKET}" ]; then' \
  'log_error "snapshot_s3_endpoint, snapshot_s3_region, and snapshot_s3_bucket are required for S3"' \
  'case "${SNAPSHOT_S3_USE_HTTPS}" in' \
  'log_error "snapshot_s3_use_https must be true or false"' \
  'case "${SNAPSHOT_S3_PATH_STYLE}" in' \
  'log_error "snapshot_s3_path_style must be true or false"' \
  'if [ "X${SNAPSHOT_S3_PROVIDER}" = "Xoss" ] && [ "X${SNAPSHOT_S3_PATH_STYLE}" = "Xtrue" ]; then' \
  'log_error "snapshot S3 OSS provider requires virtual-host addressing"' \
  'if [ -z "${SNAPSHOT_S3_ACCESS_KEY:-}" ] || [ -z "${SNAPSHOT_S3_SECRET_KEY:-}" ]; then' \
  'log_error "SNAPSHOT_S3_ACCESS_KEY and SNAPSHOT_S3_SECRET_KEY are required for S3"' \
  'log_error "distributed snapshot storage backend must be datasystem, obs, or s3"' \
  'export SNAPSHOT_S3_PROVIDER SNAPSHOT_S3_ENDPOINT SNAPSHOT_S3_REGION SNAPSHOT_S3_BUCKET' \
  'export SNAPSHOT_S3_USE_HTTPS SNAPSHOT_S3_PATH_STYLE' \
  'export SNAPSHOT_S3_ACCESS_KEY SNAPSHOT_S3_SECRET_KEY SNAPSHOT_S3_SECURITY_TOKEN' || exit 0

normalized_install=$(normalize_install_lines <"${install}")
proxy_install_body=$(extract_install_function_body "${install}" \
  'function install_function_proxy() {' | normalize_install_lines) || exit 0
agent_install_body=$(extract_install_function_body "${install}" \
  'function install_function_agent_and_runtime_manager_in_the_same_process() {' \
  | normalize_install_lines) || exit 0

for install_argument in \
  '--snapshot_s3_provider="${SNAPSHOT_S3_PROVIDER:-generic}"' \
  '--snapshot_s3_endpoint="${SNAPSHOT_S3_ENDPOINT:-}"' \
  '--snapshot_s3_region="${SNAPSHOT_S3_REGION:-}"' \
  '--snapshot_s3_bucket="${SNAPSHOT_S3_BUCKET:-}"' \
  '--snapshot_s3_use_https="${SNAPSHOT_S3_USE_HTTPS:-true}"' \
  '--snapshot_s3_path_style="${SNAPSHOT_S3_PATH_STYLE:-false}"'; do
  has_exact_normalized_occurrences "${proxy_install_body}" 1 "${install_argument}" || exit 0
  has_exact_normalized_occurrences "${agent_install_body}" 1 "${install_argument}" || exit 0
  has_exact_normalized_occurrences "${normalized_install}" 2 "${install_argument}" || exit 0
done

if printf '%s\n' "${normalized_install}" \
  | grep -Eq -- '--snapshot_s3_(access_key|secret_key|security_token)(=|[[:space:]])'; then
  exit 0
fi

contains_all_binary "${agent}" \
  snapshot_s3_provider \
  snapshot_s3_endpoint \
  snapshot_s3_region \
  snapshot_s3_bucket \
  snapshot_s3_use_https \
  snapshot_s3_path_style \
  SNAPSHOT_S3_ACCESS_KEY \
  SNAPSHOT_S3_SECRET_KEY \
  SNAPSHOT_S3_SECURITY_TOKEN \
  'snapshot S3 object exceeds the 5 GiB limit' \
  'published object snapshot failed postcondition verification' || exit 0

touch "${marker}"
