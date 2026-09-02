#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

configure_snapshot_args() {
    local rrt_capability_file="${1:?RRT capability file is required}"
    local checkpoint_dir="${2:?checkpoint directory is required}"
    local standalone="${3:?standalone mode value is required}"
    local s3_capability_file="${4:-/home/yuanrong/.akernel-s3-snapshot-capable}"
    local backend="${AKERNEL_SNAPSHOT_STORAGE_BACKEND:-datasystem}"

    snapshot_args=()
    standalone_snapshot_args=()
    unset SNAPSHOT_S3_ACCESS_KEY SNAPSHOT_S3_SECRET_KEY SNAPSHOT_S3_SECURITY_TOKEN
    if [ ! -f "${rrt_capability_file}" ] \
        && [ ! -f /home/yuanrong/yr-runtime-rootfs.img ]; then
        echo "snapshot requires an image built with the RRT runtime" >&2
        return 1
    fi
    mkdir -p "${checkpoint_dir}"
    if [ ! -w "${checkpoint_dir}" ]; then
        echo "checkpoint directory is not writable: ${checkpoint_dir}" >&2
        return 1
    fi
    local configured_snapshot_args=(
        --snapshot_storage_backend "${backend}"
        --checkpoint_dir "${checkpoint_dir}"
    )
    local configured_standalone_snapshot_args=()
    local s3_access_key=""
    local s3_secret_key=""
    local s3_security_token=""
    case "${backend}" in
        datasystem) ;;
        s3)
            if [ ! -f "${s3_capability_file}" ]; then
                echo "S3 snapshot storage requires an S3-capable openYuanRong core" >&2
                return 1
            fi
            local provider="${AKERNEL_SNAPSHOT_S3_PROVIDER:-}"
            local storage_mode="${AKERNEL_SNAPSHOT_STORAGE_MODE:-distributed_cache}"
            local endpoint="${AKERNEL_SNAPSHOT_S3_ENDPOINT:-}"
            local region="${AKERNEL_SNAPSHOT_S3_REGION:-}"
            local bucket="${AKERNEL_SNAPSHOT_S3_BUCKET:-}"
            s3_access_key="${AKERNEL_SNAPSHOT_S3_ACCESS_KEY:-}"
            s3_secret_key="${AKERNEL_SNAPSHOT_S3_SECRET_KEY:-}"
            s3_security_token="${AKERNEL_SNAPSHOT_S3_SECURITY_TOKEN:-}"
            local use_https="${AKERNEL_SNAPSHOT_S3_USE_HTTPS:-}"
            local path_style="${AKERNEL_SNAPSHOT_S3_PATH_STYLE:-}"
            case "${provider}" in generic|obs|oss) ;; *)
                echo "AKERNEL_SNAPSHOT_S3_PROVIDER must be generic, obs, or oss" >&2
                return 1
            esac
            case "${storage_mode}" in distributed_cache|distributed_only) ;; *)
                echo "AKERNEL_SNAPSHOT_STORAGE_MODE must be distributed_cache or distributed_only" >&2
                return 1
            esac
            if [ -z "${endpoint}" ] || [ -z "${region}" ] || [ -z "${bucket}" ] || \
               [ -z "${s3_access_key}" ] || [ -z "${s3_secret_key}" ]; then
                echo "S3 snapshot storage requires endpoint, region, bucket, access key, and secret key" >&2
                return 1
            fi
            case "${use_https}" in true|false) ;; *)
                echo "AKERNEL_SNAPSHOT_S3_USE_HTTPS must be true or false" >&2
                return 1
            esac
            case "${path_style}" in true|false) ;; *)
                echo "AKERNEL_SNAPSHOT_S3_PATH_STYLE must be true or false" >&2
                return 1
            esac
            if [ "${provider}" = "oss" ] && [ "${path_style}" = "true" ]; then
                echo "OSS S3-compatible snapshot storage requires virtual-hosted addressing" >&2
                return 1
            fi
            configured_snapshot_args+=(
                --snapshot_storage_mode "${storage_mode}"
                --snapshot_s3_provider "${provider}"
                --snapshot_s3_endpoint "${endpoint}"
                --snapshot_s3_region "${region}"
                --snapshot_s3_bucket "${bucket}"
                --snapshot_s3_use_https "${use_https}"
                --snapshot_s3_path_style "${path_style}"
            )
            ;;
        *)
            echo "AKERNEL_SNAPSHOT_STORAGE_BACKEND must be datasystem or s3" >&2
            return 1
            ;;
    esac
    configured_standalone_snapshot_args=("${configured_snapshot_args[@]}")
    case "${standalone}" in
        true) configured_standalone_snapshot_args+=(--data_system_enable true) ;;
        false) ;;
        *) echo "AKS_LOCAL_MODE must be true or false" >&2; return 1 ;;
    esac
    snapshot_args=("${configured_snapshot_args[@]}")
    standalone_snapshot_args=("${configured_standalone_snapshot_args[@]}")
    if [ "${backend}" = "s3" ]; then
        export SNAPSHOT_S3_ACCESS_KEY="${s3_access_key}"
        export SNAPSHOT_S3_SECRET_KEY="${s3_secret_key}"
        export SNAPSHOT_S3_SECURITY_TOKEN="${s3_security_token}"
    fi
}
