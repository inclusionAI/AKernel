#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

for ((attempt = 0; attempt < 60; attempt++)); do
    address="$(
        ip -4 -o address show dev sandbox0 scope global 2>/dev/null |
            awk 'NR == 1 { split($4, value, "/"); print value[1] }'
    )"
    if [ -n "${address}" ]; then
        echo "sandbox0 is ready with IPv4 address ${address}"
        exit 0
    fi
    sleep 1
done

echo "timed out after 60s waiting for sandbox0 to have an IPv4 address" >&2
exit 1
