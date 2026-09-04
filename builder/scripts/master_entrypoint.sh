#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -e

ulimit -n 32768
BASE_DIR=$(
    cd "$(dirname "$0")"
    pwd
)
export DEPLOY_PATH="/home/yuanrong/master/"
mkdir -p "$DEPLOY_PATH"
export YR_LOG_PATH="$DEPLOY_PATH/log"

# If ConfigMap-mounted config exists, symlink it to override the baked-in default
[ -f /etc/otel-collector/otel_config.yaml ] && ln -sf /etc/otel-collector/otel_config.yaml /home/yuanrong/otel_config.yaml

# otel watchdog: monitor and restart otelcol-contrib if it crashes
otel_watchdog() {
    local otel_log="$DEPLOY_PATH/otelcol.log"
    local max_restart_interval=60
    local restart_count=0
    while true; do
        otelcol-contrib --config="/home/yuanrong/otel_config.yaml" >> "$otel_log" 2>&1 &
        local otel_pid=$!
        echo $otel_pid > $DEPLOY_PATH/otelcol.pid
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] otelcol started, PID: $otel_pid (restart count: $restart_count)" >> "$otel_log"

        wait $otel_pid
        local exit_code=$?
        restart_count=$((restart_count + 1))
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] otelcol exited with code $exit_code, restarting in 5s (restart count: $restart_count)" >> "$otel_log"

        # Exponential backoff with a cap
        local delay=$((2 ** restart_count))
        [ $delay -gt $max_restart_interval ] && delay=$max_restart_interval
        sleep $delay
    done
}

export -f otel_watchdog
if { [ "${ENABLE_METRICS:-false}" = "true" ] || [ "${ENABLE_TRACE:-false}" = "true" ]; } && command -v otelcol-contrib >/dev/null 2>&1; then
    nohup bash -c otel_watchdog &
    echo "otelcol watchdog started"
    echo "otel log: ${DEPLOY_PATH}/otelcol.log"
else
    echo "otelcol watchdog skipped"
fi

if [ -z "${LITEBUS_DATA_KEY:-}" ]; then
    echo "LITEBUS_DATA_KEY is required for akernel master/frontend" >&2
    exit 1
fi

YR_PYTHON_CLI="${YR_PYTHON_CLI:-/usr/local/bin/yr}"
if [ ! -x "${YR_PYTHON_CLI}" ]; then
    echo "openYuanRong Python CLI not found: ${YR_PYTHON_CLI}" >&2
    exit 1
fi

resolve_host() {
    local host="$1"
    local attempts="${YR_DNS_RESOLVE_ATTEMPTS:-90}"
    local delay="${YR_DNS_RESOLVE_INTERVAL_SECONDS:-2}"
    local attempt
    local resolved

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if resolved="$(python3 - "${host}" 2>/dev/null <<'PY'
import socket
import sys

print(socket.gethostbyname(sys.argv[1]))
PY
        )" && [ -n "${resolved}" ]; then
            printf '%s\n' "${resolved}"
            return 0
        fi
        sleep "${delay}"
    done
    echo "failed to resolve ${host} after ${attempts} attempts" >&2
    return 1
}

toml_etcd_addresses() {
    local addr_list="$1"
    local result="" sep="" entry host port resolved_host

    IFS=',' read -ra entries <<< "${addr_list}"
    for entry in "${entries[@]}"; do
        host="${entry%:*}"
        port="${entry##*:}"
        resolved_host="$(resolve_host "${host}")"
        result="${result}${sep}{ip=\"${resolved_host}\",peer_port=${port},port=${port}}"
        sep=","
    done
    printf '[%s]' "${result}"
}

toml_string_list() {
    python3 - "$1" <<'PY'
import json
import sys

print(json.dumps([item.strip() for item in sys.argv[1].split(",") if item.strip()]))
PY
}

is_true() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

