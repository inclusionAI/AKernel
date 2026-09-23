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
NODE_CONTAINER_NAME="akernel-node"
IMAGE="${IMAGE:-akerneldev/all-in-one:latest}"
TOKEN_FILE="${DATA_DIR}/token"
SANDBOXD_CONFIG_FILE="${DATA_DIR}/sandboxd/config.toml"
AKERNEL_NAT_BACKEND="${AKERNEL_NAT_BACKEND:-iptables}"
AKERNEL_ENABLE_RUNC="${AKERNEL_ENABLE_RUNC:-false}"
AKERNEL_CONTROL_BIND="${AKERNEL_CONTROL_BIND:-0.0.0.0}"
AKERNEL_CONTROL_PORT="${AKERNEL_CONTROL_PORT:-443}"
AKERNEL_DATA_BIND="${AKERNEL_DATA_BIND:-0.0.0.0}"
AKERNEL_DATA_PORT="${AKERNEL_DATA_PORT:-80}"
AKERNEL_ENDPOINT_HOST="${AKERNEL_ENDPOINT_HOST:-127.0.0.1}"
AKERNEL_CHUNK_DB_SIZE="${AKERNEL_CHUNK_DB_SIZE:-}"

# Container runtime command (docker or pouch)
DOCKER_CMD=""
DOCKER_PREFIX=()
PROXY_RUN_ARGS=()
GPU_RUN_ARGS=()

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

# Probe the authenticated ADX edge without exposing the API key in the curl
# command line. curl reads its header from stdin, so the key does not appear in
# process listings or normal command logs.
probe_adx_health() {
    local url="$1"
    local token

    if [[ ! -s "${TOKEN_FILE}" ]]; then
        return 1
    fi
    token="$(< "${TOKEN_FILE}")"
    printf 'header = "X-Auth: %s"\n' "${token}" \
        | curl --noproxy '*' -fkSs --config - "${url}" > /dev/null
}

probe_adx_capacity() {
    local url="$1"
    local token
    local response

    if [[ ! -s "${TOKEN_FILE}" ]]; then
        return 1
    fi
    token="$(< "${TOKEN_FILE}")"
    response="$(
        printf 'header = "X-Auth: %s"\n' "${token}" \
            | curl --noproxy '*' -fkSs --config - "${url}"
    )" || return 1
    python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, OSError):
    raise SystemExit(1)

items = payload.get("items")
if not isinstance(items, list):
    raise SystemExit(1)

for item in items:
    if not isinstance(item, dict):
        continue
    allocatable = item.get("allocatable")
    if not isinstance(allocatable, dict):
        continue
    if allocatable.get("CPU", 0) > 0 and allocatable.get("Memory", 0) > 0:
        raise SystemExit(0)
raise SystemExit(1)
' <<< "${response}"
}

probe_adx_data_listener() {
    local url="$1"
    local status

    status="$(
        curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' "${url}"
    )" || return 1
    [[ "${status}" == "426" ]]
}

probe_node_health() {
    local url="$1"

    "${DOCKER_PREFIX[@]}" "${DOCKER_CMD}" exec "${NODE_CONTAINER_NAME}" \
        curl --noproxy '*' -fSs "${url}" > /dev/null
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
        log_warn "/dev/kvm is unavailable; standalone will support runsc but will not advertise Kata or Firecracker"
    elif [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
        log_warn "/dev/kvm is not accessible to the current user; verify that the privileged node container can access it before using Kata or Firecracker"
    fi

    if ! command -v curl &> /dev/null; then
        log_error "curl is required to verify the standalone endpoint"
        exit 1
    fi
    if ! command -v python3 &> /dev/null; then
        log_error "python3 is required to verify the ADX resource directory"
        exit 1
    fi

    local port
    for port in "${AKERNEL_CONTROL_PORT}" "${AKERNEL_DATA_PORT}"; do
        if [[ ! "${port}" =~ ^[0-9]+$ || "${port}" -lt 1 || "${port}" -gt 65535 ]]; then
            log_error "standalone ports must be integers between 1 and 65535"
            exit 1
        fi
    done
    if [[ "${AKERNEL_CONTROL_BIND}:${AKERNEL_CONTROL_PORT}" == \
          "${AKERNEL_DATA_BIND}:${AKERNEL_DATA_PORT}" ]]; then
        log_error "control and data listeners cannot use the same host address and port"
        exit 1
    fi
    if [[ -z "${AKERNEL_ENDPOINT_HOST}" ]]; then
        log_error "AKERNEL_ENDPOINT_HOST must not be empty"
        exit 1
    fi

    case "${AKERNEL_ENABLE_RUNC}" in
        true|false)
            ;;
        *)
            log_error "AKERNEL_ENABLE_RUNC must be true or false"
            exit 1
            ;;
    esac

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

