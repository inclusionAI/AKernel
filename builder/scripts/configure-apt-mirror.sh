#!/bin/sh
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

# Replace official repository URIs only. Keep suites, signing keys and policy.
set -eu

ubuntu=${AKERNEL_APT_UBUNTU_MIRROR:-}
ports=${AKERNEL_APT_UBUNTU_PORTS_MIRROR:-}
debian=${AKERNEL_APT_DEBIAN_MIRROR:-}
security=${AKERNEL_APT_DEBIAN_SECURITY_MIRROR:-}
[ -n "${ubuntu}${ports}${debian}${security}" ] || exit 0
for mirror in "${ubuntu}" "${ports}" "${debian}" "${security}"; do
    [ -n "${mirror}" ] || continue
    case "${mirror}" in
        *[!a-zA-Z0-9._~%+:/-]*)
            echo "APT mirror must be a public HTTP(S) repository URL without credentials or query parameters" >&2
            exit 1 ;;
    esac
    printf '%s\n' "${mirror}" | grep -Eq '^https?://[a-zA-Z0-9._-]+(:[0-9]+)?(/[a-zA-Z0-9._~%+:/-]*)?$' || {
        echo "APT mirror must be a public HTTP(S) repository URL without credentials or query parameters" >&2
        exit 1
    }
done

# The optional root allows source-file fixtures without editing the host.
root=${1:-/}
root=${root%/}
# shellcheck source=/dev/null
. "${root}/etc/os-release"
case "${ID}" in
    ubuntu)
        case "$(dpkg --print-architecture)" in
            amd64) mirror=${ubuntu} ;;
            arm64) mirror=${ports} ;;
            *) echo "unsupported Ubuntu mirror architecture" >&2; exit 1 ;;
        esac
        [ -n "${mirror}" ] || exit 0
        expression="s#https?://([a-z0-9-]+\.)?archive\.ubuntu\.com/ubuntu([/[:space:]]|$)#${mirror%/}\2#g; s#https?://security\.ubuntu\.com/ubuntu([/[:space:]]|$)#${mirror%/}\1#g; s#https?://ports\.ubuntu\.com/ubuntu-ports([/[:space:]]|$)#${mirror%/}\1#g"
        ;;
    debian)
        expression=""
        if [ -n "${debian}" ]; then
            expression="s#https?://deb\.debian\.org/debian([/[:space:]]|$)#${debian%/}\1#g; "
        fi
        if [ -n "${security}" ]; then
            expression="${expression}s#https?://(security\.debian\.org|deb\.debian\.org)/debian-security([/[:space:]]|$)#${security%/}\2#g; s#https?://security\.debian\.org/debian([/[:space:]]|$)#${security%/}\1#g"
        fi
        [ -n "${expression}" ] || exit 0
        ;;
    *) echo "APT mirrors support Ubuntu and Debian base images only" >&2; exit 1 ;;
esac

for source in "${root}/etc/apt/sources.list" \
    "${root}/etc/apt/sources.list.d/"*.list \
    "${root}/etc/apt/sources.list.d/"*.sources; do
    [ -f "${source}" ] || continue
    temporary=$(mktemp "${source}.XXXXXX")
    trap 'rm -f "${temporary}"' EXIT HUP INT TERM
    sed -E "${expression}" "${source}" > "${temporary}"
    cat "${temporary}" > "${source}"
    rm -f "${temporary}"
    trap - EXIT HUP INT TERM
done
