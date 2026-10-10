#!/bin/sh
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

# Inspect the payload itself. Executing --version is not an architecture check
# on hosts with binfmt/QEMU support.
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 ELF_BINARY TARGETARCH" >&2
    exit 2
fi
binary=$1
target_arch=$2

fail() {
    printf '%s: %s\n' "$binary" "$1" >&2
    exit 1
}

case "$target_arch" in
    amd64|x86_64) expected_machine=62 ;;
    arm64|aarch64) expected_machine=183 ;;
    *) fail "unsupported target architecture: $target_arch" ;;
esac
[ -f "$binary" ] || fail "ELF binary is not a regular file"

# od is available in the build images and avoids an additional interpreter or
# binutils dependency. Unsigned decimal bytes make field splitting unambiguous.
header="$(LC_ALL=C od -An -v -t u1 -N 64 < "$binary")"
# shellcheck disable=SC2086
set -- $header
if [ "$#" -lt 4 ] || [ "$1 $2 $3 $4" != "127 69 76 70" ]; then
    fail "not an ELF binary"
fi
[ "$#" -eq 64 ] || fail "truncated ELF header: expected at least 64 bytes"
[ "$5" -eq 2 ] || fail "requires ELF64 (class 2)"
[ "$6" -eq 1 ] || fail "requires little-endian ELF (data 1)"
[ "$7" -eq 1 ] || fail "unsupported ELF identification version: $7"
[ "${21} ${22} ${23} ${24}" = "1 0 0 0" ] ||
    fail "unsupported ELF header version"
[ "${53} ${54}" = "64 0" ] || fail "invalid ELF64 header size"
case "${17} ${18}" in
    "2 0"|"3 0") ;;
    *) fail "ELF must be executable (ET_EXEC or ET_DYN)" ;;
esac
machine=$(( ${19} + ${20} * 256 ))
[ "$machine" -eq "$expected_machine" ] ||
    fail "ELF machine mismatch: expected $expected_machine for linux/$target_arch, found $machine"
