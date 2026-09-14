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
        /usr/local/bin/ensure-component-cert
        # The shared image uses systemd's stop signal for node/standalone.
        # Translate it for the CLI and keep PID 1 alive until cleanup finishes.
        child_pid=""
        stop_requested=false
        stop_control_plane() {
            stop_requested=true
            if [ -n "$child_pid" ]; then
                kill -TERM "$child_pid" 2>/dev/null || true
            fi
        }
        trap stop_control_plane TERM INT RTMIN+3
        /bin/bash /home/yuanrong/entrypoint.sh "$@" &
        child_pid=$!
        if [ "$stop_requested" = true ]; then
            stop_control_plane
        fi
        # A trapped signal interrupts wait before the child has exited.
        status=0
        while true; do
            if wait "$child_pid"; then
                status=0
                break
            else
                status=$?
            fi
            if ! kill -0 "$child_pid" 2>/dev/null; then
                break
            fi
        done
        exit "$status"
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
