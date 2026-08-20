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
    master|frontend|node|standalone)
        export AKERNEL_ROLE="${role}"
        ;;
    *)
        echo "unsupported AKERNEL_ROLE: ${role}; expected master, frontend, node, or standalone" >&2
        exit 1
        ;;
esac

case "${role}" in
    master|frontend)
        /usr/local/bin/ensure-component-cert
        exec /bin/bash /home/yuanrong/entrypoint.sh "$@"
        ;;
    node)
        /bin/bash /root/prepare_node.sh
        exec /usr/sbin/init "$@"
        ;;
    standalone)
        /usr/local/bin/ensure-component-cert
        exec /usr/sbin/init "$@"
        ;;
esac
