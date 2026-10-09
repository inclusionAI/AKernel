#!/usr/bin/env bash
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
# Run in a disposable privileged container with its own network namespace.
set -euo pipefail

[[ "$(uname -m)" == aarch64 ]]
test -c /dev/net/tun
test -c /dev/loop-control
test -c /dev/fuse
test -f /sys/fs/cgroup/cgroup.controllers
for controller in cpu io memory pids; do
    grep -qw "$controller" /sys/fs/cgroup/cgroup.controllers
done
test -e /proc/sys/net/bridge/bridge-nf-call-iptables
test -e /proc/sys/net/bridge/bridge-nf-call-ip6tables

probe_dir="$(mktemp -d /home/akernel/.orbstack-preflight.XXXXXX)"
cgroup_probe=""
ext4_mounted=false
lower_mounted=false
overlay_mounted=false
tap_created=false
veth_created=false
ipset_created=false
ipv4_chain_created=false
ipv6_chain_created=false

cleanup() {
    local status=$?
    local failed=false
    trap - EXIT
    set +e
    if [[ "${overlay_mounted}" == true ]]; then
        if umount "${probe_dir}/overlay"; then overlay_mounted=false; else failed=true; fi
    fi
    if [[ "${overlay_mounted}" == false && "${lower_mounted}" == true ]]; then
        if umount "${probe_dir}/lower"; then lower_mounted=false; else failed=true; fi
    fi
    if [[ "${overlay_mounted}" == false && "${ext4_mounted}" == true ]]; then
        if umount "${probe_dir}/ext4"; then ext4_mounted=false; else failed=true; fi
    fi
    if [[ "${tap_created}" == true ]]; then ip link del akp0 || failed=true; fi
    if [[ "${veth_created}" == true ]]; then ip link del akpv0 || failed=true; fi
    if [[ "${ipset_created}" == true ]]; then ipset destroy AKPSET || failed=true; fi
    if [[ "${ipv4_chain_created}" == true ]]; then
        iptables -t filter -F AKP4 || failed=true
        iptables -t filter -X AKP4 || failed=true
    fi
    if [[ "${ipv6_chain_created}" == true ]]; then
        ip6tables -t filter -F AKP6 || failed=true
        ip6tables -t filter -X AKP6 || failed=true
    fi
    if [[ -n "${cgroup_probe}" ]]; then rmdir "${cgroup_probe}" || failed=true; fi
    if [[ "${ext4_mounted}" == false && "${lower_mounted}" == false && "${overlay_mounted}" == false ]]; then
        rm -f "${probe_dir}/filestore.img" || failed=true
        local directory
        for directory in ext4 lower overlay; do
            if [[ -d "${probe_dir}/${directory}" ]]; then
                rmdir "${probe_dir}/${directory}" || failed=true
            fi
        done
        rmdir "${probe_dir}" || failed=true
    fi
    if [[ "${failed}" == true ]]; then
        echo "OrbStack preflight cleanup failed; inspect ${probe_dir}" >&2
        if [[ "${status}" == 0 ]]; then status=1; fi
    fi
    exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cgroup_probe="$(mktemp -d /sys/fs/cgroup/akernel-preflight.XXXXXX)"
ip tuntap add dev akp0 mode tap
tap_created=true
ip link set akp0 up
ip link add akpv0 type veth peer name akpv1
veth_created=true
iptables -t nat -S >/dev/null
iptables -t filter -N AKP4
ipv4_chain_created=true
iptables -t filter -A AKP4 -m physdev --physdev-in akp0 ! -s 192.0.2.1 -j DROP
iptables -t filter -A AKP4 -m conntrack --ctstate NEW -j CONNMARK --set-xmark 0x1/0x1
iptables -t filter -A AKP4 -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -m connmark --mark 0x1/0x1 -j RETURN
ip6tables -t filter -N AKP6
ipv6_chain_created=true
ip6tables -t filter -A AKP6 -m physdev --physdev-is-bridged -j RETURN
ipset create AKPSET hash:ip family inet timeout 1 maxelem 1
ipset_created=true

mkdir "${probe_dir}/ext4" "${probe_dir}/lower" "${probe_dir}/overlay"
truncate -s 32M "${probe_dir}/filestore.img"
mkfs.ext4 -q -F "${probe_dir}/filestore.img"
mount -o loop "${probe_dir}/filestore.img" "${probe_dir}/ext4"
ext4_mounted=true
printf '%s\n' ok > "${probe_dir}/ext4/probe"
test "$(cat "${probe_dir}/ext4/probe")" = ok

if [[ "${AKERNEL_ENABLE_RUNC:-false}" == true ]]; then
    for filesystem in overlay erofs; do
        awk -v fs="${filesystem}" '$NF == fs { found=1 } END { exit !found }' /proc/filesystems
    done
    /usr/local/bin/runc --version >/dev/null
    test -x /usr/local/bin/runc-shim
    mount -t erofs -o loop,ro /home/yuanrong/yr-runtime-rootfs.img "${probe_dir}/lower"
    lower_mounted=true
    test -x "${probe_dir}/lower/usr/local/bin/rrt-runtime"
    mkdir "${probe_dir}/ext4/upper" "${probe_dir}/ext4/work"
    mount -t overlay overlay \
        -o "lowerdir=${probe_dir}/lower,upperdir=${probe_dir}/ext4/upper,workdir=${probe_dir}/ext4/work" \
        "${probe_dir}/overlay"
    overlay_mounted=true
    printf '%s\n' overlay-ok > "${probe_dir}/overlay/akernel-preflight"
    test "$(cat "${probe_dir}/overlay/akernel-preflight")" = overlay-ok
    test -x "${probe_dir}/overlay/usr/local/bin/rrt-runtime"
fi

echo "OrbStack standalone host capabilities passed"
