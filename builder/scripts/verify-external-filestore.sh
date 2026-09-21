#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

state_dir="${AKERNEL_EXTERNAL_FILESTORE_STATE_DIR:-/etc/akernel/external-filestore}"
required="${AKERNEL_EXTERNAL_FILESTORE_REQUIRED:-false}"

die() {
    echo "external filestore validation failed: $1" >&2
    exit 1
}

read_state() {
    local name=$1
    [[ -f "${state_dir}/${name}" ]] || die "state file is missing: ${name}"
    cat "${state_dir}/${name}"
}

case "${required}" in
    true|false)
        ;;
    *)
        die "AKERNEL_EXTERNAL_FILESTORE_REQUIRED must be true or false"
        ;;
esac

if [[ -e "${state_dir}" ]]; then
    [[ -d "${state_dir}" ]] || die "state path is not a directory"
    enabled="$(read_state enabled)"
    target="$(read_state target)"
    expected_source="$(read_state source)"
    expected_fstype="$(read_state fstype)"
    expected_uuid="$(read_state uuid)"
else
    enabled="${AKERNEL_EXTERNAL_FILESTORE_ENABLED:-false}"
    target="${AKERNEL_EXTERNAL_FILESTORE_TARGET:-/home/akernel/filestore}"
    expected_source="${AKERNEL_EXTERNAL_FILESTORE_SOURCE:-}"
    expected_fstype="${AKERNEL_EXTERNAL_FILESTORE_FSTYPE:-}"
    expected_uuid="${AKERNEL_EXTERNAL_FILESTORE_UUID:-}"
fi

if [[ "${required}" == "true" && "${enabled}" != "true" ]]; then
    die "external filestore is required but its enabled state is unavailable"
fi

case "${enabled}" in
    false)
        exit 0
        ;;
    true)
        ;;
    *)
        die "AKERNEL_EXTERNAL_FILESTORE_ENABLED must be true or false"
        ;;
esac

for command in findmnt mountpoint mktemp; do
    command -v "${command}" > /dev/null 2>&1 || die "${command} is required"
done

[[ -n "${expected_fstype}" ]] || die "expected filesystem type is missing"
if [[ -z "${expected_uuid}" && -z "${expected_source}" ]]; then
    die "expected filesystem UUID or source is required"
fi
mountpoint -q "${target}" || die "${target} is not a mountpoint"

actual_source="$(findmnt -n -o SOURCE -M "${target}")"
actual_fstype="$(findmnt -n -o FSTYPE -M "${target}")"
actual_uuid="$(findmnt -n -o UUID -M "${target}")"

[[ -n "${actual_source}" ]] || die "filesystem source is empty"
[[ "${actual_source}" != /dev/loop* ]] || die "loop-backed filesystems are not allowed"
[[ "${actual_fstype}" == "${expected_fstype}" ]] || \
    die "filesystem type changed from ${expected_fstype} to ${actual_fstype:-unknown}"

if [[ -n "${expected_uuid}" ]]; then
    [[ "${actual_uuid}" == "${expected_uuid}" ]] || \
        die "filesystem UUID does not match the configured device"
else
    [[ "${actual_source}" == "${expected_source}" ]] || \
        die "filesystem source changed from the configured mount"
fi

[[ ! -e "${target}/ext4.img" ]] || die "legacy loop-backed ext4.img exists"

probe="$(mktemp "${target}/.akernel-filestore-probe.XXXXXX")" || \
    die "filesystem is not writable"
rm -f -- "${probe}"
