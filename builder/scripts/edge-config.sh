#!/bin/bash
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

configure_edge() {
    EDGE_ARGS=()
    if [ "${ENABLE_EDGE_FRONTEND:-false}" != true ]; then
        return
    fi
    export EDGE_ADVERTISE_IP="${INSTANCE_IP:-${YR_NODE_IP:-127.0.0.1}}"
    export YR_DATA_PLANE_EDGE_FRONTEND_PROXY_ROUTES_FILE="${YR_DATA_PLANE_EDGE_FRONTEND_PROXY_ROUTES_FILE:-/run/akernel/edge-proxy-routes.json}"
    mkdir -p /run/akernel/edge-http || return 1
    python3 - <<'PY' || return 1
import json
import os
from pathlib import Path

Path('/run/akernel/edge-http/internal-stats').write_text(json.dumps({
    'pod_ip': os.environ['EDGE_ADVERTISE_IP'],
    'http_port': int(os.environ.get('EDGE_PLAIN_PORT', '80')),
    'https_port': int(os.environ.get('EDGE_TLS_PORT', '443')),
}))
routes = [{'name': 'internal-stats', 'path_prefix': '/internal-stats',
           'upstream': 'http://127.0.0.1:18081', 'strip_prefix': False}]
if os.environ.get('EDGE_GRAFANA_URL'):
    routes.append({'name': 'grafana', 'path_prefix': '/grafana',
                   'upstream': os.environ['EDGE_GRAFANA_URL'], 'strip_prefix': False})
route_file = Path(os.environ['YR_DATA_PLANE_EDGE_FRONTEND_PROXY_ROUTES_FILE'])
if route_file == Path('/run/akernel/edge-proxy-routes.json'):
    route_file.write_text(json.dumps(routes))
PY
    python3 -m http.server 18081 --bind 127.0.0.1 --directory /run/akernel/edge-http &
    EDGE_ARGS=(
        --enable_edge_frontend true
        --edge_frontend_tls_bind "0.0.0.0:${EDGE_TLS_PORT:-443}"
        --edge_frontend_plain_bind "0.0.0.0:${EDGE_PLAIN_PORT:-80}"
        --edge_frontend_health_bind "0.0.0.0:${EDGE_HEALTH_PORT:-18080}"
        --edge_frontend_tls_cert "${EDGE_TLS_CERT:-/home/yuanrong/.cert/module.crt}"
        --edge_frontend_tls_key "${EDGE_TLS_KEY:-/home/yuanrong/.cert/module.key}"
        --edge_frontend_control_plane_address "${EDGE_ADVERTISE_IP}:8888"
        --edge_frontend_iam_address 127.0.0.1:31113
        --edge_frontend_validate_iam true
        --edge_frontend_allowed_client_cidrs "${EDGE_ALLOWED_CLIENT_CIDRS:-0.0.0.0/0}"
        --data_plane_log_dir "${DATA_PLANE_LOG_DIR:-${YR_LOG_PATH:-/home/yuanrong/logs}}"
        --data_plane_log_stdout true
        --edge_frontend_access_log_enabled true
    )
    FRONTEND_SSL_ENABLE=false
    IAM_SSL_ENABLE=false
}
