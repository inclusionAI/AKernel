#!/usr/bin/env bash
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    echo "AKERNEL_ENABLE_ASCEND requires a native linux/amd64 host" >&2
    exit 1
fi
for directory in /usr/local/Ascend/driver /usr/local/dcmi; do
    if [[ ! -d "${directory}" ]]; then
        echo "Required Ascend host directory is missing: ${directory}" >&2
        exit 1
    fi
done
if [[ ! -f /usr/local/bin/npu-smi || ! -x /usr/local/bin/npu-smi ]]; then
    echo "Required Ascend host executable is missing: /usr/local/bin/npu-smi" >&2
    exit 1
fi
