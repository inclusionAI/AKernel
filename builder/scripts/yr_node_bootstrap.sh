#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
ulimit -n 32768
export YR_RUNTIME_BACKEND=sandboxd
export YR_IMAGE_PROCESS_CONFIG="${YR_IMAGE_PROCESS_CONFIG:-/run/akernel/yr-image-process.json}"

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

# Read the same final TOML file as sandboxd, including mounted overrides.
resolve_node_proxy_target_cidrs() {
    python3 - <<'PY'
import ipaddress
import os
import sys
import tomllib

try:
    value = os.environ.get("NODE_PROXY_ALLOWED_TARGET_CIDRS", "").strip()
    if not value:
        path = os.environ.get("SANDBOXD_CONFIG_PATH", "/home/akernel/sandboxd/config.toml")
        with open(path, "rb") as config_file:
            value = tomllib.load(config_file)["plugin"]["network"]["ip_range"]
    networks = [str(ipaddress.ip_network(part.strip(), strict=False)) for part in value.split(",")]
    print(",".join(networks))
except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
    print(f"Cannot resolve Node Proxy target CIDRs: {error}", file=sys.stderr)
    sys.exit(1)
PY
}

YR_NODE_IP="$(resolve_node_ip)"
echo "Using ${YR_NODE_IP} as the YuanRong node address"
CHECKPOINT_DIR="/home/akernel/checkpoints"
mkdir -p "${CHECKPOINT_DIR}"

. /root/edge-config.sh
configure_edge || exit 1

NODE_PROXY_ARGS=()
if [ "${ENABLE_NODE_PROXY:-false}" = "true" ]; then
    NODE_PROXY_TARGET_CIDRS="$(resolve_node_proxy_target_cidrs)" || exit 1
    if [ -z "${NODE_PROXY_ALLOWED_EDGE_CIDRS:-}" ] && [ "${AKS_LOCAL_MODE:-false}" = true ]; then
        # Standalone Edge shares this network namespace and connects locally.
        NODE_PROXY_ALLOWED_EDGE_CIDRS="127.0.0.1/32,${YR_NODE_IP}/32"
    fi
    NODE_PROXY_ARGS=(
        --enable_node_proxy true
        --node_proxy_bind "0.0.0.0:${NODE_PROXY_PORT:-9443}"
        --node_proxy_advertise_address "${YR_NODE_IP}:${NODE_PROXY_PORT:-9443}"
        --node_proxy_health_bind "0.0.0.0:${NODE_PROXY_HEALTH_PORT:-18443}"
        --node_proxy_security_mode network
        --node_proxy_allowed_target_cidrs "${NODE_PROXY_TARGET_CIDRS}"
        --node_proxy_allowed_edge_cidrs "${NODE_PROXY_ALLOWED_EDGE_CIDRS:?required for Node Proxy}"
        --data_plane_log_dir "${DATA_PLANE_LOG_DIR:-${YR_LOG_PATH:-/home/yuanrong/logs}}"
        --data_plane_log_stdout true
    )
fi

run_yuanrong() {
    local child_pid=""
    local stop_requested=false
    local status=0
    stop_yuanrong() {
        if [ "$stop_requested" = false ]; then
            stop_requested=true
            if [ -n "$child_pid" ]; then
                kill -TERM "$child_pid" 2>/dev/null || true
            fi
        fi
    }
    trap stop_yuanrong TERM INT
    "$@" &
    child_pid=$!
    if [ "$stop_requested" = true ]; then
        kill -TERM "$child_pid" 2>/dev/null || true
    fi
    # A signal interrupts wait; keep the service MainPID alive until the CLI
    # has finished its ordered shutdown, including sandbox cleanup.
    while true; do
        if wait "$child_pid"; then
            status=0
            break
        else
            status=$?
        fi
        if ! kill -0 "$child_pid" 2>/dev/null; then
            break
        fi
    done
    trap - TERM INT
    return "$status"
}

