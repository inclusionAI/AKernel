#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

CONFIG_PATH="${SANDBOXD_CONFIG_PATH:-/home/akernel/sandboxd/config.toml}"
SYSCTL_BIN="${SYSCTL_BIN:-/usr/sbin/sysctl}"

network_value() {
    local key="$1"

    awk -F= -v wanted="${key}" '
        /^[[:space:]]*\[plugin\.network\][[:space:]]*$/ {
            in_network = 1
            next
        }
        /^[[:space:]]*\[/ {
            in_network = 0
        }
        in_network {
            name = $1
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
            if (name != wanted) {
                next
            }
            value = substr($0, index($0, "=") + 1)
            sub(/[[:space:]]*#.*/, "", value)
            gsub(/^[[:space:]"]+|[[:space:]"]+$/, "", value)
            print value
            exit
        }
    ' "${CONFIG_PATH}"
}

if [[ ! -r "${CONFIG_PATH}" ]]; then
    echo "sandboxd network config is not readable: ${CONFIG_PATH}" >&2
    exit 1
fi

nat_backend="$(network_value nat_backend)"
enable_local_dnat="$(network_value enable_local_dnat)"
enable_network_acl="$(network_value enable_network_acl)"
nat_backend="${nat_backend:-iptables}"

if [[ ! -c /dev/net/tun ]]; then
    echo "/dev/net/tun is unavailable; load the host tun module before starting the AKernel node" >&2
    exit 1
fi

# Both NAT backends route packets between sandbox0 and the selected external
# interface. AKernel owns this network-namespace prerequisite.
"${SYSCTL_BIN}" -w net.ipv4.ip_forward=1

# The iptables ACL backend filters bridged sandbox traffic through host kernel
# netfilter hooks. Host provisioning must load br_netfilter; this hook only
# configures the node container's network namespace.
if [[ "${enable_network_acl,,}" == "true" && "${nat_backend}" == "iptables" ]]; then
    if [[ ! -e /proc/sys/net/bridge/bridge-nf-call-iptables ||
          ! -e /proc/sys/net/bridge/bridge-nf-call-ip6tables ]]; then
        echo "br_netfilter is unavailable; load it on the host before starting the AKernel node" >&2
        exit 1
    fi
    "${SYSCTL_BIN}" -w net.bridge.bridge-nf-call-iptables=1
    "${SYSCTL_BIN}" -w net.bridge.bridge-nf-call-ip6tables=1
fi

# bpfnat validates this setting when its local-DNAT path is enabled. Apply it
# before sandboxd starts so startup never races systemd's sysctl processing.
if [[ "${nat_backend}" == "bpfnat" && "${enable_local_dnat,,}" == "true" ]]; then
    "${SYSCTL_BIN}" -w net.ipv4.conf.all.rp_filter=0
fi
