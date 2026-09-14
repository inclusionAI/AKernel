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
load_env_config "${env_name}"
tf_dir="$(vendor_dir "${vendor}")"
dir="$(state_dir "${env_name}")"
require_cmd terraform kubectl python3
terraform_state_file="${TERRAFORM_STATE_FILE:-${dir}/terraform.tfstate}"
setup_terraform_env "${env_name}" "${tf_dir}"

kubeconfig="$(terraform -chdir="${tf_dir}" output -state="${terraform_state_file}" -raw kubeconfig_path 2>/dev/null || true)"
[[ -n "${kubeconfig}" && -f "${kubeconfig}" ]] || die "kubeconfig not available from Terraform output"
core_ns="$(terraform -chdir="${tf_dir}" output -state="${terraform_state_file}" -raw core_namespace 2>/dev/null || printf '%s' "${CORE_NAMESPACE:-akernel}")"
monitor_ns="$(terraform -chdir="${tf_dir}" output -state="${terraform_state_file}" -raw monitor_namespace 2>/dev/null || printf '%s' "${MONITOR_NAMESPACE:-akernel-monitor}")"

get_lb_host() {
  local namespace="$1"
  local service="$2"
  local host
  host="$(kubectl --kubeconfig "${kubeconfig}" -n "${namespace}" get svc "${service}" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  if [[ -z "${host}" ]]; then
    host="$(kubectl --kubeconfig "${kubeconfig}" -n "${namespace}" get svc "${service}" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  fi
  printf '%s' "${host}"
}

gateway_service="${AKERNEL_GATEWAY_SERVICE:-}"
if [[ -z "${gateway_service}" ]]; then
  gateway_service="$(kubectl --kubeconfig "${kubeconfig}" -n "${core_ns}" get svc \
    -l app.kubernetes.io/component=edge -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi
[[ -n "${gateway_service}" ]] || die "Edge gateway Service not found; set AKERNEL_GATEWAY_SERVICE"
gateway_host="$(get_lb_host "${core_ns}" "${gateway_service}")"
[[ -n "${gateway_host}" ]] || die "${gateway_service} LoadBalancer address is not ready"

https_port="$(kubectl --kubeconfig "${kubeconfig}" -n "${core_ns}" get svc "${gateway_service}" -o 'jsonpath={.spec.ports[?(@.name=="websecure")].port}')"
http_port="$(kubectl --kubeconfig "${kubeconfig}" -n "${core_ns}" get svc "${gateway_service}" -o 'jsonpath={.spec.ports[?(@.name=="web")].port}')"
[[ -n "${https_port}" && -n "${http_port}" ]] || die "Edge Service must expose websecure and web ports"

token="$("${AKERNEL_REPO_ROOT}/deploy/scripts/generate-token.py" --env "${env_name}" --write-file "${dir}/token")"

sdk_env="${dir}/sdk.env"
{
  if [[ "${https_port}" == 443 && "${http_port}" == 80 ]]; then
    printf 'export AKERNEL_SERVER_ADDRESS=%q\n' "${gateway_host}"
  else
    printf 'export AKERNEL_SERVER_ADDRESS=%q\n' "https://${gateway_host}:${https_port}"
    printf 'export AKERNEL_GATEWAY_ADDRESS=%q\n' "http://${gateway_host}:${http_port}"
  fi
  printf 'export AKERNEL_TOKEN=%q\n' "${token}"
} > "${sdk_env}"
chmod 600 "${sdk_env}"

echo "SDK environment:"
cat "${sdk_env}"

grafana_host="$(get_lb_host "${monitor_ns}" grafana)"
if [[ -n "${grafana_host}" ]]; then
  grafana_port="$(kubectl --kubeconfig "${kubeconfig}" -n "${monitor_ns}" get svc grafana -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || true)"
  grafana_port="${grafana_port:-3000}"
  echo
  echo "Grafana:"
  echo "  http://${grafana_host}:${grafana_port}"
fi
