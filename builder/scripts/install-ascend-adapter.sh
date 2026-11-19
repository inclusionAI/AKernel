#!/bin/sh
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -eu

url=$1
sha256=$2
mount_profile=$3
destination=$4
script_dir=$(dirname "$0")
bundle=ascend-oci-adapter_v0.1.0-rc.2_linux_amd64

workdir=$(mktemp -d)
trap 'rm -rf -- "$workdir"' EXIT
archive="$workdir/adapter.tar.gz"
curl -fSL --retry 10 --retry-delay 2 --retry-all-errors "$url" -o "$archive"
printf '%s  %s\n' "$sha256" "$archive" | sha256sum -c -
tar --no-same-owner -xzf "$archive" -C "$workdir" \
    "$bundle/bin/ascend-oci-adapter" \
    "$bundle/licenses/ascend-oci-adapter/LICENSE" \
    "$bundle/licenses/mind-cluster/LICENSE" \
    "$bundle/licenses/mind-cluster/Third_Party_Open_Source_Software_Notice.md"

source="$workdir/$bundle"
sh "$script_dir/verify-elf-arch.sh" "$source/bin/ascend-oci-adapter" amd64
mkdir -p "$destination/usr/local/libexec/akernel" "$destination/etc/akernel/ascend" \
    "$destination/opt/akernel/licenses/ascend-oci-adapter" \
    "$destination/opt/akernel/licenses/mind-cluster"
install -m 0755 "$source/bin/ascend-oci-adapter" \
    "$destination/usr/local/libexec/akernel/ascend-oci-adapter"
install -m 0644 "$mount_profile" "$destination/etc/akernel/ascend/mounts.json"
install -m 0644 "$source/licenses/ascend-oci-adapter/LICENSE" \
    "$destination/opt/akernel/licenses/ascend-oci-adapter/"
install -m 0644 "$source/licenses/mind-cluster/LICENSE" \
    "$source/licenses/mind-cluster/Third_Party_Open_Source_Software_Notice.md" \
    "$destination/opt/akernel/licenses/mind-cluster/"
