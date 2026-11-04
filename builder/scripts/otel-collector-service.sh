#!/usr/bin/env bash
# Systemd services import only the container's telemetry identity/endpoints.
set -euo pipefail
while IFS= read -r -d '' entry; do
  case "${entry}" in
    AKERNEL_ROLE=*|AKERNEL_ENV=*|AKERNEL_CLUSTER_NAME=*|POD_NAME=*|POD_NAMESPACE=*|NODE_NAME=*|PROMETHEUS_ENDPOINT=*|LOKI_ENDPOINT=*|TEMPO_ENDPOINT=*)
      name="${entry%%=*}"
      if ! declare -p "${name}" >/dev/null 2>&1; then
        export "${entry}"
      fi
      ;;
  esac
done </proc/1/environ
configs=(--config=/etc/akernel/otel_config.yaml)
if [[ "${AKERNEL_ROLE:-standalone}" == standalone ]]; then
  configs+=(--config=/etc/akernel/otel_standalone.yaml)
fi
exec /usr/local/bin/otelcol-contrib "${configs[@]}"
