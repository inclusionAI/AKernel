#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
helper="${repo_root}/builder/scripts/yr_pause_resume_args.sh"
standalone_start="${repo_root}/deploy/standalone/start.sh"
systemd_unit="${repo_root}/builder/systemd_services/yuanrong.service"
tmp_dir=$(mktemp -d)
trap 'rm -rf "${tmp_dir}"' EXIT
rrt_capability="${tmp_dir}/rrt-capable"
s3_capability="${tmp_dir}/s3-capable"
checkpoint_dir="${tmp_dir}/checkpoints"
output="${tmp_dir}/output"
touch "${rrt_capability}" "${s3_capability}"

source "${helper}"

fail() {
  echo "$1" >&2
  exit 1
}

assert_output_credentials_unset() {
  [[ -z "${SNAPSHOT_S3_ACCESS_KEY+x}" ]] || fail "stale S3 access key remained exported"
  [[ -z "${SNAPSHOT_S3_SECRET_KEY+x}" ]] || fail "stale S3 secret key remained exported"
  [[ -z "${SNAPSHOT_S3_SECURITY_TOKEN+x}" ]] || fail "stale S3 security token remained exported"
}

assert_no_credential_canary() {
  local candidate=$1
  local canary
  for canary in AK_CANARY_MUST_NOT_REACH_ARGV SK_CANARY_MUST_NOT_REACH_ARGV \
    TOKEN_CANARY_MUST_NOT_REACH_ARGV STALE_AK_CANARY STALE_SK_CANARY STALE_TOKEN_CANARY; do
    if [[ "${candidate}" == *"${canary}"* ]]; then
      fail "S3 credential value reached argv or command output"
    fi
  done
}

set_s3_environment() {
  export AKERNEL_SNAPSHOT_STORAGE_BACKEND=s3
  export AKERNEL_SNAPSHOT_STORAGE_MODE="${3:-distributed_cache}"
  export AKERNEL_SNAPSHOT_S3_PROVIDER=$1
  export AKERNEL_SNAPSHOT_S3_ENDPOINT="${1}.private.example:9000"
  export AKERNEL_SNAPSHOT_S3_REGION=test-region-1
  export AKERNEL_SNAPSHOT_S3_BUCKET=akernel-test
  export AKERNEL_SNAPSHOT_S3_ACCESS_KEY=AK_CANARY_MUST_NOT_REACH_ARGV
  export AKERNEL_SNAPSHOT_S3_SECRET_KEY=SK_CANARY_MUST_NOT_REACH_ARGV
  export AKERNEL_SNAPSHOT_S3_SECURITY_TOKEN=TOKEN_CANARY_MUST_NOT_REACH_ARGV
  export AKERNEL_SNAPSHOT_S3_USE_HTTPS=true
  export AKERNEL_SNAPSHOT_S3_PATH_STYLE=$2
}

assert_s3_success() {
  local provider=$1
  local path_style=$2
  local storage_mode=${3:-distributed_cache}
  set_s3_environment "${provider}" "${path_style}" "${storage_mode}"
  : >"${output}"
  configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" false \
    "${s3_capability}" >"${output}" 2>&1
  local expected="--snapshot_storage_backend s3 --checkpoint_dir ${checkpoint_dir}"
  expected+=" --snapshot_storage_mode ${storage_mode}"
  expected+=" --snapshot_s3_provider ${provider}"
  expected+=" --snapshot_s3_endpoint ${provider}.private.example:9000"
  expected+=" --snapshot_s3_region test-region-1 --snapshot_s3_bucket akernel-test"
  expected+=" --snapshot_s3_use_https true --snapshot_s3_path_style ${path_style}"
  [[ "${snapshot_args[*]}" == "${expected}" ]] || fail "${provider} S3 argv is not exact"
  [[ "${standalone_snapshot_args[*]}" == "${expected}" ]] \
    || fail "${provider} standalone S3 argv is not exact"
  [[ "${SNAPSHOT_S3_ACCESS_KEY}" == "${AKERNEL_SNAPSHOT_S3_ACCESS_KEY}" ]]
  [[ "${SNAPSHOT_S3_SECRET_KEY}" == "${AKERNEL_SNAPSHOT_S3_SECRET_KEY}" ]]
  [[ "${SNAPSHOT_S3_SECURITY_TOKEN}" == "${AKERNEL_SNAPSHOT_S3_SECURITY_TOKEN}" ]]
  assert_no_credential_canary "${snapshot_args[*]} ${standalone_snapshot_args[*]} $(<"${output}")"
}

