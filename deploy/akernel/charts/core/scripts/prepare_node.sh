#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

install -d -m 0755 /home/akernel/filestore

# sandboxd's bridge network requires the host br_netfilter module. The node
# container is privileged and mounts the host's matching module tree read-only.
if ! grep -q '^br_netfilter ' /proc/modules; then
    modprobe br_netfilter
fi

# Keep host scheduling and memory behavior consistent across CCE images. HCE
# disables scheduler autogrouping and enables THP globally; Ubuntu defaults are
# different and caused otherwise identical worker nodes to benchmark
# differently. These files are host-global even though this script runs from
# the privileged node pod.
if [ -w /proc/sys/kernel/sched_autogroup_enabled ]; then
    echo 0 > /proc/sys/kernel/sched_autogroup_enabled
fi
thp_enabled=/host-sys/kernel/mm/transparent_hugepage/enabled
if [ -w "$thp_enabled" ]; then
    echo always > "$thp_enabled"
fi
if [ -w /proc/sys/net/bridge/bridge-nf-call-iptables ]; then
    echo 1 > /proc/sys/net/bridge/bridge-nf-call-iptables
fi
if [ -w /proc/sys/net/bridge/bridge-nf-call-ip6tables ]; then
    echo 1 > /proc/sys/net/bridge/bridge-nf-call-ip6tables
fi

mkdir -p /etc/k8s_secrets
mount --bind /run/secrets/kubernetes.io/serviceaccount /etc/k8s_secrets
