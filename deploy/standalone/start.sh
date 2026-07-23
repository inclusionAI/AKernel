#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
# This script starts the akernel in standalone mode

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${SCRIPT_DIR}/config"
DATA_DIR="${SCRIPT_DIR}/data"
FRONTEND_PORT="8888"
ETCD_PORT="${ETCD_PORT:-2379}"
ETCD_PEER_PORT="${ETCD_PEER_PORT:-2378}"
NODE_CONTAINER_NAME="akernel-node"
TRAEFIK_CONTAINER_NAME="akernel-traefik"
IMAGE="${IMAGE:-akerneldev/all-in-one:latest}"
TRAEFIK_IMAGE="${TRAEFIK_IMAGE:-traefik:v3.6.8}"
IAM_SEED_FILE="${DATA_DIR}/iam-seed"
TOKEN_FILE="${DATA_DIR}/token"
LITEBUS_DATA_KEY=""

# Container runtime command (docker or pouch)
DOCKER_CMD=""
DOCKER_PREFIX=()
PROXY_RUN_ARGS=()

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check prerequisites
check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check Docker first, if not found, check Pouch
    if command -v docker &> /dev/null; then
        DOCKER_CMD="docker"
        log_info "Found Docker as container engine"
    elif command -v pouch &> /dev/null; then
        DOCKER_CMD="pouch"
        log_info "Found Pouch as container engine"
    else
        log_error "Neither Docker nor Pouch is installed or not in PATH"
        exit 1
    fi

    # Check if container runtime daemon is running. Prefer direct access so
    # users in the docker/pouch group do not need sudo; fall back to
    # passwordless sudo for hosts that require it.
    if ${DOCKER_CMD} info &> /dev/null; then
        DOCKER_PREFIX=()
    elif sudo -n ${DOCKER_CMD} info &> /dev/null; then
        DOCKER_PREFIX=(sudo)
    else
        log_error "${DOCKER_CMD} daemon is not running"
        exit 1
    fi

    log_info "${DOCKER_CMD} is available"

    if [[ ! -c /dev/kvm ]]; then
        log_warn "/dev/kvm is unavailable; standalone will support runsc but will not advertise the Kata runtime"
    elif [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
        log_warn "/dev/kvm is not accessible to the current user; verify that the privileged node container can access it before using Kata"
    fi

    if ! command -v curl &> /dev/null; then
        log_error "curl is required to verify the standalone endpoint"
        exit 1
    fi

    if ! command -v python3 &> /dev/null; then
        log_error "python3 is required to generate standalone credentials"
        exit 1
    fi

    # Create data directory
    mkdir -p "${DATA_DIR}"
    log_info "Data directory: ${DATA_DIR}"

    # Check if config files exist
    local config_files=(
        "config.json"
        "oss.json"
        "registry.json"
        "sandboxd_config.toml"
        "oss_auths.json"
        "registry_auths.json"
    )

    local missing=0
    for file in "${config_files[@]}"; do
        if [[ ! -f "${CONFIG_DIR}/${file}" ]]; then
            log_error "Missing config file: ${CONFIG_DIR}/${file}"
            missing=1
        fi
    done

    if [[ $missing -eq 1 ]]; then
        exit 1
    fi

    # config/ holds the input configs; image_manager/ is runtime state that
    # sandboxd wipes on pod change. Keep them as distinct subtrees.
    mkdir -p "${DATA_DIR}/sandboxd/config" "${DATA_DIR}/sandboxd/image_manager"

    log_info "All config files found"
}

