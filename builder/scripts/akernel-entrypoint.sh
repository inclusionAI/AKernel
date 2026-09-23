#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

role="${AKERNEL_ROLE:-}"

if [ -z "${role}" ] && [ "$#" -gt 0 ]; then
    case "$1" in
        master|frontend|node|standalone)
            role="$1"
            shift
            ;;
    esac
fi

if [ -z "${role}" ]; then
    if [ "${AKS_LOCAL_MODE:-}" = "true" ]; then
        role="standalone"
    else
        echo "AKERNEL_ROLE is required: master, frontend, node, or standalone" >&2
        exit 1
    fi
fi

case "${role}" in
    master|frontend)
        exec /usr/local/bin/adx-service run
        ;;
    node)
        /bin/bash /root/prepare_node.sh
        exec /usr/sbin/init "$@"
        ;;
    standalone)
        systemctl enable adx.service >/dev/null
        exec /usr/sbin/init "$@"
        ;;
    *)
        echo "unsupported AKERNEL_ROLE: ${role}" >&2
        exit 1
        ;;
esac
