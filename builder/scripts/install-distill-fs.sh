#!/bin/sh
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

# Install the distill-fs release for AKernel. Callers pass pins from
# builder/distill-fs-versions.env, never checksums downloaded with the archive.
set -eu

DISTILL_FS_RELEASE=$1
DISTILL_FS_AMD64_URL=$2
DISTILL_FS_AMD64_SHA256=$3
destination=$4

case "${TARGETARCH:-$(uname -m)}" in
    amd64|x86_64) ;;
    *) echo "distill-fs release supports linux/amd64 only" >&2; exit 1 ;;
esac
: "${DISTILL_FS_RELEASE:?distill-fs release is not pinned}"
: "${DISTILL_FS_AMD64_URL:?distill-fs release URL is not pinned}"
: "${DISTILL_FS_AMD64_SHA256:?publish and pin the distill-fs release before building}"
printf '%s\n' "$DISTILL_FS_AMD64_SHA256" | grep -Eq '^[0-9a-f]{64}$' || {
    echo "invalid distill-fs SHA-256 pin" >&2
    exit 1
}

# Keep the download separate from installed files. Image consumers copy only
# bin/ and share/ from this staging directory.
mkdir -p "$destination/download" "$destination/bin" "$destination/share/distill-fs"
archive="$destination/download/distill-fs.tar.gz"
curl -fSL --retry 5 --retry-delay 2 --retry-all-errors \
    "$DISTILL_FS_AMD64_URL" -o "$archive"
printf '%s  %s\n' "$DISTILL_FS_AMD64_SHA256" "$archive" | sha256sum -c -
tar -xzf "$archive" -C "$destination/download" \
    distill_fs manifest.json LICENSE NOTICE Cargo.lock
bundle="$destination/download"
jq -e --arg release "$DISTILL_FS_RELEASE" \
    '.component == "distill-fs" and .release_tag == $release and
     .version == ($release | ltrimstr("v")) and
     .repository == "inclusionAI/distill-fs" and
     .target == "x86_64-unknown-linux-musl" and
     (.source_revision | test("^[0-9a-f]{40}$")) and
     (.binary_sha256 | test("^[0-9a-f]{64}$"))' \
    "$bundle/manifest.json" >/dev/null
printf '%s  %s\n' "$(jq -r .binary_sha256 "$bundle/manifest.json")" \
    "$bundle/distill_fs" | sha256sum -c -
readelf -h "$bundle/distill_fs" >/dev/null
if readelf -l "$bundle/distill_fs" | grep -q INTERP ||
    readelf -d "$bundle/distill_fs" | grep -q NEEDED; then
    echo "distill-fs release must be a static executable" >&2
    exit 1
fi
chmod 0755 "$bundle/distill_fs"
test "$("$bundle/distill_fs" --version)" = "distill_fs ${DISTILL_FS_RELEASE#v}"
install -m 0755 "$bundle/distill_fs" "$destination/bin/distill_fs"
install -m 0644 "$bundle/manifest.json" "$bundle/LICENSE" \
    "$bundle/NOTICE" "$bundle/Cargo.lock" "$destination/share/distill-fs/"