# Stop and remove existing container
cleanup_existing() {
    if "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} container inspect "${NODE_CONTAINER_NAME}" &> /dev/null; then
        log_warn "Existing container '${NODE_CONTAINER_NAME}' found; run stop.sh first"
        exit 1
    fi
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

configure_gpu() {
    if [[ "${AKERNEL_ENABLE_GPU:-false}" != "true" ]]; then
        return 0
    fi
    if [[ "${DOCKER_CMD}" != "docker" ]]; then
        log_error "AKERNEL_ENABLE_GPU currently requires Docker"
        exit 1
    fi

    GPU_RUN_ARGS=(
        --gpus "${AKERNEL_GPU_DEVICES:-all}"
        -e NVIDIA_DRIVER_CAPABILITIES="${NVIDIA_DRIVER_CAPABILITIES:-compute,utility}"
    )
    log_info "Enabling NVIDIA GPU access for the AKernel node container"
}

configure_network() {
    local config_tmp="${SANDBOXD_CONFIG_FILE}.tmp"
    local sed_args=(
        -E
        -e "s/^[[:space:]]*nat_backend[[:space:]]*=.*/nat_backend=\"${AKERNEL_NAT_BACKEND}\"/"
    )

    case "${AKERNEL_NAT_BACKEND}" in
        iptables|bpfnat)
            ;;
        *)
            log_error "AKERNEL_NAT_BACKEND must be 'iptables' or 'bpfnat'"
            exit 1
            ;;
    esac

    if ! grep -q '^[[:space:]]*nat_backend[[:space:]]*=' \
        "${CONFIG_DIR}/sandboxd_config.toml"; then
        log_error "Missing nat_backend in ${CONFIG_DIR}/sandboxd_config.toml"
        exit 1
    fi
    if [[ "${AKERNEL_ENABLE_RUNC}" == "true" ]]; then
        if ! grep -q '^[[:space:]]*# AKERNEL_RUNTIME_RUNC[[:space:]]*$' \
            "${CONFIG_DIR}/sandboxd_config.toml"; then
            log_error "AKERNEL_ENABLE_RUNC requires the # AKERNEL_RUNTIME_RUNC marker in sandboxd_config.toml"
            exit 1
        fi
        sed_args+=(
            -e 's|^[[:space:]]*# AKERNEL_RUNTIME_RUNC[[:space:]]*$|runc="/usr/local/bin/runc"|'
        )
    fi
    if [[ -n "${AKERNEL_CHUNK_DB_SIZE}" ]]; then
        if [[ ! "${AKERNEL_CHUNK_DB_SIZE}" =~ ^[0-9]+(B|KiB|MiB|GiB|TiB)?$ ]]; then
            log_error "AKERNEL_CHUNK_DB_SIZE must be whole bytes or an integer with B/KiB/MiB/GiB/TiB"
            exit 1
        fi
        if ! grep -q '^[[:space:]]*# AKERNEL_CHUNK_DB_SIZE[[:space:]]*$' \
            "${CONFIG_DIR}/sandboxd_config.toml"; then
            log_error "AKERNEL_CHUNK_DB_SIZE requires the # AKERNEL_CHUNK_DB_SIZE marker in sandboxd_config.toml"
            exit 1
        fi
        sed_args+=(
            -e "s|^[[:space:]]*# AKERNEL_CHUNK_DB_SIZE[[:space:]]*$|chunk_db_size=\"${AKERNEL_CHUNK_DB_SIZE}\"|"
        )
    fi
    sed "${sed_args[@]}" "${CONFIG_DIR}/sandboxd_config.toml" > "${config_tmp}"
    mv "${config_tmp}" "${SANDBOXD_CONFIG_FILE}"

    if [[ "${AKERNEL_ENABLE_RUNC}" == "true" ]]; then
        log_info "Enabling the optional runc sandbox runtime"
    fi

    if [[ "${AKERNEL_NAT_BACKEND}" == "bpfnat" ]]; then
        log_warn "Using the experimental bpfnat network backend"
    else
        log_info "Using the iptables network backend"
    fi
}