configure_auth() {
    if [[ ! -s "${IAM_SEED_FILE}" ]]; then
        python3 -c 'import secrets; print(secrets.token_hex(32).upper())' \
            > "${IAM_SEED_FILE}"
        chmod 0600 "${IAM_SEED_FILE}"
        log_info "Generated a deployment-specific IAM seed"
    fi

    LITEBUS_DATA_KEY="$(tr -d '[:space:]' < "${IAM_SEED_FILE}")"
    if [[ ! "${LITEBUS_DATA_KEY}" =~ ^[0-9A-Fa-f]+$ ]] || \
       (( ${#LITEBUS_DATA_KEY} % 2 != 0 )); then
        log_error "${IAM_SEED_FILE} must contain an even-length hexadecimal seed"
        exit 1
    fi

    "${SCRIPT_DIR}/../scripts/generate-token.py" \
        --seed-file "${IAM_SEED_FILE}" \
        --ttl "${STANDALONE_TOKEN_TTL:-24h}" \
        --write-file "${TOKEN_FILE}" > /dev/null
}

# Stop and remove existing container
cleanup_existing() {
    local container
    for container in "${NODE_CONTAINER_NAME}" "${TRAEFIK_CONTAINER_NAME}"; do
        if "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} container inspect "${container}" &> /dev/null; then
            log_warn "Existing container '${container}' found; run stop.sh first"
            exit 1
        fi
    done
}

# Pull an image when it is not already available locally.
ensure_image() {
    local image="$1"
    if "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} image inspect "${image}" &> /dev/null; then
        log_info "Using local image: ${image}"
        return 0
    fi

    log_info "Pulling image: ${image}"
    if "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} pull "${image}"; then
        log_info "Image pulled successfully"
    else
        log_error "Failed to pull image: ${image}"
        exit 1
    fi
}

configure_container_proxy() {
    local proxy="${AKERNEL_CONTAINER_PROXY:-}"
    if [[ -z "${proxy}" ]]; then
        return 0
    fi

    local no_proxy="${AKERNEL_CONTAINER_NO_PROXY:-localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
    log_info "Using container proxy: ${proxy}"

    cat > "${DATA_DIR}/proxy.env" <<EOF
HTTP_PROXY=${proxy}
HTTPS_PROXY=${proxy}
ALL_PROXY=${proxy}
http_proxy=${proxy}
https_proxy=${proxy}
all_proxy=${proxy}
NO_PROXY=${no_proxy}
no_proxy=${no_proxy}
EOF

    PROXY_RUN_ARGS=(
        --add-host=host.docker.internal:host-gateway
        -e HTTP_PROXY="${proxy}"
        -e HTTPS_PROXY="${proxy}"
        -e ALL_PROXY="${proxy}"
        -e http_proxy="${proxy}"
        -e https_proxy="${proxy}"
        -e all_proxy="${proxy}"
        -e NO_PROXY="${no_proxy}"
        -e no_proxy="${no_proxy}"
        -v "${DATA_DIR}/proxy.env:/etc/akernel/proxy.env:ro"
    )
}

# Start the AKernel all-in-one container. Traefik runs separately so traffic
# from the gateway enters this network namespace through PREROUTING.
start_node_container() {
    log_info "Starting container: ${NODE_CONTAINER_NAME}"

    "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} run -d \
        --name "${NODE_CONTAINER_NAME}" \
        --privileged \
        --net bridge \
        --restart always \
        -e AKS_LOCAL_MODE="true" \
        -e TRAEFIK_MODE="etcd" \
        -e TRAEFIK_HTTP_ENTRYPOINT="web" \
        -e TRAEFIK_ENABLE_TLS="false" \
        -e ETCD_PORT="${ETCD_PORT}" \
        -e ETCD_PEER_PORT="${ETCD_PEER_PORT}" \
        -e NODE_NAME="$(hostname)" \
        -e POD_NAME=akernel-node-local \
        -e POD_NAMESPACE=default \
        -e TZ=Asia/Shanghai \
        -e ENABLE_TRACE="${ENABLE_TRACE:-false}" \
        -e ENABLE_METRICS="${ENABLE_METRICS:-false}" \
        "${PROXY_RUN_ARGS[@]}" \
        --entrypoint=/usr/local/bin/akernel-entrypoint \
        -v "${DATA_DIR}:/home/akernel" \
        -v "${CONFIG_DIR}/oss_auths.json:/home/akernel/sandboxd/config/oss_auths.json:ro" \
        -v "${CONFIG_DIR}/oss.json:/home/akernel/sandboxd/config/oss.json:ro" \
        -v "${CONFIG_DIR}/registry_auths.json:/home/akernel/sandboxd/config/registry_auths.json:ro" \
        -v "${CONFIG_DIR}/registry.json:/home/akernel/sandboxd/config/registry.json:ro" \
        -v "${CONFIG_DIR}/config.json:/home/akernel/images/config.json:ro" \
        -v "${CONFIG_DIR}/sandboxd_config.toml:/home/akernel/sandboxd/config.toml:ro" \
        "${IMAGE}"
}

# Wait for container to be ready
wait_for_ready() {
    log_info "Waiting for container to be ready..."

    local retries=30
    local delay=2

    for i in $(seq 1 $retries); do
        if "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} exec "${NODE_CONTAINER_NAME}" systemctl is-system-running &> /dev/null; then
            log_info "AKernel container is ready"
            return 0
        fi

        if [[ $i -eq $retries ]]; then
            log_warn "AKernel may not be fully ready; check ${DOCKER_CMD} logs ${NODE_CONTAINER_NAME}"
            return 1
        fi

        sleep $delay
    done
}

container_ip() {
    "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} inspect \
        --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1"
}

write_traefik_config() {
    local node_ip="$1"
    local traefik_dir="${DATA_DIR}/traefik"
    mkdir -p "${traefik_dir}"

    cat > "${traefik_dir}/dynamic.yml" <<EOF
http:
  routers:
    akernel-frontend:
      entryPoints:
        - websecure
      rule: "PathPrefix(\`/terminal\`) || PathPrefix(\`/api/instances\`) || PathPrefix(\`/api/jobs\`) || PathPrefix(\`/functions\`) || PathPrefix(\`/api-docs\`) || PathPrefix(\`/admin/v1/functions\`) || PathPrefix(\`/serverless/v1/functions\`) || PathPrefix(\`/serverless/v1/stream\`) || PathPrefix(\`/serverless/v1/componentshealth\`) || PathPrefix(\`/serverless/v1/posix\`) || PathPrefix(\`/serverless/v2\`) || PathPrefix(\`/frontend/v1/instance\`) || PathPrefix(\`/datasystem/v1\`) || PathPrefix(\`/app/v1\`) || PathPrefix(\`/client/v1/lease\`) || PathPrefix(\`/invocations\`) || PathPrefix(\`/global-scheduler\`) || Path(\`/healthz\`)"
      service: akernel-frontend
      tls: {}

  services:
    akernel-frontend:
      loadBalancer:
        serversTransport: akernel-frontend
        servers:
          - url: "https://${node_ip}:${FRONTEND_PORT}"

  serversTransports:
    akernel-frontend:
      insecureSkipVerify: true
      disableHTTP2: true
EOF
}

start_traefik_container() {
    local etcd_endpoint="$1"
    local dynamic_config="${DATA_DIR}/traefik/dynamic.yml"

    log_info "Starting container: ${TRAEFIK_CONTAINER_NAME}"
    "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} run -d \
        --name "${TRAEFIK_CONTAINER_NAME}" \
        --net bridge \
        --restart always \
        -v "${dynamic_config}:/etc/traefik/dynamic.yml:ro" \
        "${TRAEFIK_IMAGE}" \
        --entryPoints.web.address=:80 \
        --entryPoints.websecure.address=:443 \
        --providers.file.filename=/etc/traefik/dynamic.yml \
        --providers.etcd.endpoints="${etcd_endpoint}" \
        --providers.etcd.rootKey=traefik \
        --log.level=INFO \
        --accessLog=true \
        --accessLog.format=json \
        --accessLog.fields.names.RequestPath=drop
}

wait_for_gateway() {
    local traefik_ip="$1"
    local retries=60
    local delay=2

    log_info "Waiting for Traefik at ${traefik_ip}"
    for i in $(seq 1 ${retries}); do
        if curl --noproxy '*' -fkSs "https://${traefik_ip}/healthz" > /dev/null; then
            log_info "Traefik gateway is ready"
            return 0
        fi

        if ! "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} inspect \
            --format '{{.State.Running}}' "${TRAEFIK_CONTAINER_NAME}" 2> /dev/null \
            | grep -q true; then
            log_error "Traefik exited during startup"
            "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} logs "${TRAEFIK_CONTAINER_NAME}" || true
            return 1
        fi

        if [[ ${i} -eq ${retries} ]]; then
            log_error "Traefik gateway did not become ready"
            return 1
        fi
        sleep ${delay}
    done
}

# Show status
show_status() {
    local node_ip="$1"
    local traefik_ip="$2"

    echo ""
    log_info "Container status:"
    "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} ps -a \
        --filter "name=${NODE_CONTAINER_NAME}" \
        --filter "name=${TRAEFIK_CONTAINER_NAME}"

    echo ""
    log_info "Useful commands:"
    echo "  AKernel logs:  ${DOCKER_CMD} logs -f ${NODE_CONTAINER_NAME}"
    echo "  Traefik logs:  ${DOCKER_CMD} logs -f ${TRAEFIK_CONTAINER_NAME}"
    echo "  Enter AKernel: ${DOCKER_CMD} exec -it ${NODE_CONTAINER_NAME} bash"
    echo "  AKernel IP:    ${node_ip}"
    echo "  Traefik IP:    ${traefik_ip}"
    echo "  SDK token:     ${TOKEN_FILE}"
}

# Main
check_prerequisites
cleanup_existing
configure_auth
ensure_image "${IMAGE}"
ensure_image "${TRAEFIK_IMAGE}"
configure_container_proxy
start_node_container
wait_for_ready
NODE_IP="$(container_ip "${NODE_CONTAINER_NAME}")"
if [[ -z "${NODE_IP}" ]]; then
    log_error "Could not determine the AKernel container IP"
    exit 1
fi
write_traefik_config "${NODE_IP}"
ETCD_ENDPOINT="${NODE_IP}:${ETCD_PORT}"
log_info "Using embedded etcd endpoint: ${ETCD_ENDPOINT}"
start_traefik_container "${ETCD_ENDPOINT}"
TRAEFIK_IP="$(container_ip "${TRAEFIK_CONTAINER_NAME}")"
if [[ -z "${TRAEFIK_IP}" ]]; then
    log_error "Could not determine the Traefik container IP"
    exit 1
fi
wait_for_gateway "${TRAEFIK_IP}"
show_status "${NODE_IP}" "${TRAEFIK_IP}"

log_info "AKernel started successfully in standalone mode"
log_info "Set AKERNEL_SERVER_ADDRESS=${TRAEFIK_IP}"
log_info "Set AKERNEL_TOKEN=\$(cat ${TOKEN_FILE})"
