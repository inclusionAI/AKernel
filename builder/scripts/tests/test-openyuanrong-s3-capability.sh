#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
detector="${root}/builder/scripts/detect-openyuanrong-s3-snapshot-capability.sh"
task7_source_root="${YR_S3_TASK7_SOURCE_ROOT:-}"
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
marker="${tmp}/.akernel-s3-snapshot-capable"
sentinel="${tmp}/detector-must-not-remove"

config_parser_evidence=(
  '--snapshot_s3_provider) SNAPSHOT_S3_PROVIDER=$2 && shift 2 ;;'
  '--snapshot_s3_endpoint) SNAPSHOT_S3_ENDPOINT=$2 && shift 2 ;;'
  '--snapshot_s3_region) SNAPSHOT_S3_REGION=$2 && shift 2 ;;'
  '--snapshot_s3_bucket) SNAPSHOT_S3_BUCKET=$2 && shift 2 ;;'
  '--snapshot_s3_use_https) SNAPSHOT_S3_USE_HTTPS=$2 && shift 2 ;;'
  '--snapshot_s3_path_style) SNAPSHOT_S3_PATH_STYLE=$2 && shift 2 ;;'
)
config_backend_evidence=(
  'if [ "X${SNAPSHOT_STORAGE_MODE}" != "Xlocal_only" ]; then'
  'case "${SNAPSHOT_STORAGE_BACKEND}" in'
  '      s3)'
  'case "${SNAPSHOT_S3_PROVIDER}" in'
  'generic|obs|oss) ;;'
  'log_error "snapshot_s3_provider must be generic, obs, or oss"'
  'if [ -z "${SNAPSHOT_S3_ENDPOINT}" ] || [ -z "${SNAPSHOT_S3_REGION}" ] || [ -z "${SNAPSHOT_S3_BUCKET}" ]; then'
  'log_error "snapshot_s3_endpoint, snapshot_s3_region, and snapshot_s3_bucket are required for S3"'
  'case "${SNAPSHOT_S3_USE_HTTPS}" in'
  'log_error "snapshot_s3_use_https must be true or false"'
  'case "${SNAPSHOT_S3_PATH_STYLE}" in'
  'log_error "snapshot_s3_path_style must be true or false"'
  'if [ "X${SNAPSHOT_S3_PROVIDER}" = "Xoss" ] && [ "X${SNAPSHOT_S3_PATH_STYLE}" = "Xtrue" ]; then'
  'log_error "snapshot S3 OSS provider requires virtual-host addressing"'
  'if [ -z "${SNAPSHOT_S3_ACCESS_KEY:-}" ] || [ -z "${SNAPSHOT_S3_SECRET_KEY:-}" ]; then'
  'log_error "SNAPSHOT_S3_ACCESS_KEY and SNAPSHOT_S3_SECRET_KEY are required for S3"'
  'log_error "distributed snapshot storage backend must be datasystem, obs, or s3"'
)
config_export_evidence=(
  'export SNAPSHOT_S3_PROVIDER SNAPSHOT_S3_ENDPOINT SNAPSHOT_S3_REGION SNAPSHOT_S3_BUCKET'
  'export SNAPSHOT_S3_USE_HTTPS SNAPSHOT_S3_PATH_STYLE'
  'export SNAPSHOT_S3_ACCESS_KEY SNAPSHOT_S3_SECRET_KEY SNAPSHOT_S3_SECURITY_TOKEN'
)
install_evidence=(
  '--snapshot_s3_provider="${SNAPSHOT_S3_PROVIDER:-generic}"'
  '--snapshot_s3_endpoint="${SNAPSHOT_S3_ENDPOINT:-}"'
  '--snapshot_s3_region="${SNAPSHOT_S3_REGION:-}"'
  '--snapshot_s3_bucket="${SNAPSHOT_S3_BUCKET:-}"'
  '--snapshot_s3_use_https="${SNAPSHOT_S3_USE_HTTPS:-true}"'
  '--snapshot_s3_path_style="${SNAPSHOT_S3_PATH_STYLE:-false}"'
)
install_path_evidence=(
  'function install_function_proxy() {'
  'function install_function_agent_and_runtime_manager_in_the_same_process() {'
)
agent_evidence=(
  snapshot_s3_provider
  snapshot_s3_endpoint
  snapshot_s3_region
  snapshot_s3_bucket
  snapshot_s3_use_https
  snapshot_s3_path_style
  SNAPSHOT_S3_ACCESS_KEY
  SNAPSHOT_S3_SECRET_KEY
  SNAPSHOT_S3_SECURITY_TOKEN
  'snapshot S3 object exceeds the 5 GiB limit'
  'published object snapshot failed postcondition verification'
)

