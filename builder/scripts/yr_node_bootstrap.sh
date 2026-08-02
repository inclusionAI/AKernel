#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
ulimit -n 32768
export YR_RUNTIME_BACKEND=sandboxd
# Set enable_traefik_registry based on TRAEFIK_MODE
if [ "${TRAEFIK_MODE:-etcd}" = "etcd" ]; then
    ENABLE_TRAEFIK_REGISTRY=${ENABLE_TRAEFIK_REGISTRY:-true}
else
    ENABLE_TRAEFIK_REGISTRY=false
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
        --traefik_enable_tls=${TRAEFIK_ENABLE_TLS:-false} \
        --traefik_etcd_prefix=traefik \
        --traefik_lease_ttl=300000 \
        --traefik_http_entrypoint=${TRAEFIK_HTTP_ENTRYPOINT:-websecure} \
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
