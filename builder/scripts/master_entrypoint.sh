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
export YR_IMAGE_PROCESS_CONFIG="${YR_IMAGE_PROCESS_CONFIG:-/run/akernel/yr-image-process.json}"

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

# Set enable_traefik_provider based on TRAEFIK_MODE
if [ "${TRAEFIK_MODE:-etcd}" = "http" ]; then
    ENABLE_TRAEFIK_PROVIDER=true
else
    ENABLE_TRAEFIK_PROVIDER=false
fi

if [ -z "${LITEBUS_DATA_KEY:-}" ]; then
    echo "LITEBUS_DATA_KEY is required for akernel master/frontend" >&2
    exit 1
fi

YR_BIN="${YR_BIN:-/usr/bin/yr}"
if [ ! -x "${YR_BIN}" ]; then
    echo "yr binary not found or not executable: ${YR_BIN}" >&2
    exit 1
fi

exec "${YR_BIN}" start --master --block true \
    -e -c 0 -m 8000 -s 4096 -n $HOSTNAME \
    -d $DEPLOY_PATH \
    --fs_health_check_retry_interval 1 \
    --schedule_relaxed 20 \
    --enable_faas_frontend ${ENABLE_FAAS_FRONTEND:-true} \
    --enable_function_scheduler ${ENABLE_FUNCTION_SCHEDULER:-false} \
    --enable_meta_service ${ENABLE_META_SERVICE:-true} \
    --enable_iam_server ${ENABLE_IAM_SERVER:-true} \
    --iam_token_expired_time_span 604800 \
    --ssl_base_path=/home/yuanrong/.cert/ \
    --frontend_ssl_enable=true \
    --frontend_client_auth_type NoClientCert \
    --enable_function_token_auth ${ENABLE_FUNCTION_TOKEN_AUTH:-true} \
    --enable_inherit_env false \
    --npu_collection_mode off \
    --port_policy FIX \
    --system_timeout 300000 \
    --enable_distributed_master false \
    --etcd_mode outter \
    --etcd_addr_list $ETCD_ADDRESS \
    --etcd_port ${ETCD_PORT} \
    --etcd_peer_port 2378 \
    --enable_metrics ${ENABLE_METRICS} \
    --metrics_config_file "/home/yuanrong/metrics/metrics_config.json" \
    --enable_trace ${ENABLE_TRACE} \
    --trace_config "$(cat /home/yuanrong/trace/trace_config.json)" \
    --ds_rpc_thread_num 128 \
    --function_proxy_merge_process_enable true \
    --force_low_reliability_instance true \
    --enable_traefik_provider=${ENABLE_TRAEFIK_PROVIDER} \
    --traefik_http_entry_point=${TRAEFIK_HTTP_ENTRYPOINT:-websecure} \
    --traefik_enable_tls=${TRAEFIK_ENABLE_TLS:-false} \
    --traefik_forward_timeout_ms=3000 \
    --frontend_lease_bypass true \
    --iam_ssl_enable true \
    --ssl_root_file ca.crt \
    --ssl_cert_file module.crt \
    --ssl_key_file module.key \
    --iam_local_listen_port 31113 \
    --iam_local_ip 127.0.0.1 \
    --enable_direct_routing false \
    --enable_sandbox_router true \
    ${META_SERVICE_ADDRESS:+--meta_service_address $META_SERVICE_ADDRESS}