copy_task7_sources() {
  local config_source="${task7_source_root}/deploy/process/config.sh"
  local install_source="${task7_source_root}/functionsystem/scripts/deploy/function_system/install.sh"
  if [[ ! -f "${install_source}" ]]; then
    install_source="${task7_source_root}/functionsystem/deploy/install.sh"
  fi
  if [[ ! -f "${config_source}" || ! -f "${install_source}" ]]; then
    echo "YR_S3_TASK7_SOURCE_ROOT does not contain reviewed Task 7 config/install sources" >&2
    exit 1
  fi
  cp "${config_source}" "${tmp}/deploy/process/config.sh"
  cp "${install_source}" "${tmp}/functionsystem/deploy/install.sh"
}

write_synthetic_task7_sources() {
  {
    printf '%s\n' \
      'parse_snapshot_args() {' \
      '  while true; do' \
      '    case "$1" in'
    printf '    %s\n' "${config_parser_evidence[@]}"
    printf '%s\n' \
      '    --) shift && break ;;' \
      '    esac' \
      '  done' \
      '  if [ "X${SNAPSHOT_STORAGE_MODE}" != "Xlocal_only" ]; then' \
      '    case "${SNAPSHOT_STORAGE_BACKEND}" in' \
      '      datasystem|obs) ;;' \
      '      s3)' \
      '        case "${SNAPSHOT_S3_PROVIDER}" in' \
      '          generic|obs|oss) ;;' \
      '          *) log_error "snapshot_s3_provider must be generic, obs, or oss"; return 1 ;;' \
      '        esac' \
      '        if [ -z "${SNAPSHOT_S3_ENDPOINT}" ] || [ -z "${SNAPSHOT_S3_REGION}" ] || [ -z "${SNAPSHOT_S3_BUCKET}" ]; then' \
      '          log_error "snapshot_s3_endpoint, snapshot_s3_region, and snapshot_s3_bucket are required for S3"' \
      '          return 1' \
      '        fi' \
      '        case "${SNAPSHOT_S3_USE_HTTPS}" in' \
      '          true|false) ;;' \
      '          *) log_error "snapshot_s3_use_https must be true or false"; return 1 ;;' \
      '        esac' \
      '        case "${SNAPSHOT_S3_PATH_STYLE}" in' \
      '          true|false) ;;' \
      '          *) log_error "snapshot_s3_path_style must be true or false"; return 1 ;;' \
      '        esac' \
      '        if [ "X${SNAPSHOT_S3_PROVIDER}" = "Xoss" ] && [ "X${SNAPSHOT_S3_PATH_STYLE}" = "Xtrue" ]; then' \
      '          log_error "snapshot S3 OSS provider requires virtual-host addressing"' \
      '          return 1' \
      '        fi' \
      '        if [ -z "${SNAPSHOT_S3_ACCESS_KEY:-}" ] || [ -z "${SNAPSHOT_S3_SECRET_KEY:-}" ]; then' \
      '          log_error "SNAPSHOT_S3_ACCESS_KEY and SNAPSHOT_S3_SECRET_KEY are required for S3"' \
      '          return 1' \
      '        fi' \
      '        ;;' \
      '      *)' \
      '        log_error "distributed snapshot storage backend must be datasystem, obs, or s3"' \
      '        return 1' \
      '        ;;' \
      '    esac' \
      '  fi' \
      '}' \
      'export_config() {' \
      '  export SNAPSHOT_S3_PROVIDER SNAPSHOT_S3_ENDPOINT SNAPSHOT_S3_REGION SNAPSHOT_S3_BUCKET' \
      '  export SNAPSHOT_S3_USE_HTTPS SNAPSHOT_S3_PATH_STYLE' \
      '  export SNAPSHOT_S3_ACCESS_KEY SNAPSHOT_S3_SECRET_KEY SNAPSHOT_S3_SECURITY_TOKEN' \
      '}'
  } >"${tmp}/deploy/process/config.sh"

  {
    printf '%s\n' 'function install_function_proxy() {' '  merge_process_args="'
    printf '    %s \\\n' "${install_evidence[@]}"
    printf '%s\n' '  "' '}' \
      'function install_function_agent_and_runtime_manager_in_the_same_process() {' \
      '  agent_args=('
    printf '    %s\n' "${install_evidence[@]}"
    printf '%s\n' '  )' '}'
  } >"${tmp}/functionsystem/deploy/install.sh"
}

