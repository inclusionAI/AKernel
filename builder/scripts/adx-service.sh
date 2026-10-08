#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

load_container_environment() {
  local source="${1:-/proc/1/environ}" entry name
  [[ -r "${source}" ]] || return 0
  # System services do not automatically inherit the container PID 1 environment.
  # Keep explicit service overrides and import only the ADX deployment contract.
  while IFS= read -r -d '' entry; do
    case "${entry}" in
      AKERNEL_ADX_CONFIG=*|AKERNEL_ADX_MANAGED_CREDENTIALS=*|AKERNEL_ADX_STATE_DIR=*|ADX_REDIS_URL=*|NODE_NAME=*|INSTANCE_IP=*|AWS_ACCESS_KEY_ID=*|AWS_SECRET_ACCESS_KEY=*|AWS_SESSION_TOKEN=*|AWS_EC2_METADATA_DISABLED=*)
        name="${entry%%=*}"
        if ! declare -p "${name}" >/dev/null 2>&1; then
          export "${entry}"
        fi
        ;;
    esac
  done <"${source}"
}

ensure_public_tls() (
  set -euo pipefail
  state_dir="$1"
  tls_dir="${state_dir}/tls"
  install -d -m 0700 "${tls_dir}"
  if [[ -s "${tls_dir}/ingress-public.pem" && -s "${tls_dir}/ingress-public.key" ]]; then
    exit 0
  fi
  tmp_dir="$(mktemp -d "${state_dir}/.certs.XXXXXX")"
  trap 'rm -rf -- "${tmp_dir}"' EXIT
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "${tmp_dir}/ingress-public.key" -out "${tmp_dir}/ingress-public.pem" \
    -subj "/CN=akernel" -days 3650 \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "extendedKeyUsage=serverAuth" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
  install -m 0600 "${tmp_dir}/ingress-public.key" "${tls_dir}/ingress-public.key"
  install -m 0644 "${tmp_dir}/ingress-public.pem" "${tls_dir}/ingress-public.pem"
)

main() {
  load_container_environment
  action="${1:?ADX service action is required}"
  config="${AKERNEL_ADX_CONFIG:-/etc/akernel/adx-standalone.yaml}"

  if [[ "${AKERNEL_ADX_MANAGED_CREDENTIALS:-local}" != external ]]; then
    state_dir="${AKERNEL_ADX_STATE_DIR:-/home/akernel/adx}"
    install -d -m 0700 "${state_dir}" "${state_dir}/run"
    if [[ "${action}" == run ]]; then
      ensure_public_tls "${state_dir}"
    fi
    ln -sfn "${state_dir}" /opt/adx/config
    ln -sfn "${state_dir}" /opt/adx/data
    ln -sfn "${state_dir}/run" /opt/adx/run
  fi

  case "${action}" in
    run|stop)
      exec /opt/adx/current/bin/adxctl --config "${config}" "${action}"
      ;;
    *)
      echo "unsupported ADX service action: ${action}" >&2
      exit 2
      ;;
  esac

}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
