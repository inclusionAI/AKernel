#!/usr/bin/env bash
# Remake /yr-runtime-rootfs.img with a replacement rrt-runtime, starting
# from the FIXED existing rootfs image so every other byte is inherited,
# never rebuilt from a moving apt state.
#
# Provenance of the first run (manual steps, recorded in
# ov20-assembly-independent-inputs.json):
#   old rootfs  aef38c4aba794559a01ec7efa21de286c321a67ae1b20217424b3c664ff5e2d8
#   new rootfs  2208e228c480282482a1a5726b42490ed15206dca4b23ab6d36cedca6f341325
#   only differing file: usr/local/bin/rrt-runtime
#               38844a96e7b641e1200afd907fa25611dd5afbae77c763bd812191cb7e6348bf
#            -> 19b28bae612959912243ff01c83256985a891eb1b87c685d86390672e4b2a615
# The first committed version of this script had a control-flow defect
# (set -e killed it on the EXPECTED diff exit 1) and never ran to
# completion; see rootfs-recipe-1ce5710-diff-control-flow-review.json.
# This version distinguishes the expected single-file delta from tool
# errors, verifies the delta by exact path+digest, unmounts via trap, and
# asserts the pinned erofs-utils version.
#
# Usage (on BareMetal1, as root):
#   OLD_ROOTFS_IMG=<path> NEW_RRT=<path> OUT=<dir> bash remake-rrt-rootfs.sh
set -euo pipefail

: "${OLD_ROOTFS_IMG:?path to the fixed old yr-runtime-rootfs.img}"
: "${NEW_RRT:?path to the replacement rrt-runtime}"
: "${OUT:?output directory}"

OLD_ROOTFS_SHA256=aef38c4aba794559a01ec7efa21de286c321a67ae1b20217424b3c664ff5e2d8
OLD_RRT_SHA256=38844a96e7b641e1200afd907fa25611dd5afbae77c763bd812191cb7e6348bf
NEW_RRT_SHA256=19b28bae612959912243ff01c83256985a891eb1b87c685d86390672e4b2a615
BASE_UBUNTU=ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
EROFS_UTILS_VERSION=1.7.1-1build2
RRT_PATH=./usr/local/bin/rrt-runtime

# ---- input identity: fail before any work if the bytes are not the
# pinned ones (tool errors and expected deltas are distinguished from
# wrong inputs from here on)
[ "$(sha256sum "$OLD_ROOTFS_IMG" | awk '{print $1}')" = "$OLD_ROOTFS_SHA256" ] \
  || { echo "FATAL: OLD_ROOTFS_IMG is not the pinned fixed rootfs" >&2; exit 1; }
[ "$(sha256sum "$NEW_RRT" | awk '{print $1}')" = "$NEW_RRT_SHA256" ] \
  || { echo "FATAL: NEW_RRT is not the pinned diagnostic artifact" >&2; exit 1; }

WORK="$OUT/work"
mkdir -p "$WORK/extract-old" "$WORK/staging" "$OUT/tools"
MNT_OLD="$WORK/mnt-old"; MNT_NEW="$WORK/mnt-new"
mkdir -p "$MNT_OLD" "$MNT_NEW"

cleanup() {
  umount "$MNT_OLD" 2>/dev/null || true
  umount "$MNT_NEW" 2>/dev/null || true
}
trap cleanup EXIT

# ---- 1. extract the fixed old rootfs read-only via the kernel erofs driver
modprobe erofs
mount -o loop,ro "$OLD_ROOTFS_IMG" "$MNT_OLD"
cp -a "$MNT_OLD/." "$WORK/extract-old/"
umount "$MNT_OLD"

# ---- 2. staging differs from the extraction in EXACTLY one file
cp -a "$WORK/extract-old/." "$WORK/staging/"
install -m 0755 "$NEW_RRT" "$WORK/staging/$RRT_PATH"

sums_of() { (cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | LC_ALL=C sort); }
sums_of "$WORK/extract-old" > "$WORK/old.sums"
sums_of "$WORK/staging"     > "$WORK/new.sums"