write_complete_package() {
  rm -rf "${tmp}/deploy" "${tmp}/functionsystem"
  mkdir -p "${tmp}/deploy/process" "${tmp}/functionsystem/deploy" \
    "${tmp}/functionsystem/bin"
  if [[ -n "${task7_source_root}" ]]; then
    copy_task7_sources
  else
    write_synthetic_task7_sources
  fi
  printf '%s\n' "${agent_evidence[@]}" \
    >"${tmp}/functionsystem/bin/function_agent"
  chmod +x "${tmp}/functionsystem/bin/function_agent"
}

remove_first_occurrence() {
  local file=$1
  local evidence=$2
  python3 - "${file}" "${evidence}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
evidence = sys.argv[2]
text = path.read_text()
if evidence not in text:
    raise SystemExit(f"fixture does not contain evidence: {evidence}")
path.write_text(text.replace(evidence, "", 1))
PY
}

remove_from_function_body() {
  local file=$1
  local function_header=$2
  local evidence=$3
  python3 - "${file}" "${function_header}" "${evidence}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
header = sys.argv[2]
evidence = sys.argv[3]
lines = path.read_text().splitlines(keepends=True)
try:
    start = next(index for index, line in enumerate(lines) if line.rstrip("\r\n") == header)
    end = next(index for index in range(start + 1, len(lines)) if lines[index].rstrip("\r\n") == "}")
    target = next(index for index in range(start + 1, end) if evidence in lines[index])
except StopIteration as error:
    raise SystemExit(f"function fixture does not contain evidence: {header}: {evidence}") from error
lines[target] = lines[target].replace(evidence, "", 1)
path.write_text("".join(lines))
PY
}

insert_before_function_close() {
  local file=$1
  local function_header=$2
  local line=$3
  python3 - "${file}" "${function_header}" "${line}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
header = sys.argv[2]
inserted = sys.argv[3]
lines = path.read_text().splitlines(keepends=True)
try:
    start = next(index for index, line in enumerate(lines) if line.rstrip("\r\n") == header)
    end = next(index for index in range(start + 1, len(lines)) if lines[index].rstrip("\r\n") == "}")
except StopIteration as error:
    raise SystemExit(f"function fixture is malformed: {header}") from error
lines.insert(end, f"    {inserted}\n")
path.write_text("".join(lines))
PY
}

assert_fixture_occurrences() {
  local file=$1
  local expected_count=$2
  local evidence=$3
  local actual_count
  actual_count=$(grep -Fc -- "${evidence}" "${file}" || true)
  if [[ "${actual_count}" -ne "${expected_count}" ]]; then
    echo "fixture expected ${expected_count} occurrences of ${evidence}, got ${actual_count}" >&2
    exit 1
  fi
}

assert_capable() {
  local description=$1
  "${detector}" "${tmp}"
  if [[ ! -f "${marker}" ]]; then
    echo "${description}: capable package did not create marker" >&2
    exit 1
  fi
  [[ -f "${sentinel}" ]]
}

assert_not_capable() {
  local description=$1
  touch "${marker}"
  "${detector}" "${tmp}"
  if [[ -e "${marker}" ]]; then
    echo "${description}: incomplete package retained capability marker" >&2
    exit 1
  fi
  [[ -f "${sentinel}" ]]
}

