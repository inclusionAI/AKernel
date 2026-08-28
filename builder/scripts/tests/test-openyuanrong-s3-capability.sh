#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
detector="${root}/builder/scripts/detect-openyuanrong-s3-snapshot-capability.sh"
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
mkdir -p "${tmp}/deploy/process" "${tmp}/functionsystem/deploy" "${tmp}/functionsystem/bin"
printf '%s\n' 'snapshot_s3_provider:' >"${tmp}/deploy/process/config.sh"
printf '%s\n' '--snapshot_s3_provider="${SNAPSHOT_S3_PROVIDER:-}"' \
  >"${tmp}/functionsystem/deploy/install.sh"
printf '%s\n' 'snapshot_s3_provider' 'remote S3 snapshot exceeds the 5 GiB capability limit' \
  >"${tmp}/functionsystem/bin/function_agent"
chmod +x "${tmp}/functionsystem/bin/function_agent"

"${detector}" "${tmp}"
[[ -f "${tmp}/.akernel-s3-snapshot-capable" ]]

printf '%s\n' 'legacy function agent' >"${tmp}/functionsystem/bin/function_agent"
"${detector}" "${tmp}"
[[ ! -e "${tmp}/.akernel-s3-snapshot-capable" ]]

echo "openYuanRong S3 capability detection checks passed"
