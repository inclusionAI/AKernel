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
SANDBOXD_CONFIG_FILE="${DATA_DIR}/sandboxd/config.toml"
AKERNEL_NAT_BACKEND="${AKERNEL_NAT_BACKEND:-iptables}"
AKERNEL_ENABLE_RUNSC="${AKERNEL_ENABLE_RUNSC:-true}"
AKERNEL_ENABLE_RUNC="${AKERNEL_ENABLE_RUNC:-false}"
STANDALONE_FILESTORE_DIR="${STANDALONE_FILESTORE_DIR:-}"
YR_IMAGE_PROCESS_CONFIG="${YR_IMAGE_PROCESS_CONFIG:-/run/akernel/yr-image-process.json}"
LITEBUS_DATA_KEY=""

# Container runtime command (docker or pouch)
DOCKER_CMD=""
DOCKER_PREFIX=()
PROXY_RUN_ARGS=()
GPU_RUN_ARGS=()
FILESTORE_RUN_ARGS=()
FILESTORE_STATE_DIR="${DATA_DIR}/sandboxd/external-filestore"
FILESTORE_REQUIRED=false

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
        log_warn "/dev/kvm is unavailable; standalone will support runsc but will not advertise Kata or Firecracker"
    elif [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
        log_warn "/dev/kvm is not accessible to the current user; verify that the privileged node container can access it before using Kata or Firecracker"
    fi

    if ! command -v curl &> /dev/null; then
        log_error "curl is required to verify the standalone endpoint"
        exit 1
    fi

    if ! command -v python3 &> /dev/null; then
        log_error "python3 is required to generate standalone credentials"
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
    case "${AKERNEL_ENABLE_RUNSC}" in
        true|false)
            ;;
        *)
            log_error "AKERNEL_ENABLE_RUNSC must be true or false"
            exit 1
            ;;
    esac
    if [[ "${AKERNEL_ENABLE_RUNSC}" == "false" && "${AKERNEL_ENABLE_RUNC}" != "true" ]]; then
        log_error "Disabling runsc requires AKERNEL_ENABLE_RUNC=true"
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

image_label() {
    local image=$1 label=$2 value
    value="$("${DOCKER_PREFIX[@]}" ${DOCKER_CMD} image inspect \
        --format "{{ index .Config.Labels \"${label}\" }}" "${image}")"
    if [[ "${value}" == "<no value>" ]]; then
        value=""
    fi
    printf '%s\n' "${value}"
}

validate_image_capabilities() {
    local image_runsc_enabled image_runc_enabled
    image_runsc_enabled="$(image_label "${IMAGE}" org.akernel.runsc.enabled)"
    image_runc_enabled="$(image_label "${IMAGE}" org.akernel.runc.enabled)"

    if [[ -z "${image_runsc_enabled}" ]]; then
        log_warn "Image has no runsc capability label; assuming legacy runsc support"
        image_runsc_enabled=true
    fi
    case "${image_runsc_enabled}" in
        true|false) ;;
        *)
            log_error "Invalid org.akernel.runsc.enabled label on ${IMAGE}: ${image_runsc_enabled}"
            exit 1
            ;;
    esac
    case "${image_runc_enabled}" in
        true|false) ;;
        *)
            log_error "Missing or invalid org.akernel.runc.enabled label on ${IMAGE}"
            exit 1
            ;;
    esac

    if [[ "${AKERNEL_ENABLE_RUNSC}" == "true" && "${image_runsc_enabled}" != "true" ]]; then
        log_error "AKERNEL_ENABLE_RUNSC=true but ${IMAGE} does not contain runsc"
        exit 1
    fi
    if [[ "${AKERNEL_ENABLE_RUNC}" == "true" && "${image_runc_enabled}" != "true" ]]; then
        log_error "AKERNEL_ENABLE_RUNC=true but ${IMAGE} does not contain runc"
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