prepare_host_network_modules() {
    local modprobe_bin
    modprobe_bin="$(command -v modprobe || true)"
    if [[ -z "${modprobe_bin}" ]]; then
        log_error "modprobe is required to load AKernel host network modules"
        exit 1
    fi

    if [[ "$(id -u)" -eq 0 ]]; then
        "${modprobe_bin}" tun
    elif sudo -n "${modprobe_bin}" tun; then
        :
    else
        log_error "Unable to load tun; run this script as root or allow passwordless sudo for modprobe"
        exit 1
    fi
    if [[ ! -c /dev/net/tun ]]; then
        log_error "tun loaded but /dev/net/tun is unavailable"
        exit 1
    fi
    log_info "Loaded host tun module for pooled TAP networking"

    if [[ "${AKERNEL_NAT_BACKEND}" != "iptables" ]]; then
        return 0
    fi

    if [[ "$(id -u)" -eq 0 ]]; then
        "${modprobe_bin}" ip_tables
        "${modprobe_bin}" iptable_filter
        "${modprobe_bin}" ip6_tables
        "${modprobe_bin}" ip6table_filter
        "${modprobe_bin}" br_netfilter
        "${modprobe_bin}" xt_physdev
        "${modprobe_bin}" nf_conntrack
        "${modprobe_bin}" nf_conntrack_netlink
        "${modprobe_bin}" xt_conntrack
        "${modprobe_bin}" xt_connmark
        "${modprobe_bin}" ip_set
        "${modprobe_bin}" ip_set_hash_ip
        "${modprobe_bin}" xt_set
    elif sudo -n "${modprobe_bin}" ip_tables &&
         sudo -n "${modprobe_bin}" iptable_filter &&
         sudo -n "${modprobe_bin}" ip6_tables &&
         sudo -n "${modprobe_bin}" ip6table_filter &&
         sudo -n "${modprobe_bin}" br_netfilter &&
         sudo -n "${modprobe_bin}" xt_physdev &&
         sudo -n "${modprobe_bin}" nf_conntrack &&
         sudo -n "${modprobe_bin}" nf_conntrack_netlink &&
         sudo -n "${modprobe_bin}" xt_conntrack &&
         sudo -n "${modprobe_bin}" xt_connmark &&
         sudo -n "${modprobe_bin}" ip_set &&
         sudo -n "${modprobe_bin}" ip_set_hash_ip &&
         sudo -n "${modprobe_bin}" xt_set; then
        :
    else
        log_error "Unable to load required iptables ACL modules; run this script as root or allow passwordless sudo for modprobe"
        exit 1
    fi

    if [[ ! -e /proc/sys/net/bridge/bridge-nf-call-iptables ||
          ! -e /proc/sys/net/bridge/bridge-nf-call-ip6tables ]]; then
        log_error "br_netfilter loaded but bridge netfilter sysctls are unavailable"
        exit 1
    fi
    log_info "Loaded host filter, bridge, conntrack, and ipset modules for the iptables ACL backend"
}

# Start the AKernel all-in-one container and publish Edge's distinct control
# and data listeners directly on the host.
start_node_container() {
    log_info "Starting container: ${NODE_CONTAINER_NAME}"
    "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} run -d \
        --name "${NODE_CONTAINER_NAME}" \
        --privileged \
        --net bridge \
        --restart always \
        -p "${AKERNEL_CONTROL_BIND}:${AKERNEL_CONTROL_PORT}:8443" \
        -p "${AKERNEL_DATA_BIND}:${AKERNEL_DATA_PORT}:8080" \
        -e container=oci \
        -e AKS_LOCAL_MODE="true" \
        -e NODE_NAME="$(hostname)" \
        -e POD_NAME=akernel-node-local \
        -e POD_NAMESPACE=default \
        -e TZ=Asia/Shanghai \
        -e ENABLE_TRACE="${ENABLE_TRACE:-false}" \
        -e ENABLE_METRICS="${ENABLE_METRICS:-false}" \
        "${PROXY_RUN_ARGS[@]}" \
        "${GPU_RUN_ARGS[@]}" \
        --entrypoint=/usr/local/bin/akernel-entrypoint \
        -v "${DATA_DIR}:/home/akernel" \
        -v "${CONFIG_DIR}/oss_auths.json:/home/akernel/sandboxd/config/oss_auths.json:ro" \
        -v "${CONFIG_DIR}/oss.json:/home/akernel/sandboxd/config/oss.json:ro" \
        -v "${CONFIG_DIR}/registry_auths.json:/home/akernel/sandboxd/config/registry_auths.json:ro" \
        -v "${CONFIG_DIR}/registry.json:/home/akernel/sandboxd/config/registry.json:ro" \
        -v "${CONFIG_DIR}/config.json:/home/akernel/images/config.json:ro" \
        -v "${SANDBOXD_CONFIG_FILE}:/home/akernel/sandboxd/config.toml:ro" \
        "${IMAGE}"
}

