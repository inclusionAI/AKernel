#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
standalone_dir="$(cd "${script_dir}/.." && pwd)"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT

# shellcheck disable=SC1091
source "${standalone_dir}/start.sh"

DATA_DIR="${test_root}/data"
STANDALONE_FILESTORE_DIR="${test_root}/filestore"
mkdir -p "${DATA_DIR}/filestore" "${STANDALONE_FILESTORE_DIR}"
FILESTORE_STATE_DIR="${DATA_DIR}/sandboxd/external-filestore"
mkdir -p "${DATA_DIR}/sandboxd"

fake_source=/dev/test-disk
fake_fstype=ext4
fake_uuid=test-uuid
fake_mountpoint=true

mountpoint() {
    [[ "${fake_mountpoint}" == true ]]
}

readlink() {
    [[ "$1" == -f ]]
    printf '%s\n' "$2"
}

findmnt() {
    case "$*" in
        *"-o SOURCE"*) printf '%s\n' "${fake_source}" ;;
        *"-o FSTYPE"*) printf '%s\n' "${fake_fstype}" ;;
        *"-o UUID"*) printf '%s\n' "${fake_uuid}" ;;
        *) return 2 ;;
    esac
}

expect_failure() {
    if (configure_external_filestore) > /dev/null 2>&1; then
        echo "expected external filestore launcher validation to fail" >&2
        exit 1
    fi
}

configure_external_filestore > /dev/null
args=" ${FILESTORE_RUN_ARGS[*]} "
[[ "${args}" == *" ${STANDALONE_FILESTORE_DIR}:/home/akernel/filestore "* ]]
[[ "${args}" == *" ${FILESTORE_STATE_DIR}:/etc/akernel/external-filestore:ro "* ]]
[[ "${FILESTORE_REQUIRED}" == true ]]
[[ "$(< "${FILESTORE_STATE_DIR}/enabled")" == true ]]
[[ "$(< "${FILESTORE_STATE_DIR}/source")" == "${fake_source}" ]]
[[ "$(< "${FILESTORE_STATE_DIR}/fstype")" == "${fake_fstype}" ]]
[[ "$(< "${FILESTORE_STATE_DIR}/uuid")" == "${fake_uuid}" ]]
grep -Fx 'ExecStartPre=/usr/local/bin/verify-external-filestore' \
    "${standalone_dir}/../../builder/systemd_services/sandboxd.service" > /dev/null

fake_source=/dev/loop0
expect_failure
fake_source=/dev/test-disk

fake_uuid=
expect_failure
fake_uuid=test-uuid

touch "${STANDALONE_FILESTORE_DIR}/ext4.img"
expect_failure
rm -f -- "${STANDALONE_FILESTORE_DIR}/ext4.img"

fake_mountpoint=false
expect_failure

fake_mountpoint=true
STANDALONE_FILESTORE_DIR=/
expect_failure

STANDALONE_FILESTORE_DIR="${DATA_DIR}"
expect_failure

STANDALONE_FILESTORE_DIR="${DATA_DIR}/filestore"
expect_failure

STANDALONE_FILESTORE_DIR="${test_root}"
expect_failure

STANDALONE_FILESTORE_DIR=
configure_external_filestore
[[ ! -e "${FILESTORE_STATE_DIR}" ]]
[[ "${FILESTORE_REQUIRED}" == false ]]

image_runsc=true
image_runc=true
image_label() {
    case "$2" in
        org.akernel.runsc.enabled) printf '%s\n' "${image_runsc}" ;;
        org.akernel.runc.enabled) printf '%s\n' "${image_runc}" ;;
        *) return 1 ;;
    esac
}

# Consumed dynamically by validate_image_capabilities from the sourced launcher.
# shellcheck disable=SC2034
AKERNEL_ENABLE_RUNSC=false
AKERNEL_ENABLE_RUNC=true
validate_image_capabilities > /dev/null

image_runc=false
if (validate_image_capabilities) > /dev/null 2>&1; then
    echo "expected runtime capability validation to fail without runc" >&2
    exit 1
fi
image_runc=true

# shellcheck disable=SC2034
AKERNEL_ENABLE_RUNSC=true
image_runsc=false
if (validate_image_capabilities) > /dev/null 2>&1; then
    echo "expected runtime capability validation to fail without runsc" >&2
    exit 1
fi
image_runsc=true

# Consumed by prepare_runc_host_modules from the sourced launcher.
# shellcheck disable=SC2034
AKERNEL_ENABLE_RUNC=true
overlay_available=true
erofs_available=true
loop_available=true
loaded_modules=()

host_filesystem_available() {
    case "$1" in
        overlay) [[ "${overlay_available}" == true ]] ;;
        erofs) [[ "${erofs_available}" == true ]] ;;
        *) return 1 ;;
    esac
}

host_loop_available() {
    [[ "${loop_available}" == true ]]
}

load_host_module() {
    loaded_modules+=("$1")
    case "$1" in
        overlay) overlay_available=true ;;
        erofs) erofs_available=true ;;
        loop) loop_available=true ;;
        *) return 1 ;;
    esac
}

prepare_runc_host_modules > /dev/null
[[ ${#loaded_modules[@]} -eq 0 ]]

erofs_available=false
loop_available=false
prepare_runc_host_modules > /dev/null
[[ " ${loaded_modules[*]} " == *" erofs "* ]]
[[ " ${loaded_modules[*]} " == *" loop "* ]]

echo "external filestore launcher tests passed"
