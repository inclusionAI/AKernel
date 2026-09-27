#!/usr/bin/env bash
# Remake /yr-runtime-rootfs.img with a replacement rrt-runtime, starting
# from the FIXED existing rootfs image so every other byte is inherited,
# never rebuilt from a moving apt state.
#
# Provenance of the run recorded in INPUT-SHA256.txt (ov20):
#   old rootfs  aef38c4aba794559a01ec7efa21de286c321a67ae1b20217424b3c664ff5e2d8
#               (extracted from akernel-runtime:canon-8c53bdd-rebuild1, image
#                sha256:2bfa19cf46615835d422f58f2355f4ebebe27aac7242e7fe88b4049c1dfc2c44;
#                identical bytes at /home/yuanrong/yr-runtime-rootfs.img inside
#                akernel-bm1/all-in-one:canon-8c53bdd-rebuild1 f34b68d26263)
#   new rootfs  2208e228c480282482a1a5726b42490ed15206dca4b23ab6d36cedca6f341325
#   only differing file: usr/local/bin/rrt-runtime
#               38844a96e7b641e1200afd907fa25611dd5afbae77c763bd812191cb7e6348bf
#            -> 19b28bae612959912243ff01c83256985a891eb1b87c685d86390672e4b2a615
#               (static musl diagnostic build, source 6640371feb9abecde918bad8bf23175c73209505
#                on scheduler/rrt-wait-diagnosis; NOT the platform gitlink pin)
#   tool        erofs-utils 1.7.1-1build2 inside ubuntu:24.04@sha256:008173c23f95b170204
#               355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3 (the same base digest the
#               canonical runtime.Dockerfile resolved on 2026-09-20), flags identical to
#               builder/runtime.Dockerfile: mkfs.erofs -E noinline_data + fsck.erofs
#
# Usage (on BareMetal1, as root):
#   OLD_ROOTFS_IMG=<path> NEW_RRT=<path> OUT=<dir> bash remake-rrt-rootfs.sh
set -euo pipefail

: "${OLD_ROOTFS_IMG:?path to the fixed old yr-runtime-rootfs.img}"
: "${NEW_RRT:?path to the replacement rrt-runtime (verify its SHA256 first)}"
: "${OUT:?output directory}"
BASE_UBUNTU=ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
WORK="$OUT/work"
mkdir -p "$WORK/extract-old" "$WORK/staging" "$OUT/tools"

# 1. extract the fixed old rootfs read-only via the kernel erofs driver
modprobe erofs
mkdir -p /mnt/rrt-old
mount -o loop,ro "$OLD_ROOTFS_IMG" /mnt/rrt-old
cp -a /mnt/rrt-old/. "$WORK/extract-old/"
umount /mnt/rrt-old

# 2. staging differs from the extraction in EXACTLY one file
cp -a "$WORK/extract-old/." "$WORK/staging/"
install -m 0755 "$NEW_RRT" "$WORK/staging/usr/local/bin/rrt-runtime"

# 3. prove the single-file delta before making the image
(cd "$WORK/extract-old" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sort) > "$WORK/old.sums"
(cd "$WORK/staging"     && find . -type f -print0 | sort -z | xargs -0 sha256sum | sort) > "$WORK/new.sums"
diff "$WORK/old.sums" "$WORK/new.sums" > "$WORK/content.delta"
[ "$(wc -l < "$WORK/content.delta")" -eq 4 ]  # exactly one replaced file (2 add + 2 del lines)

# 4. remake with the pinned tool container, canonical flags
cat > "$OUT/tools/mkfs.sh" <<'EOS'
set -e
apt-get update -qq >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends erofs-utils >/dev/null 2>&1
dpkg-query -W -f='${Version}\n' erofs-utils
mkfs.erofs -E noinline_data /out/yr-runtime-rootfs.img /staging
fsck.erofs /out/yr-runtime-rootfs.img
sha256sum /out/yr-runtime-rootfs.img
EOS
docker run --rm \
  -v "$WORK/staging":/staging:ro \
  -v "$OUT":/out \
  -v "$OUT/tools/mkfs.sh":/mkfs.sh:ro \
  "$BASE_UBUNTU" bash /mkfs.sh

# 5. verify the new image: mount it and re-diff against the old extraction
mkdir -p /mnt/rrt-new
mount -o loop,ro "$OUT/yr-runtime-rootfs.img" /mnt/rrt-new
(cd /mnt/rrt-new && find . -type f -print0 | xargs -0 sha256sum | sort) > "$WORK/mounted.sums"
umount /mnt/rrt-new
diff "$WORK/new.sums" "$WORK/mounted.sums"
echo "single-file rrt-runtime replacement verified; image at $OUT/yr-runtime-rootfs.img"
