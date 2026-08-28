#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

yr_root="${1:?usage: $0 YR_ROOT}"
marker="${yr_root}/.akernel-s3-snapshot-capable"
config="${yr_root}/deploy/process/config.sh"
install="${yr_root}/functionsystem/deploy/install.sh"
agent="${yr_root}/functionsystem/bin/function_agent"

rm -f "${marker}"
if [[ -f "${config}" && -f "${install}" && -x "${agent}" ]] \
  && grep -Fq 'snapshot_s3_provider:' "${config}" \
  && grep -Fq -- '--snapshot_s3_provider="${SNAPSHOT_S3_PROVIDER:-}"' "${install}" \
  && grep -aFq 'snapshot_s3_provider' "${agent}" \
  && grep -aFq 'remote S3 snapshot exceeds the 5 GiB capability limit' "${agent}"; then
  touch "${marker}"
fi