host_ip="${INSTANCE_IP:-$(hostname -i | awk '{print $1}')}"
etcd_addr_list="${YR_ETCD_ADDR_LIST:-${ETCD_ADDRESS:-127.0.0.1}:${ETCD_PORT:-2379}}"
etcd_addresses="$(toml_etcd_addresses "${etcd_addr_list}")"
services_path="${YR_SERVICES_PATH:-/home/yuanrong/deploy/process/services.yaml}"
common_args=(
    --function-proxy-merge-process-enable
    --port-policy FIX
    --block true
    -s "values.host_ip=\"${host_ip}\""
    -s "values.local_ip=\"${host_ip}\""
    -s "values.cpu_num=${YR_CPU_NUM_MILLICORES:-1000}"
    -s "values.memory_num=${YR_MEMORY_NUM_MB:-1536}"
    -s "values.shared_memory_num=${YR_SHARED_MEMORY_NUM_MB:-512}"
    -s 'ds_worker.health_check.timeout=300'
    -s 'ds_worker.args.heartbeat_interval_ms=1000'
    -s 'ds_worker.args.node_timeout_s=20'
    -s 'ds_worker.args.node_dead_timeout_s=60'
    -s 'ds_worker.args.client_dead_timeout_s=60'
    -s "ds_worker.args.cluster_name=\"${YR_DATA_SYSTEM_CLUSTER_NAME:-}\""
    -s 'values.etcd.enable_multi_master=true'
    -s "values.etcd.address=${etcd_addresses}"
    -s 'values.fs.tls.enable=false'
    -s "function_proxy.args.services_path=\"${services_path}\""
    -s 'function_proxy.args.advertise_frontend_proxy_create=false'
    -s 'function_proxy.args.enable_inherit_env=true'
    -s 'function_proxy.args.force_low_reliability_instance=true'
)