configure_external_filestore() {
    if [[ -z "${STANDALONE_FILESTORE_DIR}" ]]; then
        FILESTORE_REQUIRED=false
        FILESTORE_RUN_ARGS=()
        rm -f -- \
            "${FILESTORE_STATE_DIR}/enabled" \
            "${FILESTORE_STATE_DIR}/target" \
            "${FILESTORE_STATE_DIR}/source" \
            "${FILESTORE_STATE_DIR}/fstype" \
            "${FILESTORE_STATE_DIR}/uuid"
        rmdir "${FILESTORE_STATE_DIR}" 2> /dev/null || true
        return 0
    fi

    local resolved data_resolved source filesystem filesystem_uuid probe state_tmp
    for command in findmnt mountpoint readlink mktemp; do
        if ! command -v "${command}" &> /dev/null; then
            log_error "${command} is required for an external standalone filestore"
            exit 1
        fi
    done

    resolved="$(readlink -f "${STANDALONE_FILESTORE_DIR}")"
    if [[ ! -d "${resolved}" ]] || ! mountpoint -q "${resolved}"; then
        log_error "STANDALONE_FILESTORE_DIR must resolve to a mounted directory"
        exit 1
    fi
    data_resolved="$(readlink -f "${DATA_DIR}")"
    if [[ "${resolved}" == / ||
          "${resolved}" == "${data_resolved}" ||
          "${resolved}" == "${data_resolved}"/* ||
          "${data_resolved}" == "${resolved}"/* ]]; then
        log_error "STANDALONE_FILESTORE_DIR must not be / or overlap the standalone data directory"
        exit 1
    fi
    source="$(findmnt -n -o SOURCE -M "${resolved}")"
    filesystem="$(findmnt -n -o FSTYPE -M "${resolved}")"
    filesystem_uuid="$(findmnt -n -o UUID -M "${resolved}")"
    if [[ -z "${source}" || -z "${filesystem}" ]]; then
        log_error "Unable to identify the external filestore filesystem"
        exit 1
    fi
    if [[ "${source}" == /dev/loop* ]]; then
        log_error "STANDALONE_FILESTORE_DIR must not be backed by a loop device"
        exit 1
    fi
    if [[ "${source}" == /dev/* && -z "${filesystem_uuid}" ]]; then
        log_error "Block-device filestores must have a filesystem UUID"
        exit 1
    fi
    if [[ -e "${DATA_DIR}/filestore/ext4.img" ]]; then
        log_error "legacy loop-backed filestore image exists in the standalone data directory"
        exit 1
    fi
    if [[ -e "${resolved}/ext4.img" ]]; then
        log_error "legacy loop-backed ext4.img exists in the external filestore"
        exit 1
    fi

    probe="$(mktemp "${resolved}/.akernel-filestore-probe.XXXXXX")"
    rm -f -- "${probe}"
    state_tmp="$(mktemp -d "${DATA_DIR}/sandboxd/.external-filestore.XXXXXX")"
    printf 'true\n' > "${state_tmp}/enabled"
    printf '/home/akernel/filestore\n' > "${state_tmp}/target"
    printf '%s\n' "${source}" > "${state_tmp}/source"
    printf '%s\n' "${filesystem}" > "${state_tmp}/fstype"
    printf '%s\n' "${filesystem_uuid}" > "${state_tmp}/uuid"
    chmod 0644 "${state_tmp}"/*
    rm -f -- \
        "${FILESTORE_STATE_DIR}/enabled" \
        "${FILESTORE_STATE_DIR}/target" \
        "${FILESTORE_STATE_DIR}/source" \
        "${FILESTORE_STATE_DIR}/fstype" \
        "${FILESTORE_STATE_DIR}/uuid"
    rmdir "${FILESTORE_STATE_DIR}" 2> /dev/null || true
    mv "${state_tmp}" "${FILESTORE_STATE_DIR}"
    FILESTORE_RUN_ARGS=(
        -v "${resolved}:/home/akernel/filestore"
        -v "${FILESTORE_STATE_DIR}:/etc/akernel/external-filestore:ro"
    )
    FILESTORE_REQUIRED=true
    log_info "Using external filestore mount: ${resolved} (${source}, ${filesystem})"
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
    if [[ "${AKERNEL_ENABLE_RUNSC}" == "false" ]]; then
        if [[ "$(grep -Fxc 'runsc="/usr/local/bin/runsc"' \
            "${CONFIG_DIR}/sandboxd_config.toml")" != 1 ]]; then
            log_error "Disabling runsc requires exactly one runtime binary setting"
            exit 1
        fi
        sed_args+=(
            -e '/^[[:space:]]*runsc="\/usr\/local\/bin\/runsc"[[:space:]]*$/d'
        )
    fi
    if [[ -n "${STANDALONE_FILESTORE_DIR}" ]]; then
        if [[ "$(grep -Ec '^[[:space:]]*filestore_dir_size[[:space:]]*=' \
            "${CONFIG_DIR}/sandboxd_config.toml")" != 1 ]]; then
            log_error "External filestore requires exactly one filestore_dir_size setting"
            exit 1
        fi
        sed_args+=(
            -e 's|^[[:space:]]*filestore_dir_size[[:space:]]*=.*|filestore_dir_size=""|'
        )
    fi
    sed "${sed_args[@]}" "${CONFIG_DIR}/sandboxd_config.toml" > "${config_tmp}"
    mv "${config_tmp}" "${SANDBOXD_CONFIG_FILE}"

    if [[ "${AKERNEL_ENABLE_RUNC}" == "true" ]]; then
        log_info "Enabling the optional runc sandbox runtime"
    fi
    if [[ "${AKERNEL_ENABLE_RUNSC}" == "false" ]]; then
        log_info "Disabling the gVisor runsc sandbox runtime"
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

host_filesystem_available() {
    grep -Eq "(^|[[:space:]])$1$" /proc/filesystems
}

host_loop_available() {
    [[ -c /dev/loop-control ]]
}

load_host_module() {
    local module=$1 modprobe_bin
    modprobe_bin="$(command -v modprobe || true)"
    [[ -n "${modprobe_bin}" ]] || return 1
    if [[ "$(id -u)" -eq 0 ]]; then
        "${modprobe_bin}" "${module}"
    else
        sudo -n "${modprobe_bin}" "${module}"
    fi
}

prepare_runc_host_modules() {
    if [[ "${AKERNEL_ENABLE_RUNC}" != "true" ]]; then
        return 0
    fi

    local filesystem
    if host_filesystem_available overlay &&
       host_filesystem_available erofs &&
       host_loop_available; then
        log_info "Host overlay, EROFS, and loop capabilities are ready for runc"
        return 0
    fi

    for filesystem in overlay erofs; do
        if ! host_filesystem_available "${filesystem}"; then
            if ! load_host_module "${filesystem}"; then
                log_error "Unable to load ${filesystem}; run this script as root or allow passwordless sudo for modprobe"
                exit 1
            fi
        fi
    done
    if ! host_loop_available; then
        if ! load_host_module loop; then
            log_error "Unable to load loop; run this script as root or allow passwordless sudo for modprobe"
            exit 1
        fi
    fi

    for filesystem in overlay erofs; do
        host_filesystem_available "${filesystem}" || \
            { log_error "runc requires the host ${filesystem} filesystem"; exit 1; }
    done
    if ! host_loop_available; then
        log_error "runc requires /dev/loop-control"
        exit 1
    fi
    log_info "Loaded host overlay, EROFS, and loop modules for runc"
}

# Start the AKernel all-in-one container. Traefik runs separately so traffic
# from the gateway enters this network namespace through PREROUTING.
start_node_container() {
    log_info "Starting container: ${NODE_CONTAINER_NAME}"
    # FunctionMaster's HTTP provider publishes the per-sandbox routes required
    # by reverse tunnels; the legacy etcd mode cannot publish those routes.

    "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} run -d \
        --name "${NODE_CONTAINER_NAME}" \
        --privileged \
        --net bridge \
        --restart always \
        -e container=oci \
        -e AKS_LOCAL_MODE="true" \
        -e YR_RRT_CONTROL_SOCKET_PATH="/run/akernel" \
        -e YR_IMAGE_PROCESS_CONFIG="${YR_IMAGE_PROCESS_CONFIG}" \
        -e TRAEFIK_MODE="http" \
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
        -e AKERNEL_EXTERNAL_FILESTORE_REQUIRED="${FILESTORE_REQUIRED}" \
        "${PROXY_RUN_ARGS[@]}" \
        "${GPU_RUN_ARGS[@]}" \
        --entrypoint=/usr/local/bin/akernel-entrypoint \
        -v "${DATA_DIR}:/home/akernel" \
        "${FILESTORE_RUN_ARGS[@]}" \
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
    sandbox-router:
      entryPoints:
        - websecure
      rule: "PathPrefix(\`/api/sandbox\`) || PathPrefix(\`/direct/\`) || Path(\`/direct\`)"
      priority: 100
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
    local provider_endpoint="$1"
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
        --providers.http.endpoint="${provider_endpoint}" \
        --providers.http.pollInterval=1s \
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

main() {
    check_prerequisites
    cleanup_existing
    configure_auth
    ensure_image "${IMAGE}"
    ensure_image "${TRAEFIK_IMAGE}"
    validate_image_capabilities
    configure_container_proxy
    configure_gpu
    configure_external_filestore
    configure_network
    prepare_runc_host_modules
    prepare_host_network_modules
    start_node_container
    wait_for_ready
    NODE_IP="$(container_ip "${NODE_CONTAINER_NAME}")"
    if [[ -z "${NODE_IP}" ]]; then
        log_error "Could not determine the AKernel container IP"
        exit 1
    fi
    write_traefik_config "${NODE_IP}"
    TRAEFIK_PROVIDER_ENDPOINT="http://${NODE_IP}:22770/global-scheduler/traefik/config"
    log_info "Using FunctionMaster route provider: ${TRAEFIK_PROVIDER_ENDPOINT}"
    start_traefik_container "${TRAEFIK_PROVIDER_ENDPOINT}"
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
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
