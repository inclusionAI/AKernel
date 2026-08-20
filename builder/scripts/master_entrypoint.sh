#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

ulimit -n 32768

YR_CLI=/opt/openyuanrong/bin/yr
YR_CONFIG_TEMPLATE=/etc/yuanrong/config.toml.jinja
YR_CONFIG_PATH="${YR_RENDERED_CONFIG_PATH:-/run/yuanrong/config.toml}"
DEPLOY_PATH=/home/yuanrong/master
export DEPLOY_PATH
export YR_LOG_PATH="${DEPLOY_PATH}/log"

if [ -z "${LITEBUS_DATA_KEY:-}" ]; then
    echo "LITEBUS_DATA_KEY is required for akernel master/frontend" >&2
    exit 1
fi

if [ ! -x "${YR_CLI}" ]; then
    echo "yr binary not found or not executable: ${YR_CLI}" >&2
    exit 1
fi

if [ "${ENABLE_TRACE:-false}" = "true" ]; then
    trace_config_file="${TRACE_CONFIG_FILE:-/home/yuanrong/trace/trace_config.json}"
    if [ ! -r "${trace_config_file}" ]; then
        echo "trace config file is not readable: ${trace_config_file}" >&2
        exit 1
    fi
    YR_TRACE_CONFIG_CONTENT="$(cat "${trace_config_file}")"
    export YR_TRACE_CONFIG_CONTENT
else
    unset YR_TRACE_CONFIG_CONTENT
fi

mkdir -p "$(dirname "${YR_CONFIG_PATH}")"
"${YR_CLI}" config render \
    -t "${YR_CONFIG_TEMPLATE}" \
    -o "${YR_CONFIG_PATH}"

YR_CLI_ARGS=(
    "${YR_CLI}"
    --config "${YR_CONFIG_PATH}"
    start --master --block true
    --port-policy FIX
    --function-proxy-merge-process-enable
)

if [ "${YR_CLI_DRY_RUN:-false}" = "true" ]; then
    if [ -z "${YR_CLI_CAPTURE_FILE:-}" ]; then
        echo "YR_CLI_CAPTURE_FILE is required when YR_CLI_DRY_RUN=true" >&2
        exit 1
    fi
    mkdir -p "$(dirname "${YR_CLI_CAPTURE_FILE}")"
    printf '%s\0' "${YR_CLI_ARGS[@]}" > "${YR_CLI_CAPTURE_FILE}"
    exit 0
fi

mkdir -p "${DEPLOY_PATH}" "${YR_LOG_PATH}"

# If ConfigMap-mounted config exists, symlink it to override the baked-in default.
[ -f /etc/otel-collector/otel_config.yaml ] &&
    ln -sf /etc/otel-collector/otel_config.yaml /home/yuanrong/otel_config.yaml

# Monitor and restart the collector when observability is enabled.
otel_watchdog() {
    local otel_log="${DEPLOY_PATH}/otelcol.log"
    local max_restart_interval=60
    local restart_count=0
    while true; do
        otelcol-contrib --config=/home/yuanrong/otel_config.yaml >> "${otel_log}" 2>&1 &
        local otel_pid=$!
        echo "${otel_pid}" > "${DEPLOY_PATH}/otelcol.pid"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] otelcol started, PID: ${otel_pid} (restart count: ${restart_count})" >> "${otel_log}"

        local exit_code=0
        wait "${otel_pid}" || exit_code=$?
        restart_count=$((restart_count + 1))
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] otelcol exited with code ${exit_code}, restarting (restart count: ${restart_count})" >> "${otel_log}"

        local delay=$((2 ** restart_count))
        [ "${delay}" -gt "${max_restart_interval}" ] && delay="${max_restart_interval}"
        sleep "${delay}"
    done
}

export -f otel_watchdog
if { [ "${ENABLE_METRICS:-false}" = "true" ] || [ "${ENABLE_TRACE:-false}" = "true" ]; } &&
    command -v otelcol-contrib >/dev/null 2>&1; then
    nohup bash -c otel_watchdog &
    echo "otelcol watchdog started"
    echo "otel log: ${DEPLOY_PATH}/otelcol.log"
else
    echo "otelcol watchdog skipped"
fi

exec "${YR_CLI_ARGS[@]}"
