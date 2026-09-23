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
write_file=""
print_export=0

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
    --write-file)
      write_file="$2"
      shift 2
      ;;
    --print-export)
      print_export=1
      shift
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

vendor="$(normalize_vendor "${vendor}")"
load_env_config "${env_name}"
tf_dir="$(vendor_dir "${vendor}")"
dir="$(state_dir "${env_name}")"
require_cmd terraform kubectl python3
terraform_state_file="${TERRAFORM_STATE_FILE:-${dir}/terraform.tfstate}"
setup_terraform_env "${env_name}" "${tf_dir}"

kubeconfig="$(terraform -chdir="${tf_dir}" output -state="${terraform_state_file}" -raw kubeconfig_path 2>/dev/null || true)"
[[ -n "${kubeconfig}" && -f "${kubeconfig}" ]] || die "kubeconfig not available from Terraform output"
core_namespace="$(terraform -chdir="${tf_dir}" output -state="${terraform_state_file}" -raw core_namespace 2>/dev/null || printf '%s' "${CORE_NAMESPACE:-akernel}")"
encoded="$(kubectl --kubeconfig "${kubeconfig}" -n "${core_namespace}" get secret akernel-adx-tls -o jsonpath='{.data.admin-key}')"
[[ -n "${encoded}" ]] || die "admin-key is missing from ${core_namespace}/akernel-adx-tls"
key="$(python3 -c 'import base64,sys; print(base64.b64decode(sys.argv[1]).decode())' "${encoded}")"

if [[ -n "${write_file}" ]]; then
  install -d -m 0700 "$(dirname "${write_file}")"
  printf '%s\n' "${key}" >"${write_file}"
  chmod 0600 "${write_file}"
fi

if [[ "${print_export}" -eq 1 ]]; then
  printf 'export AKERNEL_TOKEN=%q\n' "${key}"
else
  printf '%s\n' "${key}"
fi
