#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
helper="${repo_root}/builder/scripts/yr_pause_resume_args.sh"
tmp_dir=$(mktemp -d)
trap 'rm -rf "${tmp_dir}"' EXIT
rrt_capability="${tmp_dir}/rrt-capable"
s3_capability="${tmp_dir}/s3-capable"
checkpoint_dir="${tmp_dir}/checkpoints"
touch "${rrt_capability}" "${s3_capability}"

source "${helper}"
configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" true "${s3_capability}"
expected="--snapshot_storage_backend datasystem --checkpoint_dir ${checkpoint_dir} --data_system_enable true"
[[ "${standalone_snapshot_args[*]}" == "${expected}" ]]

export AKERNEL_SNAPSHOT_STORAGE_BACKEND=s3
export AKERNEL_SNAPSHOT_S3_PROVIDER=generic
export AKERNEL_SNAPSHOT_S3_ENDPOINT=s3.private.example:9000
export AKERNEL_SNAPSHOT_S3_REGION=us-east-1
export AKERNEL_SNAPSHOT_S3_BUCKET=akernel-test
export AKERNEL_SNAPSHOT_S3_ACCESS_KEY=encrypted-access
export AKERNEL_SNAPSHOT_S3_SECRET_KEY=encrypted-secret
export AKERNEL_SNAPSHOT_S3_SECURITY_TOKEN=encrypted-token
export AKERNEL_SNAPSHOT_S3_USE_HTTPS=false
export AKERNEL_SNAPSHOT_S3_PATH_STYLE=true
configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" false "${s3_capability}"
[[ " ${snapshot_args[*]} " == *" --snapshot_s3_provider generic "* ]]
[[ " ${snapshot_args[*]} " == *" --snapshot_s3_endpoint s3.private.example:9000 "* ]]
[[ " ${snapshot_args[*]} " != *" --snapshot_s3_access_key "* ]]
[[ " ${snapshot_args[*]} " != *" --snapshot_s3_secret_key "* ]]
[[ " ${snapshot_args[*]} " != *" --snapshot_s3_security_token "* ]]
[[ "${SNAPSHOT_S3_ACCESS_KEY}" == "encrypted-access" ]]
[[ "${SNAPSHOT_S3_SECRET_KEY}" == "encrypted-secret" ]]
[[ "${SNAPSHOT_S3_SECURITY_TOKEN}" == "encrypted-token" ]]

export AKERNEL_SNAPSHOT_S3_PROVIDER=oss
export AKERNEL_SNAPSHOT_S3_PATH_STYLE=true
if configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" false "${s3_capability}" \
    >/dev/null 2>&1; then
    echo "OSS snapshot storage accepted path-style addressing" >&2
    exit 1
fi

export AKERNEL_SNAPSHOT_STORAGE_BACKEND=obs
if configure_snapshot_args "${rrt_capability}" "${checkpoint_dir}" false "${s3_capability}" \
    >/dev/null 2>&1; then
    echo "removed snapshot OBS backend was accepted" >&2
    exit 1
fi

echo "standalone snapshot wiring contract passed"