assert_s3_failure() {
  local description=$1
  : >"${output}"
  export SNAPSHOT_S3_ACCESS_KEY=STALE_AK_CANARY
  export SNAPSHOT_S3_SECRET_KEY=STALE_SK_CANARY
  export SNAPSHOT_S3_SECURITY_TOKEN=STALE_TOKEN_CANARY
  if configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" false \
    "${s3_capability}" >"${output}" 2>&1; then
    fail "${description}: invalid S3 configuration was accepted"
  fi
  [[ ${#snapshot_args[@]} -eq 0 ]] || fail "${description}: failed call returned snapshot argv"
  [[ ${#standalone_snapshot_args[@]} -eq 0 ]] \
    || fail "${description}: failed call returned standalone argv"
  assert_output_credentials_unset
  assert_no_credential_canary "$(<"${output}")"
}

# Default DataSystem behavior remains exact and removes stale S3 output.
unset AKERNEL_SNAPSHOT_STORAGE_BACKEND
export SNAPSHOT_S3_ACCESS_KEY=STALE_AK_CANARY
export SNAPSHOT_S3_SECRET_KEY=STALE_SK_CANARY
export SNAPSHOT_S3_SECURITY_TOKEN=STALE_TOKEN_CANARY
configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" true "${s3_capability}"
expected="--snapshot_storage_backend datasystem --checkpoint_dir ${checkpoint_dir} --data_system_enable true"
[[ "${standalone_snapshot_args[*]}" == "${expected}" ]] || fail "DataSystem argv changed"
assert_output_credentials_unset

configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" false "${s3_capability}"
expected="--snapshot_storage_backend datasystem --checkpoint_dir ${checkpoint_dir}"
[[ "${snapshot_args[*]}" == "${expected}" ]] || fail "cluster DataSystem argv changed"
[[ "${standalone_snapshot_args[*]}" == "${expected}" ]] || fail "standalone=false DataSystem argv changed"
assert_output_credentials_unset

export AKERNEL_SNAPSHOT_STORAGE_MODE=invalid
configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" false "${s3_capability}"
[[ "${snapshot_args[*]}" == "${expected}" ]] \
  || fail "DataSystem unexpectedly consumed the S3-only storage mode"
unset AKERNEL_SNAPSHOT_STORAGE_MODE

assert_s3_success generic true
assert_s3_success obs true
assert_s3_success oss false
assert_s3_success generic false distributed_only

set_s3_environment generic false distributed_cache
configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" true "${s3_capability}"
[[ "${standalone_snapshot_args[*]}" == "${snapshot_args[*]} --data_system_enable true" ]] \
  || fail "standalone S3 argv did not preserve mode before DataSystem enablement"

set_s3_environment generic false
unset AKERNEL_SNAPSHOT_STORAGE_MODE
configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" false "${s3_capability}"
[[ " ${snapshot_args[*]} " == *" --snapshot_storage_mode distributed_cache "* ]] \
  || fail "S3 storage mode did not default to distributed_cache"

set_s3_environment generic false
unset AKERNEL_SNAPSHOT_S3_SECURITY_TOKEN
configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" false "${s3_capability}"
[[ -n "${SNAPSHOT_S3_SECURITY_TOKEN+x}" && -z "${SNAPSHOT_S3_SECURITY_TOKEN}" ]] \
  || fail "optional S3 security token was not exported as empty"
assert_no_credential_canary "${snapshot_args[*]} ${standalone_snapshot_args[*]}"

# A missing capability marker fails before any argv or credential output is returned.
set_s3_environment generic true
rm "${s3_capability}"
assert_s3_failure "missing capability marker"
touch "${s3_capability}"

set_s3_environment generic true
export SNAPSHOT_S3_ACCESS_KEY=STALE_AK_CANARY
export SNAPSHOT_S3_SECRET_KEY=STALE_SK_CANARY
export SNAPSHOT_S3_SECURITY_TOKEN=STALE_TOKEN_CANARY
if configure_snapshot_args "${tmp_dir}/missing-rrt-capability" "${checkpoint_dir}" false \
  "${s3_capability}" >"${output}" 2>&1; then
  fail "missing RRT capability was accepted"
fi
[[ ${#snapshot_args[@]} -eq 0 && ${#standalone_snapshot_args[@]} -eq 0 ]] \
  || fail "missing RRT capability returned argv"
assert_output_credentials_unset
assert_no_credential_canary "$(<"${output}")"

for missing in \
  AKERNEL_SNAPSHOT_S3_PROVIDER AKERNEL_SNAPSHOT_S3_ENDPOINT \
  AKERNEL_SNAPSHOT_S3_REGION AKERNEL_SNAPSHOT_S3_BUCKET \
  AKERNEL_SNAPSHOT_S3_ACCESS_KEY AKERNEL_SNAPSHOT_S3_SECRET_KEY \
  AKERNEL_SNAPSHOT_S3_USE_HTTPS AKERNEL_SNAPSHOT_S3_PATH_STYLE; do
  set_s3_environment generic true
  unset "${missing}"
  assert_s3_failure "missing ${missing}"
done

set_s3_environment unknown false
assert_s3_failure "unknown provider"

set_s3_environment generic true
export AKERNEL_SNAPSHOT_S3_USE_HTTPS=yes
assert_s3_failure "non-boolean HTTPS"

set_s3_environment generic true
export AKERNEL_SNAPSHOT_S3_PATH_STYLE=1
assert_s3_failure "non-boolean path style"

set_s3_environment oss true
assert_s3_failure "OSS path-style addressing"

for invalid_mode in local_only invalid; do
  set_s3_environment generic false "${invalid_mode}"
  assert_s3_failure "invalid S3 storage mode ${invalid_mode}"
done

# Exercise the only validation that follows S3 credential collection explicitly.
set_s3_environment generic true
export SNAPSHOT_S3_ACCESS_KEY=STALE_AK_CANARY
export SNAPSHOT_S3_SECRET_KEY=STALE_SK_CANARY
export SNAPSHOT_S3_SECURITY_TOKEN=STALE_TOKEN_CANARY
if configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" invalid \
  "${s3_capability}" >"${output}" 2>&1; then
  fail "invalid standalone selector was accepted"
fi
[[ ${#snapshot_args[@]} -eq 0 && ${#standalone_snapshot_args[@]} -eq 0 ]] \
  || fail "invalid standalone selector returned argv"
assert_output_credentials_unset
assert_no_credential_canary "$(<"${output}")"

export AKERNEL_SNAPSHOT_STORAGE_BACKEND=obs
assert_s3_failure "legacy OBS backend"

# Standalone S3 configuration crosses sudo through a protected env-file; no
# snapshot value or variable name is exposed in Docker argv.
# Source the real function body without executing start.sh's main program.
standalone_functions="${tmp_dir}/standalone-functions.sh"
sed '/^# Main$/,$d' "${standalone_start}" >"${standalone_functions}"
source "${standalone_functions}"
docker_argv_capture="${tmp_dir}/docker-argv"
docker_env_capture="${tmp_dir}/docker-env"
docker_env_file_capture="${tmp_dir}/docker-env-file"
docker_env_mode_capture="${tmp_dir}/docker-env-mode"
docker_capture_status=0
docker_capture_signal=""

clear_docker_capture() {
  : >"${docker_argv_capture}"
  : >"${docker_env_capture}"
  : >"${docker_env_file_capture}"
  : >"${docker_env_mode_capture}"
}

sudo_sanitized_docker_capture() {
  printf '%s\n' "$@" >"${docker_argv_capture}"
  local env_file=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--env-file" ]]; then
      shift
      env_file="${1:-}"
      break
    fi
    shift
  done
  if [[ -n "${env_file}" ]]; then
    printf '%s\n' "${env_file}" >"${docker_env_file_capture}"
    local env_mode
    if env_mode=$(stat -f '%Lp' "${env_file}" 2>/dev/null); then
      :
    else
      env_mode=$(stat -c '%a' "${env_file}")
    fi
    printf '%s\n' "${env_mode}" >"${docker_env_mode_capture}"
    /usr/bin/env -i ENV_FILE="${env_file}" CAPTURE_FILE="${docker_env_capture}" \
      /bin/bash -c '
        while IFS= read -r assignment || [[ -n "${assignment}" ]]; do
          export "${assignment}"
        done <"${ENV_FILE}"
        /usr/bin/env | LC_ALL=C sort >"${CAPTURE_FILE}"
      '
  fi
  if [[ -n "${docker_capture_signal}" ]]; then
    local current_shell_pid
    current_shell_pid=$(/bin/sh -c 'printf "%s\n" "$PPID"')
    kill -s "${docker_capture_signal}" "${current_shell_pid}"
  fi
  return "${docker_capture_status}"
}
# Bash 3.2 preserves one empty word for an empty quoted array in command
# position. Put the capture function in the prefix so this test exercises the
# same command construction on both macOS and Linux.
DOCKER_PREFIX=(sudo_sanitized_docker_capture)
DOCKER_CMD=""
PROXY_RUN_ARGS=(--label task8-proxy=unused)
GPU_RUN_ARGS=(--label task8-gpu=unused)
IMAGE=container-image:task8
set_s3_environment generic true distributed_only
clear_docker_capture
start_node_container >"${output}" 2>&1
grep -Fxq -- '--env-file' "${docker_argv_capture}" \
  || fail "standalone S3 launch did not use an env-file"
[[ "$(grep -Fxc -- '--env-file' "${docker_argv_capture}")" -eq 1 ]] \
  || fail "standalone S3 launch emitted multiple env-file options"
if grep -Fq 'AKERNEL_SNAPSHOT_' "${docker_argv_capture}"; then
  fail "standalone S3 Docker argv contains snapshot environment names"
fi
for forbidden_value in distributed_only generic.private.example:9000 test-region-1 akernel-test; do
  if grep -Fq -- "${forbidden_value}" "${docker_argv_capture}"; then
    fail "standalone S3 Docker argv contains snapshot configuration values"
  fi
done
assert_no_credential_canary "$(<"${docker_argv_capture}") $(<"${output}")"
[[ "$(<"${docker_env_mode_capture}")" == "600" ]] \
  || fail "standalone S3 env-file is not mode 0600"
snapshot_env_file=$(<"${docker_env_file_capture}")
[[ -n "${snapshot_env_file}" && ! -e "${snapshot_env_file}" ]] \
  || fail "standalone S3 env-file survived successful Docker launch"
for expected_env in \
  'AKERNEL_SNAPSHOT_STORAGE_BACKEND=s3' \
  'AKERNEL_SNAPSHOT_STORAGE_MODE=distributed_only' \
  'AKERNEL_SNAPSHOT_S3_PROVIDER=generic' \
  'AKERNEL_SNAPSHOT_S3_ENDPOINT=generic.private.example:9000' \
  'AKERNEL_SNAPSHOT_S3_REGION=test-region-1' \
  'AKERNEL_SNAPSHOT_S3_BUCKET=akernel-test' \
  'AKERNEL_SNAPSHOT_S3_ACCESS_KEY=AK_CANARY_MUST_NOT_REACH_ARGV' \
  'AKERNEL_SNAPSHOT_S3_SECRET_KEY=SK_CANARY_MUST_NOT_REACH_ARGV' \
  'AKERNEL_SNAPSHOT_S3_SECURITY_TOKEN=TOKEN_CANARY_MUST_NOT_REACH_ARGV' \
  'AKERNEL_SNAPSHOT_S3_USE_HTTPS=true' \
  'AKERNEL_SNAPSHOT_S3_PATH_STYLE=true'; do
  grep -Fxq -- "${expected_env}" "${docker_env_capture}" \
    || fail "sudo-sanitized Docker environment omitted ${expected_env%%=*}"
done
[[ "$(grep -c '^AKERNEL_SNAPSHOT_' "${docker_env_capture}")" -eq 11 ]] \
  || fail "sudo-sanitized Docker environment contains an unexpected snapshot field set"

set_s3_environment generic false
unset AKERNEL_SNAPSHOT_STORAGE_MODE
clear_docker_capture
start_node_container >"${output}" 2>&1
grep -Fxq -- 'AKERNEL_SNAPSHOT_STORAGE_MODE=distributed_cache' "${docker_env_capture}" \
  || fail "standalone env-file did not default S3 mode to distributed_cache"
unset AKERNEL_SNAPSHOT_S3_PATH_STYLE
clear_docker_capture
start_node_container >"${output}" 2>&1
grep -Fxq -- 'AKERNEL_SNAPSHOT_S3_PATH_STYLE=false' "${docker_env_capture}" \
  || fail "standalone path-style default does not match Task 7"

# The env-file is deleted when Docker fails or the launch subshell is signaled.
set_s3_environment generic false
docker_capture_status=23
clear_docker_capture
if start_node_container >"${output}" 2>&1; then
  fail "standalone ignored a Docker launch failure"
fi
snapshot_env_file=$(<"${docker_env_file_capture}")
[[ -n "${snapshot_env_file}" && ! -e "${snapshot_env_file}" ]] \
  || fail "standalone S3 env-file survived Docker failure"
assert_no_credential_canary "$(<"${docker_argv_capture}") $(<"${output}")"
docker_capture_status=0

docker_capture_signal=TERM
clear_docker_capture
if start_node_container >"${output}" 2>&1; then
  fail "standalone ignored a terminated Docker launch"
fi
snapshot_env_file=$(<"${docker_env_file_capture}")
[[ -n "${snapshot_env_file}" && ! -e "${snapshot_env_file}" ]] \
  || fail "standalone S3 env-file survived launch termination"
assert_no_credential_canary "$(<"${docker_argv_capture}") $(<"${output}")"
docker_capture_signal=""

for line_break_value in $'endpoint\nINJECTED_VALUE' $'endpoint\rINJECTED_VALUE'; do
  set_s3_environment generic false
  export AKERNEL_SNAPSHOT_S3_ENDPOINT="${line_break_value}"
  clear_docker_capture
  if start_node_container >"${output}" 2>&1; then
    fail "standalone accepted CR/LF in an S3 env-file value"
  fi
  [[ ! -s "${docker_env_file_capture}" ]] \
    || fail "standalone created an env-file before CR/LF validation"
  if grep -Fq 'INJECTED_VALUE' "${output}"; then
    fail "standalone logged an invalid S3 env-file value"
  fi
done

export AKERNEL_SNAPSHOT_STORAGE_BACKEND=datasystem
clear_docker_capture
start_node_container >"${output}" 2>&1
[[ ! -s "${docker_env_file_capture}" ]] \
  || fail "DataSystem standalone launch created an S3 env-file"
if grep -Eq 'AKERNEL_SNAPSHOT_S3_|AKERNEL_SNAPSHOT_STORAGE_MODE|--env-file' \
  "${docker_argv_capture}"; then
  fail "DataSystem standalone launch inherited S3-specific Docker arguments"
fi
grep -Fxq -- 'AKERNEL_SNAPSHOT_STORAGE_BACKEND=datasystem' "${docker_argv_capture}" \
  || fail "DataSystem standalone backend argument changed"

# systemd must carry all host-facing inputs into the bootstrap shell.
pass_environment=$(sed -n 's/^PassEnvironment=//p' "${systemd_unit}" | tr '\n' ' ')
for variable in AKERNEL_SNAPSHOT_STORAGE_BACKEND AKERNEL_SNAPSHOT_STORAGE_MODE \
  AKERNEL_SNAPSHOT_S3_PROVIDER \
  AKERNEL_SNAPSHOT_S3_ENDPOINT AKERNEL_SNAPSHOT_S3_REGION AKERNEL_SNAPSHOT_S3_BUCKET \
  AKERNEL_SNAPSHOT_S3_ACCESS_KEY AKERNEL_SNAPSHOT_S3_SECRET_KEY \
  AKERNEL_SNAPSHOT_S3_SECURITY_TOKEN AKERNEL_SNAPSHOT_S3_USE_HTTPS \
  AKERNEL_SNAPSHOT_S3_PATH_STYLE; do
  [[ " ${pass_environment} " == *" ${variable} "* ]] \
    || fail "systemd omitted ${variable}"
done

echo "standalone snapshot wiring contract passed"
