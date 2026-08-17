#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
ulimit -n 32768
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

YR_NODE_IP="$(resolve_node_ip)"
echo "Using ${YR_NODE_IP} as the YuanRong node address"

# Select the legacy etcd registry or the FunctionMaster HTTP provider.
if [ "${TRAEFIK_MODE:-etcd}" = "etcd" ]; then
    ENABLE_TRAEFIK_REGISTRY=${ENABLE_TRAEFIK_REGISTRY:-true}
    ENABLE_TRAEFIK_PROVIDER=false
else
    ENABLE_TRAEFIK_REGISTRY=false
    ENABLE_TRAEFIK_PROVIDER=true
fi

if [  "x${AKS_LOCAL_MODE}" == "xtrue" ]; then
    if [ -z "${LITEBUS_DATA_KEY:-}" ] && [ -r /home/akernel/iam-seed ]; then
        LITEBUS_DATA_KEY="$(tr -d '[:space:]' < /home/akernel/iam-seed)"
        export LITEBUS_DATA_KEY
    fi
    if [ -z "${LITEBUS_DATA_KEY:-}" ]; then
        echo "LITEBUS_DATA_KEY is required in standalone mode" >&2
        exit 1
    fi
    /usr/bin/yr start --master \
        --ip_address "${YR_NODE_IP}" \
        --port_policy FIX \
        --enable_function_scheduler=false \
        --enable_faas_frontend=true \
        --enable_meta_service=true \
        --enable_iam_server=true \
        --iam_token_expired_time_span 604800 \
        --ssl_base_path=/home/yuanrong/.cert/ \
        --frontend_ssl_enable=true \
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
        --enable_traefik_registry=${ENABLE_TRAEFIK_REGISTRY} \
        --enable_traefik_provider=${ENABLE_TRAEFIK_PROVIDER} \
        --traefik_enable_tls=${TRAEFIK_ENABLE_TLS:-false} \
        --traefik_etcd_prefix=traefik \
        --traefik_lease_ttl=300000 \
        --traefik_http_entrypoint=${TRAEFIK_HTTP_ENTRYPOINT:-websecure} \
        --traefik_http_entry_point=${TRAEFIK_HTTP_ENTRYPOINT:-websecure} \
        --enable_metrics ${ENABLE_METRICS} \
        --metrics_config_file "/home/yuanrong/metrics/metrics_config.json" \
        --enable_trace ${ENABLE_TRACE} \
        --trace_config "$(cat /home/yuanrong/trace/trace_config.json)" \
        --log_root "${YR_LOG_PATH}" \
        --function_proxy_merge_process_enable true \
        --fc_agent_mgr_retry_times 30 \
        --fc_agent_mgr_retry_cycle 60000 \
        --iam_ssl_enable true \
        --ssl_root_file ca.crt \
        --ssl_cert_file module.crt \
        --ssl_key_file module.key \
        --iam_local_listen_port 31113 \
        --iam_local_ip 127.0.0.1 \
        --frontend_lease_bypass true \
        --force_low_reliability_instance true \
        --enable_sandbox_router true \
        --enable_direct_routing false
else
    /usr/bin/yr start \
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
        --enable_traefik_registry=${ENABLE_TRAEFIK_REGISTRY} \
        --traefik_enable_tls=${TRAEFIK_ENABLE_TLS:-false} \
        --traefik_etcd_prefix=traefik \
        --traefik_lease_ttl=300000 \
        --traefik_http_entrypoint=${TRAEFIK_HTTP_ENTRYPOINT:-websecure} \
        --log_root "${YR_LOG_PATH}" \
        --fc_agent_mgr_retry_times 30 \
        --fc_agent_mgr_retry_cycle 60000 \
        --log_expiration_time_threshold 10 \
        --log_expiration_cleanup_interval 10 \
        --log_expiration_max_file_count 50 \
        --function_proxy_merge_process_enable true \
        --enable_direct_routing false \
        --force_low_reliability_instance true \
        --block true
fi
