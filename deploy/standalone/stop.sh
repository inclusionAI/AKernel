#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
# AKernel Single Node Docker Stop Script

set -e

CONTAINER_NAMES=(
    "${AKERNEL_TRAEFIK_CONTAINER_NAME:-akernel-traefik}"
    "${AKERNEL_NODE_CONTAINER_NAME:-akernel-node}"
)

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

# Stop the gateway before the AKernel container so no new requests arrive
# while the runtime is shutting down.
for container in "${CONTAINER_NAMES[@]}"; do
    if "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} container inspect "${container}" &> /dev/null; then
        log_info "Stopping container: ${container}"
        "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} stop "${container}" &> /dev/null || true
        "${DOCKER_PREFIX[@]}" ${DOCKER_CMD} rm "${container}" &> /dev/null || true
        log_info "Container removed: ${container}"
    else
        log_warn "Container '${container}' not found"
    fi
done

log_info "AKernel node stopped successfully!"
