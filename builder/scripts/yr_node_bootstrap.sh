#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

ulimit -n 32768

YR_CLI=/opt/openyuanrong/bin/yr
YR_CONFIG_TEMPLATE=/etc/yuanrong/config.toml.jinja
YR_CONFIG_PATH="${YR_RENDERED_CONFIG_PATH:-/run/yuanrong/config.toml}"
export YR_RUNTIME_BACKEND=sandboxd

resolve_node_ip() {
    local default_device
    local node_ip

    if [ -n "${AKERNEL_NODE_IP:-}" ]; then
        printf '%s\n' "${AKERNEL_NODE_IP}"
        return
    fi
    if [ -n "${INSTANCE_IP:-}" ]; then
        printf '%s\n' "${INSTANCE_IP}"
        return
    fi
    if ! command -v ip >/dev/null 2>&1; then
        echo "ip is required to discover the AKernel node address" >&2
        return 1
    fi

    default_device="$(ip -4 route show default | awk 'NR == 1 { print $5 }')"
    if [ -z "${default_device}" ]; then
        echo "the AKernel network namespace has no IPv4 default route" >&2
        return 1
    fi
    node_ip="$(
        ip -4 -o address show dev "${default_device}" scope global |
            awk 'NR == 1 { split($4, address, "/"); print address[1] }'
    )"
    if [ -z "${node_ip}" ]; then
        echo "default-route device ${default_device} has no global IPv4 address" >&2
        return 1
    fi
    printf '%s\n' "${node_ip}"
}

resolve_sandbox_ip() {
    local address

    address="$(
        ip -4 -o address show dev sandbox0 scope global 2>/dev/null |
            awk 'NR == 1 { split($4, value, "/"); print value[1] }'
    )"
    if [ -z "${address}" ]; then
        echo "sandbox0 has no global IPv4 address after sandboxd startup" >&2
        return 1
    fi
    printf '%s\n' "${address}"
}

YR_NODE_IP="$(resolve_node_ip)"
export YR_NODE_IP
echo "Using ${YR_NODE_IP} as the YuanRong node address"

YR_LOCAL_IP="$(resolve_sandbox_ip)"
export YR_LOCAL_IP
echo "Using ${YR_LOCAL_IP} as the YuanRong sandbox-local service address"

role="${AKERNEL_ROLE:-}"
if [ -z "${role}" ]; then
    if [ "${AKS_LOCAL_MODE:-false}" = "true" ]; then
        role=standalone
    else
        role=node
    fi
    export AKERNEL_ROLE="${role}"
fi

case "${role}" in
    node)
        ;;
    standalone)
        if [ -z "${LITEBUS_DATA_KEY:-}" ] && [ -r /home/akernel/iam-seed ]; then
            LITEBUS_DATA_KEY="$(tr -d '[:space:]' < /home/akernel/iam-seed)"
            export LITEBUS_DATA_KEY
        fi
        if [ -z "${LITEBUS_DATA_KEY:-}" ]; then
            echo "LITEBUS_DATA_KEY is required in standalone mode" >&2
            exit 1
        fi
        ;;
    *)
        echo "AKERNEL_ROLE must be node or standalone" >&2
        exit 1
        ;;
esac

export DEPLOY_PATH="${DEPLOY_PATH:-/home/yuanrong}"
export YR_LOG_PATH="${YR_LOG_PATH:-${DEPLOY_PATH}/logs}"

if [ ! -f "${YR_CONFIG_TEMPLATE}" ]; then
    echo "YuanRong CLI config template not found: ${YR_CONFIG_TEMPLATE}" >&2
    exit 1
fi
if [ ! -x "${YR_CLI}" ]; then
    echo "YuanRong CLI not executable: ${YR_CLI}" >&2
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
    start
)
if [ "${role}" = "standalone" ]; then
    YR_CLI_ARGS+=(--master)
fi
YR_CLI_ARGS+=(
    --block true
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

mkdir -p "${YR_LOG_PATH:-/home/yuanrong/logs}"
exec "${YR_CLI_ARGS[@]}"