if [  "x${AKS_LOCAL_MODE}" == "xtrue" ]; then
    if [ -z "${LITEBUS_DATA_KEY:-}" ] && [ -r /home/akernel/iam-seed ]; then
        LITEBUS_DATA_KEY="$(tr -d '[:space:]' < /home/akernel/iam-seed)"
        export LITEBUS_DATA_KEY
    fi
    if [ -z "${LITEBUS_DATA_KEY:-}" ]; then
        echo "LITEBUS_DATA_KEY is required in standalone mode" >&2
        exit 1
    fi
    run_yuanrong /usr/bin/yr start --master "${NODE_PROXY_ARGS[@]}" "${EDGE_ARGS[@]}" \
        --ip_address "${YR_NODE_IP}" \
        --port_policy FIX \
        --enable_function_scheduler=false \
        --enable_faas_frontend=true \
        --enable_meta_service=true \
        --enable_iam_server=true \
        --iam_token_expired_time_span 604800 \
        --ssl_base_path=/home/yuanrong/.cert/ \
        --frontend_ssl_enable="${FRONTEND_SSL_ENABLE:-true}" \
        --frontend_client_auth_type NoClientCert \
        --enable_function_token_auth true \
        --ds_node_timeout_s 30 \
        --ds_client_dead_timeout_s 60 \
        --ds_heartbeat_interval_ms 1000 \
        --ds_node_dead_timeout_s 120 \
        --system_timeout 60000 \
        --block true \
        --etcd_port ${ETCD_PORT:-2379} \
        --etcd_peer_port ${ETCD_PEER_PORT:-2378} \
        --enable_inherit_env false \
        --npu_collection_mode off \
        --enable_distributed_master false \
        --metrics_collector_type external \
        --enable_traefik_registry=false \
        --enable_traefik_provider=false \
        --enable_metrics ${ENABLE_METRICS} \
        --metrics_config_file "/home/yuanrong/metrics/metrics_config.json" \
        --enable_trace ${ENABLE_TRACE} \
        --trace_config "$(cat /home/yuanrong/trace/trace_config.json)" \
        --log_root "${YR_LOG_PATH}" \
        --function_proxy_merge_process_enable true \
        --fc_agent_mgr_retry_times 30 \
        --fc_agent_mgr_retry_cycle 60000 \
        --iam_ssl_enable "${IAM_SSL_ENABLE:-true}" \
        --ssl_root_file ca.crt \
        --ssl_cert_file module.crt \
        --ssl_key_file module.key \
        --iam_local_listen_port 31113 \
        --iam_local_ip 127.0.0.1 \
        --frontend_lease_bypass true \
        --force_low_reliability_instance true \
        --snapshot_storage_mode local_only \
        --checkpoint_dir "${CHECKPOINT_DIR}" \
        --enable_sandbox_router true \
        --enable_direct_routing false
else
    run_yuanrong /usr/bin/yr start "${NODE_PROXY_ARGS[@]}" "${EDGE_ARGS[@]}" \
        --ip_address "${YR_NODE_IP}" \
        --port_policy FIX \
        --ds_node_timeout_s 30 \
        --ds_client_dead_timeout_s 60 \
        --ds_heartbeat_interval_ms 1000 \
        --ds_node_dead_timeout_s 120 \
        --etcd_addr_list ${ETCD_ADDRESS} \
        --etcd_mode outter \
        --etcd_port ${ETCD_PORT} \
        --etcd_peer_port ${ETCD_PEER_PORT:-2378} \
        --system_timeout 60000 \
        --enable_inherit_env false \
        --npu_collection_mode off \
        --enable_distributed_master false \
        --metrics_collector_type external \
        --enable_metrics ${ENABLE_METRICS} \
        --metrics_config_file "/home/yuanrong/metrics/metrics_config.json" \
        --enable_trace ${ENABLE_TRACE} \
        --trace_config "$(cat /home/yuanrong/trace/trace_config.json)" \
        -n ${HOSTNAME} \
        --enable_traefik_registry=false \
        --log_root "${YR_LOG_PATH}" \
        --fc_agent_mgr_retry_times 30 \
        --fc_agent_mgr_retry_cycle 60000 \
        --log_expiration_time_threshold 10 \
        --log_expiration_cleanup_interval 10 \
        --log_expiration_max_file_count 50 \
        --function_proxy_merge_process_enable true \
        --enable_direct_routing false \
        --force_low_reliability_instance true \
        --snapshot_storage_mode local_only \
        --checkpoint_dir "${CHECKPOINT_DIR}" \
        --block true
fi