# Wait for container to be ready
wait_for_ready() {
    log_info "Waiting for container to be ready..."

    local retries=30
    local delay=2

    for i in $(seq 1 $retries); do
        if probe_node_health http://127.0.0.1:18080/healthz; then
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

wait_for_endpoints() {
    local control_url="$1"
    local data_url="$2"
    local retries=60
    local delay=2

    log_info "Waiting for ADX Edge and an allocatable node"
    for i in $(seq 1 ${retries}); do
        if probe_adx_capacity "${control_url}/api/sandbox/v1/resources" &&
           probe_adx_data_listener "${data_url}/api/sandbox/v1/resources"; then
            log_info "ADX control and data endpoints are ready"
            return 0
        fi

        if ! "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} inspect \
            --format '{{.State.Running}}' "${NODE_CONTAINER_NAME}" 2> /dev/null \
            | grep -q true; then
            log_error "AKernel node exited during startup"
            "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} logs "${NODE_CONTAINER_NAME}" || true
            return 1
        fi

        if [[ ${i} -eq ${retries} ]]; then
            log_error "ADX endpoints did not become ready with allocatable node capacity"
            return 1
        fi
        sleep ${delay}
    done
}

# Show status
show_status() {
    local control_url="$1"
    local data_url="$2"

    echo ""
    log_info "Container status:"
    "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} ps -a \
        --filter "name=${NODE_CONTAINER_NAME}"

    echo ""
    log_info "Useful commands:"
    echo "  AKernel logs:  ${DOCKER_CMD} logs -f ${NODE_CONTAINER_NAME}"
    echo "  Enter AKernel: ${DOCKER_CMD} exec -it ${NODE_CONTAINER_NAME} bash"
    echo "  Control URL:   ${control_url}"
    echo "  Data URL:      ${data_url}"
    echo "  SDK token:     ${TOKEN_FILE}"
}

# Bootstrap credentials belong to deployment state, not certificate generation.
configure_auth() {
    python3 - "${DATA_DIR}" "${TOKEN_FILE}" <<'PYKEY'
import os
from pathlib import Path
import secrets
import sys
import tempfile

root, token = map(Path, sys.argv[1:])
key = root / "adx/secrets/admin-key"
key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
if not key.exists():
    value = token.read_text().strip() if token.is_file() else secrets.token_hex(32)
    if not 32 <= len(value.encode()) <= 512:
        raise SystemExit("deployment token must contain 32..512 bytes")
    fd, temporary = tempfile.mkstemp(prefix=".admin-key-", dir=key.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, key)
        except FileExistsError:
            pass  # Another initializer already published the deployment key.
    finally:
        os.unlink(temporary)
if not 32 <= len(key.read_text().strip().encode()) <= 512:
    raise SystemExit("deployment admin-key must contain 32..512 bytes")
os.chmod(key, 0o600)
if not token.is_symlink() or token.resolve() != key.resolve():
    link = token.with_name(".token-" + secrets.token_hex(8))
    try:
        link.symlink_to(os.path.relpath(key, token.parent))
        os.replace(link, token)
    finally:
        link.unlink(missing_ok=True)
PYKEY
}

main() {
    check_prerequisites
    cleanup_existing
    ensure_image "${IMAGE}"
    configure_container_proxy
    configure_gpu
    configure_network
    prepare_host_network_modules
    configure_auth
    start_node_container
    wait_for_ready
    CONTROL_URL="https://${AKERNEL_ENDPOINT_HOST}:${AKERNEL_CONTROL_PORT}"
    DATA_URL="http://${AKERNEL_ENDPOINT_HOST}:${AKERNEL_DATA_PORT}"
    wait_for_endpoints "${CONTROL_URL}" "${DATA_URL}"
    show_status "${CONTROL_URL}" "${DATA_URL}"

    log_info "AKernel started successfully in standalone mode"
    local sdk_address="${AKERNEL_ENDPOINT_HOST}"
    if [[ "${AKERNEL_CONTROL_PORT}" != 443 ]]; then
        sdk_address+=":${AKERNEL_CONTROL_PORT}"
    fi
    log_info "Set AKERNEL_SERVER_ADDRESS=${sdk_address}"
    if [[ "${AKERNEL_DATA_PORT}" != 80 || "${AKERNEL_CONTROL_PORT}" != 443 ]]; then
        log_info "Set AKERNEL_GATEWAY_ADDRESS=${DATA_URL}"
    fi
    log_info "Set AKERNEL_TOKEN=\$(cat ${TOKEN_FILE})"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
