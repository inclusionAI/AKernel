#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -eo pipefail

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
CHECKPOINT_DIR="/home/akernel/checkpoints"
mkdir -p "${CHECKPOINT_DIR}"

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
    local result=""
    local sep=""
    local entry host port resolved_host

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

if [[ "${YR_DATA_PLANE_NODE_PROXY_ENABLED:-false}" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]]; then
    YR_PYTHON_CLI="${YR_PYTHON_CLI:-/usr/local/bin/yr}"
    if [ ! -x "${YR_PYTHON_CLI}" ]; then
        echo "openYuanRong Python CLI not found: ${YR_PYTHON_CLI}" >&2
        exit 1
    fi
    ETCD_ADDR_LIST="${YR_ETCD_ADDR_LIST:-${ETCD_ADDRESS}:${ETCD_PORT}}"
    ETCD_ADDRESSES="$(toml_etcd_addresses "${ETCD_ADDR_LIST}")"
    MASTER_HOST="${YR_MASTER_ADDRESS:-akernel-master}"
    MASTER_IP="$(resolve_host "${MASTER_HOST}")"

    exec "${YR_PYTHON_CLI}" start \
        --function-proxy-merge-process-enable \
        --data-system-enable true \
        --port-policy FIX \
        --block true \
        --log-dir-prefix /home/yuanrong/sessions/node \
        -s "values.host_ip=\"${YR_NODE_IP}\"" \
        -s "values.local_ip=\"${YR_NODE_IP}\"" \
        -s "values.shared_memory_num=${YR_SHARED_MEMORY_NUM_MB:-512}" \
        -s 'ds_worker.health_check.timeout=300' \
        -s 'ds_worker.args.heartbeat_interval_ms=1000' \
        -s 'ds_worker.args.node_timeout_s=20' \
        -s 'ds_worker.args.node_dead_timeout_s=60' \
        -s 'ds_worker.args.client_dead_timeout_s=60' \
        -s "ds_worker.args.cluster_name=\"${YR_DATA_SYSTEM_CLUSTER_NAME:-}\"" \
        -s "values.function_master.ip=\"${MASTER_IP}\"" \
        -s 'values.etcd.enable_multi_master=true' \
        -s "values.etcd.address=${ETCD_ADDRESSES}" \
        -s 'values.fs.tls.enable=false' \
        -s 'values.node_proxy.enabled=true' \
        -s "values.node_proxy.bind=\"${YR_DATA_PLANE_NODE_PROXY_BIND:-0.0.0.0:9443}\"" \
        -s "values.node_proxy.advertise_address=\"${YR_NODE_PROXY_ADDRESS:-${YR_NODE_IP}:9443}\"" \
        -s "values.node_proxy.health_bind=\"${YR_DATA_PLANE_NODE_PROXY_HEALTH_BIND:-127.0.0.1:18443}\"" \
        -s 'values.node_proxy.allowed_target_cidrs=["172.17.0.0/16"]' \
        -s "values.node_proxy.allow_any_edge=${YR_DATA_PLANE_NODE_PROXY_ALLOW_ANY_EDGE:-true}" \
        -s "values.node_proxy.edge_security_mode=\"${YR_DATA_PLANE_EDGE_FRONTEND_NODE_SECURITY_MODE:-network}\"" \
        -s 'values.node_proxy.activity_interval_sec=2' \
        -s 'function_proxy.args.services_path="/home/yuanrong/deploy/process/services.yaml"' \
        -s 'function_proxy.args.metrics_collector_type="external"' \
        -s 'function_proxy.args.enable_traefik_registry=false' \
        -s 'function_proxy.args.enable_inherit_env=true' \
        -s 'function_proxy.args.force_low_reliability_instance=true'
fi

# Select the legacy etcd registry or the FunctionMaster HTTP provider.
if [ "${TRAEFIK_MODE:-etcd}" = "etcd" ]; then
    ENABLE_TRAEFIK_REGISTRY=${ENABLE_TRAEFIK_REGISTRY:-true}
    ENABLE_TRAEFIK_PROVIDER=false
else
    ENABLE_TRAEFIK_REGISTRY=false
    ENABLE_TRAEFIK_PROVIDER=true
fi

