#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
# AKernel Single Node Docker Stop Script

set -e

CONTAINER_NAMES=("akernel-traefik" "akernel-node")

# Container runtime command (docker or pouch)
DOCKER_CMD=""
DOCKER_PREFIX=()

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Detect container runtime
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

if ${DOCKER_CMD} info &> /dev/null; then
    DOCKER_PREFIX=()
elif sudo -n ${DOCKER_CMD} info &> /dev/null; then
    DOCKER_PREFIX=(sudo)
else
    log_error "${DOCKER_CMD} daemon is not running"
    exit 1
fi

# Resolve a successful inventory before treating any name as absent. Stop and
# remove the captured full IDs so a concurrently reused name is not targeted.
if ! inventory="$("${DOCKER_PREFIX[@]}" ${DOCKER_CMD} ps -a --no-trunc \
    --format '{{.ID}} {{.Names}}' 2> /dev/null)"; then
    log_error "Could not list containers; AKernel shutdown was not attempted"
    exit 1
fi
CONTAINER_IDS=()
for container in "${CONTAINER_NAMES[@]}"; do
    container_id=""
    while read -r listed_id listed_name extra; do
        if [[ "${listed_name#/}" != "${container}" ]]; then continue; fi
        if [[ -n "${container_id}" || -n "${extra}" || ! "${listed_id}" =~ ^[0-9a-f]{64}$ ]]; then
            log_error "Invalid container inventory; AKernel shutdown was not attempted"
            exit 1
        fi
        container_id="${listed_id}"
    done <<< "${inventory}"
    CONTAINER_IDS+=("${container_id}")
done

# Stop the gateway before the AKernel container so no new requests arrive
# while the runtime is shutting down. Continue after individual failures.
cleanup_failed=false
for index in "${!CONTAINER_NAMES[@]}"; do
    container="${CONTAINER_NAMES[index]}"
    container_id="${CONTAINER_IDS[index]}"
    if [[ -n "${container_id}" ]]; then
        log_info "Stopping container: ${container}"
        if ! "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} stop "${container_id}" &> /dev/null; then
            log_error "Failed to stop container: ${container}"
            cleanup_failed=true
        fi
        if "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} rm "${container_id}" &> /dev/null; then
            log_info "Container removed: ${container}"
        else
            log_error "Failed to remove container: ${container}"
            cleanup_failed=true
        fi
    else
        log_warn "Container '${container}' not found"
    fi
done

if [[ "${cleanup_failed}" == true ]]; then
    log_error "AKernel shutdown incomplete; inspect the failed containers and retry"
    exit 1
fi
log_info "AKernel node stopped successfully!"