touch "${sentinel}"
write_complete_package
assert_capable "complete Task 7 evidence"

write_complete_package
rm "${tmp}/deploy/process/config.sh"
assert_not_capable "missing process config"

write_complete_package
rm "${tmp}/functionsystem/deploy/install.sh"
assert_not_capable "missing install wiring"

write_complete_package
rm "${tmp}/functionsystem/bin/function_agent"
assert_not_capable "missing FunctionAgent"

for item in "${config_parser_evidence[@]}" "${config_backend_evidence[@]}" \
  "${config_export_evidence[@]}"; do
  write_complete_package
  remove_first_occurrence "${tmp}/deploy/process/config.sh" "${item}"
  assert_not_capable "partial process config without ${item}"
done

for item in "${install_evidence[@]}"; do
  write_complete_package
  remove_first_occurrence "${tmp}/functionsystem/deploy/install.sh" "${item}"
  assert_not_capable "install wiring has only one ${item} occurrence"
done

for item in "${install_evidence[@]}"; do
  write_complete_package
  remove_from_function_body "${tmp}/functionsystem/deploy/install.sh" \
    "${install_path_evidence[0]}" "${item}"
  insert_before_function_close "${tmp}/functionsystem/deploy/install.sh" \
    "${install_path_evidence[1]}" "${item}"
  assert_fixture_occurrences "${tmp}/functionsystem/deploy/install.sh" 2 "${item}"
  assert_not_capable "global count two but merged launch lacks ${item}"
done

for item in "${install_evidence[@]}"; do
  write_complete_package
  remove_from_function_body "${tmp}/functionsystem/deploy/install.sh" \
    "${install_path_evidence[0]}" "${item}"
  insert_before_function_close "${tmp}/functionsystem/deploy/install.sh" \
    "${install_path_evidence[0]}" "# ${item} \\"
  assert_fixture_occurrences "${tmp}/functionsystem/deploy/install.sh" 2 "${item}"
  assert_not_capable "comment must not replace merged launch argument ${item}"
done

for item in "${install_path_evidence[@]}"; do
  write_complete_package
  remove_first_occurrence "${tmp}/functionsystem/deploy/install.sh" "${item}"
  assert_not_capable "install wiring lacks startup path ${item}"
done

write_complete_package
printf '%s\n' "${install_evidence[0]}" >>"${tmp}/functionsystem/deploy/install.sh"
assert_not_capable "install wiring has an unexpected third S3 argument occurrence"

write_complete_package
printf '%s\n' '--snapshot_s3_access_key="${SNAPSHOT_S3_ACCESS_KEY:-}"' \
  >>"${tmp}/functionsystem/deploy/install.sh"
assert_not_capable "credential argv contamination"

for item in "${agent_evidence[@]}"; do
  write_complete_package
  remove_first_occurrence "${tmp}/functionsystem/bin/function_agent" "${item}"
  chmod +x "${tmp}/functionsystem/bin/function_agent"
  assert_not_capable "partial FunctionAgent evidence without ${item}"
done

write_complete_package
remove_first_occurrence "${tmp}/functionsystem/bin/function_agent" \
  'snapshot S3 object exceeds the 5 GiB limit'
printf '%s\n' 'remote S3 snapshot exceeds the 5 GiB capability limit' \
  >>"${tmp}/functionsystem/bin/function_agent"
assert_not_capable "stale 5 GiB prototype string"

write_complete_package
chmod -x "${tmp}/functionsystem/bin/function_agent"
assert_not_capable "non-executable FunctionAgent"

write_complete_package
printf '%s\n' 'legacy function agent' >"${tmp}/functionsystem/bin/function_agent"
chmod +x "${tmp}/functionsystem/bin/function_agent"
assert_not_capable "legacy package"

if [[ -n "${task7_source_root}" ]]; then
  echo "openYuanRong S3 capability detection checks passed with Task 7 source fixtures"
else
  echo "openYuanRong S3 capability detection checks passed with representative fixtures"
fi
