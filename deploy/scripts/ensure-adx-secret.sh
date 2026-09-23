#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

namespace="${NAMESPACE:-akernel}"
secret_name="${SECRET_NAME:-akernel-adx-tls}"
kubectl_bin="${KUBECTL:-kubectl}"
kubeconfig=""

kubectl_cmd() {
  if [[ -n "${kubeconfig}" ]]; then
    "${kubectl_bin}" --kubeconfig "${kubeconfig}" "$@"
  else
    "${kubectl_bin}" "$@"
  fi
}

usage() {
  cat <<'EOF'
Usage: ensure-adx-secret.sh [options]

Create the public HTTPS certificate and API key Secret when it does not exist.

Options:
  -n, --namespace NAME     Kubernetes namespace (default: akernel)
      --name NAME          Secret name (default: akernel-adx-tls)
      --kubeconfig PATH    kubeconfig passed to kubectl
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n | --namespace)
      namespace="$2"
      shift 2
      ;;
    --name)
      secret_name="$2"
      shift 2
      ;;
    --kubeconfig)
      kubeconfig="$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

command -v "${kubectl_bin}" >/dev/null 2>&1 || {
  echo "kubectl not found: ${kubectl_bin}" >&2
  exit 1
}
command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required to generate the public HTTPS certificate" >&2
  exit 1
}

if ! kubectl_cmd get namespace "${namespace}" >/dev/null 2>&1; then
  kubectl_cmd create namespace "${namespace}"
fi

if kubectl_cmd -n "${namespace}" get secret "${secret_name}" >/dev/null 2>&1; then
  echo "Agent DX Secret already exists: ${namespace}/${secret_name}"
  exit 0
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "${tmp_dir}"' EXIT

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "${tmp_dir}/public.key" -out "${tmp_dir}/public.pem" \
  -subj "/CN=akernel" -days 3650 \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "extendedKeyUsage=serverAuth" \
  -addext "subjectAltName=DNS:adx.internal,DNS:localhost,DNS:akernel-adx-ingress-api,DNS:akernel-adx-ingress-api.${namespace}.svc.cluster.local,IP:127.0.0.1" >/dev/null 2>&1
openssl rand -hex 32 >"${tmp_dir}/admin-key"

kubectl_cmd -n "${namespace}" create secret generic "${secret_name}" \
  --from-file=public.pem="${tmp_dir}/public.pem" \
  --from-file=public.key="${tmp_dir}/public.key" \
  --from-file=admin-key="${tmp_dir}/admin-key"
echo "Created AKernel HTTPS and API key Secret: ${namespace}/${secret_name}"