if [ "${AKERNEL_ROLE:-master}" = "frontend" ]; then
    master_host="${YR_MASTER_ADDRESS:-${META_SERVICE_ADDRESS%:*}}"
    master_ip="$(resolve_host "${master_host}")"
    meta_service_address="${META_SERVICE_ADDRESS:-${master_ip}:31111}"

    edge_args=()
    if is_true "${ENABLE_EDGE_FRONTEND:-false}"; then
        tls_dir="${AKERNEL_COMPONENT_CERT_DIR:-/home/yuanrong/.cert}"
        tls_cert="${YR_DATA_PLANE_EDGE_FRONTEND_TLS_CERT:-${tls_dir}/module.crt}"
        tls_key="${YR_DATA_PLANE_EDGE_FRONTEND_TLS_KEY:-${tls_dir}/module.key}"
        for required in "${tls_cert}" "${tls_key}"; do
            if [ ! -s "${required}" ]; then
                echo "required Rust Edge certificate is missing: ${required}" >&2
                exit 1
            fi
        done

        allowed_client_cidrs="$(toml_string_list "${YR_DATA_PLANE_EDGE_FRONTEND_ALLOWED_CLIENT_CIDRS:-}")"
        default_control_plane_routes="exact:/,exact:/healthz,prefix:/terminal,prefix:/api/instances,prefix:/api/jobs,prefix:/api/sandbox,prefix:/functions,prefix:/api-docs,prefix:/admin/v1/functions,prefix:/serverless/v1/functions,prefix:/serverless/v1/stream,prefix:/serverless/v1/componentshealth,prefix:/serverless/v1/posix,prefix:/frontend/v1/instance,prefix:/datasystem/v1,prefix:/serverless/v2,prefix:/app/v1,prefix:/client/v1/lease,prefix:/invocations,prefix:/global-scheduler"
        control_plane_routes="$(toml_string_list "${YR_DATA_PLANE_EDGE_FRONTEND_CONTROL_PLANE_ROUTES:-${default_control_plane_routes}}")"
        edge_args=(
            -s 'mode.agent.edge_frontend=true'
            -s "values.edge_frontend.tls_bind=\"${YR_DATA_PLANE_EDGE_FRONTEND_TLS_BIND:-0.0.0.0:8443}\""
            -s "values.edge_frontend.plain_bind=\"${YR_DATA_PLANE_EDGE_FRONTEND_PLAIN_BIND:-0.0.0.0:8080}\""
            -s "values.edge_frontend.health_bind=\"${YR_DATA_PLANE_EDGE_FRONTEND_HEALTH_BIND:-0.0.0.0:18080}\""
            -s "values.edge_frontend.frontend_address=\"${YR_DATA_PLANE_EDGE_FRONTEND_CONTROL_PLANE_ADDRESS:-${host_ip}:8888}\""
            -s "values.edge_frontend.control_plane_routes=${control_plane_routes}"
            -s "values.edge_frontend.tls_cert=\"${tls_cert}\""
            -s "values.edge_frontend.tls_key=\"${tls_key}\""
            -s "values.edge_frontend.validate_iam=${YR_DATA_PLANE_EDGE_FRONTEND_VALIDATE_IAM:-true}"
            -s "values.edge_frontend.iam_address=\"${YR_DATA_PLANE_EDGE_FRONTEND_IAM_ADDRESS:-${host_ip}:31112}\""
            -s "values.edge_frontend.node_security_mode=\"${YR_DATA_PLANE_EDGE_FRONTEND_NODE_SECURITY_MODE:-network}\""
            -s "values.edge_frontend.allowed_client_cidrs=${allowed_client_cidrs}"
            -s "values.edge_frontend.allow_any_client=${YR_DATA_PLANE_EDGE_FRONTEND_ALLOW_ANY_CLIENT:-false}"
            -s "values.edge_frontend.log_level=\"${YR_DATA_PLANE_EDGE_FRONTEND_LOG_LEVEL:-info}\""
        )
    fi

    exec "${YR_PYTHON_CLI}" start \
        --log-dir-prefix /home/yuanrong/sessions/frontend \
        "${common_args[@]}" \
        -s 'mode.agent.frontend=true' \
        -s 'mode.agent.iam_server=true' \
        -s "values.function_master.ip=\"${master_ip}\"" \
        -s "values.meta_service.ip=\"${master_ip}\"" \
        -s 'values.meta_service.port=31111' \
        -s 'values.frontend.port=8888' \
        -s 'values.frontend.ssl_enable=false' \
        -s 'values.frontend.client_auth_type="NoClientCert"' \
        -s "values.frontend.enable_function_token_auth=${ENABLE_FUNCTION_TOKEN_AUTH:-true}" \
        -s "values.frontend.enable_func_token_auth=${ENABLE_FUNCTION_TOKEN_AUTH:-true}" \
        -s 'values.frontend.frontend_lease_bypass=true' \
        -s "values.frontend.sandbox_router_enable=${ENABLE_SANDBOX_ROUTER:-false}" \
        -s "values.frontend.meta_service_address=\"${meta_service_address}\"" \
        -s "values.frontend.iam_server_address=\"${host_ip}:31112\"" \
        -s 'values.iam_server.port=31112' \
        "${edge_args[@]}"
fi

exec "${YR_PYTHON_CLI}" start \
    --master \
    --log-dir-prefix /home/yuanrong/sessions/master \
    "${common_args[@]}" \
    -s 'mode.master.etcd=false' \
    -s 'mode.master.ds_master=false' \
    -s "mode.master.frontend=${ENABLE_FAAS_FRONTEND:-false}" \
    -s "mode.master.function_scheduler=${ENABLE_FUNCTION_SCHEDULER:-false}" \
    -s "mode.master.meta_service=${ENABLE_META_SERVICE:-true}" \
    -s "mode.master.iam_server=${ENABLE_IAM_SERVER:-false}" \
    -s 'values.function_master.global_scheduler_port=22770' \
    -s 'values.meta_service.port=31111' \
    -s 'values.iam_server.port=31112' \
    -s 'function_master.args.enable_traefik_provider=false' \
    -s 'function_master.args.system_timeout=300000' \
    -s "function_master.args.services_path=\"${services_path}\""