if [ "${AKS_LOCAL_MODE:-}" = "true" ]; then
    if [ -z "${LITEBUS_DATA_KEY:-}" ] && [ -r /home/akernel/iam-seed ]; then
        LITEBUS_DATA_KEY="$(tr -d '[:space:]' < /home/akernel/iam-seed)"
        export LITEBUS_DATA_KEY
    fi
    if [ -z "${LITEBUS_DATA_KEY:-}" ]; then
        echo "LITEBUS_DATA_KEY is required in standalone mode" >&2
        exit 1
    fi
    YR_PYTHON_CLI="${YR_PYTHON_CLI:-/usr/local/bin/yr}"
    if [ ! -x "${YR_PYTHON_CLI}" ]; then
        echo "openYuanRong Python CLI not found: ${YR_PYTHON_CLI}" >&2
        exit 1
    fi

    exec "${YR_PYTHON_CLI}" start \
        --master \
        --function-proxy-merge-process-enable \
        --port-policy FIX \
        --block true \
        --log-dir-prefix /home/yuanrong/sessions/master \
        -s "values.host_ip=\"${YR_NODE_IP}\"" \
        -s "values.local_ip=\"${YR_NODE_IP}\"" \
        -s "values.etcd.address=[{ip=\"${YR_NODE_IP}\",peer_port=${ETCD_PEER_PORT:-2378},port=${ETCD_PORT:-2379}}]" \
        -s 'values.fs.tls.enable=false' \
        -s 'values.node_proxy.enabled=true' \
        -s 'values.node_proxy.bind="0.0.0.0:9443"' \
        -s "values.node_proxy.advertise_address=\"${YR_NODE_IP}:9443\"" \
        -s 'values.node_proxy.health_bind="127.0.0.1:18443"' \
        -s 'values.node_proxy.allowed_target_cidrs=["10.88.0.0/16"]' \
        -s 'values.node_proxy.allow_any_edge=true' \
        -s 'values.node_proxy.edge_security_mode="network"' \
        -s 'values.node_proxy.activity_interval_sec=2' \
        -s 'mode.master.frontend=true' \
        -s 'mode.master.edge_frontend=true' \
        -s 'mode.master.meta_service=true' \
        -s 'mode.master.iam_server=true' \
        -s 'mode.master.function_scheduler=false' \
        -s 'values.frontend.port=8888' \
        -s 'values.frontend.ssl_enable=false' \
        -s 'values.frontend.client_auth_type="NoClientCert"' \
        -s 'values.frontend.enable_function_token_auth=true' \
        -s 'values.frontend.enable_func_token_auth=true' \
        -s 'values.frontend.frontend_lease_bypass=true' \
        -s 'values.frontend.sandbox_router_enable=false' \
        -s "values.frontend.meta_service_address=\"${YR_NODE_IP}:31182\"" \
        -s "values.frontend.iam_server_address=\"${YR_NODE_IP}:31112\"" \
        -s 'values.edge_frontend.tls_bind="0.0.0.0:8443"' \
        -s 'values.edge_frontend.plain_bind="0.0.0.0:8080"' \
        -s 'values.edge_frontend.health_bind="0.0.0.0:18080"' \
        -s "values.edge_frontend.frontend_address=\"${YR_NODE_IP}:8888\"" \
        -s 'values.edge_frontend.control_plane_routes=["exact:/","exact:/healthz","prefix:/terminal","prefix:/api/instances","prefix:/api/jobs","prefix:/api/sandbox","prefix:/functions","prefix:/api-docs","prefix:/admin/v1/functions","prefix:/serverless/v1/functions","prefix:/serverless/v1/stream","prefix:/serverless/v1/componentshealth","prefix:/serverless/v1/posix","prefix:/frontend/v1/instance","prefix:/datasystem/v1","prefix:/serverless/v2","prefix:/app/v1","prefix:/client/v1/lease","prefix:/invocations","prefix:/global-scheduler"]' \
        -s 'values.edge_frontend.tls_cert="/home/yuanrong/.cert/module.crt"' \
        -s 'values.edge_frontend.tls_key="/home/yuanrong/.cert/module.key"' \
        -s 'values.edge_frontend.validate_iam=true' \
        -s "values.edge_frontend.iam_address=\"${YR_NODE_IP}:31112\"" \
        -s 'values.edge_frontend.node_security_mode="network"' \
        -s 'values.edge_frontend.allowed_client_cidrs=[]' \
        -s 'values.edge_frontend.allow_any_client=true' \
        -s 'values.edge_frontend.log_level="info"' \
        -s 'values.meta_service.port=31182' \
        -s 'values.meta_service.ssl_enable=false' \
        -s 'values.iam_server.port=31112' \
        -s 'function_master.args.services_path="/home/yuanrong/deploy/process/services.yaml"' \
        -s 'function_proxy.args.services_path="/home/yuanrong/deploy/process/services.yaml"' \
        -s 'function_master.args.enable_traefik_provider=false' \
        -s 'function_master.args.system_timeout=60000' \
        -s 'function_proxy.args.system_timeout=60000' \
        -s 'function_proxy.args.metrics_collector_type="external"' \
        -s 'function_proxy.args.enable_inherit_env=true' \
        -s 'function_proxy.args.force_low_reliability_instance=true'
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
        --snapshot_storage_mode local_only \
        --checkpoint_dir "${CHECKPOINT_DIR}" \
        --block true
fi