# The ONLY acceptable delta: the old digest line for $RRT_PATH removed and
# the new digest line for the same path added — verified by exact content,
# not by counting diff lines (a diff exit of 1 just means "differences
# found"; comparing it to anything else was the first version's defect).
removed=$(LC_ALL=C comm -23 "$WORK/old.sums" "$WORK/new.sums")
added=$(LC_ALL=C comm -13 "$WORK/old.sums" "$WORK/new.sums")
[ "$removed" = "$OLD_RRT_SHA256  $RRT_PATH" ] \
  || { echo "FATAL: unexpected removals beyond the pinned rrt-runtime:" >&2
       printf '%s\n' "$removed" >&2; exit 1; }
[ "$added" = "$NEW_RRT_SHA256  $RRT_PATH" ] \
  || { echo "FATAL: unexpected additions beyond the pinned rrt-runtime:" >&2
       printf '%s\n' "$added" >&2; exit 1; }

# ---- 3. remake with the pinned tool container, canonical flags
cat > "$OUT/tools/mkfs.sh" <<EOS
set -e
apt-get update -qq >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends erofs-utils >/dev/null 2>&1
ver=\$(dpkg-query -W -f='\${Version}' erofs-utils)
[ "\$ver" = "$EROFS_UTILS_VERSION" ] \\
  || { echo "FATAL: erofs-utils \$ver != pinned $EROFS_UTILS_VERSION" >&2; exit 1; }
echo "erofs-utils \$ver"
# -U pins the filesystem UUID for stability. NOTE: byte-for-byte
# reproduction across runs is NOT achievable — the erofs superblock embeds
# the build time and mkfs.erofs 1.7.1 has no timestamp override. The
# guarantee this script makes is CONTENT-level: the produced image's file
# checksum set equals the staging tree's and differs from the fixed old
# rootfs in exactly the one pinned rrt-runtime file (verified twice,
# pre-mkfs and post-mount). First committed run produced
# 79e3c4e14a43960856caf7429989b3ac1e8f97748b5d68e019348fbf553e669a;
# the earlier manual-step artifact was
# 2208e228c480282482a1a5726b42490ed15206dca4b23ab6d36cedca6f341325
# (content-identical; preserved with the first-round ov20 candidate).
mkfs.erofs -U a7dd32e3-c9c5-4751-a190-de15b8c96bd6 \
  -E noinline_data /out/yr-runtime-rootfs.img /staging
fsck.erofs /out/yr-runtime-rootfs.img
sha256sum /out/yr-runtime-rootfs.img
EOS
docker run --rm \
  -v "$WORK/staging":/staging:ro \
  -v "$OUT":/out \
  -v "$OUT/tools/mkfs.sh":/mkfs.sh:ro \
  "$BASE_UBUNTU" bash /mkfs.sh

# ---- 4. verify the produced image: mount it read-only and compare the
# content checksums against the staging tree (and re-prove the single-file
# delta versus the old extraction)
mount -o loop,ro "$OUT/yr-runtime-rootfs.img" "$MNT_NEW"
sums_of "$MNT_NEW" > "$WORK/mounted.sums"
umount "$MNT_NEW"

cmp "$WORK/new.sums" "$WORK/mounted.sums" \
  || { echo "FATAL: produced image content differs from staging tree" >&2; exit 1; }
removed2=$(LC_ALL=C comm -23 "$WORK/old.sums" "$WORK/mounted.sums")
added2=$(LC_ALL=C comm -13 "$WORK/old.sums" "$WORK/mounted.sums")
[ "$removed2" = "$OLD_RRT_SHA256  $RRT_PATH" ] && [ "$added2" = "$NEW_RRT_SHA256  $RRT_PATH" ] \
  || { echo "FATAL: produced image delta is not the single pinned rrt-runtime" >&2; exit 1; }

echo "verified: image at $OUT/yr-runtime-rootfs.img replaces exactly $RRT_PATH"
sha256sum "$OUT/yr-runtime-rootfs.img"
