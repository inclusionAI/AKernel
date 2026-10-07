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
    node|standalone)
        case "${AKERNEL_FIRECRACKER_BACKEND:-kvm}" in
            kvm) ;;
            pvm)
                for config in PVM_GUEST X86_PIE X86_INTEL_MEMORY_PROTECTION_KEYS; do
                    if ! grep -qx "CONFIG_${config}=y" /opt/firecracker/kernel.config; then
                        echo "PVM requires a validated PVM guest bundle (missing CONFIG_${config}=y)" >&2
                        exit 1
                    fi
                done
                ;;
            *) echo "AKERNEL_FIRECRACKER_BACKEND must be kvm or pvm" >&2; exit 1 ;;
        esac
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
    *)
        echo "unsupported AKERNEL_ROLE: ${role}" >&2
        exit 1
        ;;
esac
