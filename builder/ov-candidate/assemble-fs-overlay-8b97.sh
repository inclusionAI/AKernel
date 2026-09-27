#!/usr/bin/env bash
# Assemble the fs-overlay-8b97 build context from the FORMAL Release
# output of functionsystem commit 8b97a668c70e92dc1b9c1babff1083bfb1a971a9.
#
# The formal output is FLAT (bin/ + lib/): regular files PLUS symlinks (38 in
# the 8b97 release). This script materializes the verified four-program
# assembly (same structure as the 8eb8d13a overlay):
#
#   mapping (explicit, per-item recomputable):
#     output/bin/function_proxy   -> fs-overlay-8b97/function-proxy/function_proxy
#     output/bin/function_master  -> fs-overlay-8b97/function-master/function_master
#     output/bin/runtime_manager  -> fs-overlay-8b97/runtime-manager/runtime_manager
#     output/bin/function_agent   -> fs-overlay-8b97/function-agent/function_agent
#     output/lib/ (ENTIRE set, files AND symlinks, modes preserved)
#                                -> fs-overlay-8b97/<each program dir>/lib/
#
#   Each program directory carries 1 binary + the complete library set. Other
#   formal outputs (yr CLI, iam_server, meta_service) are deliberately NOT
#   overlaid: the base image ships them and the verified 8eb overlay made the
#   same choice.
#
# Usage:
#   assemble-fs-overlay-8b97.sh <formal-output-dir> <dest-dir> <output-files.sha>
#
#   <output-files.sha>  release-evidence SHA inventory of the formal output's
#                       REGULAR files ("./bin/... ./lib/..." relative hashes);
#                       every regular file is re-verified before copying.
#                       Symlinks/modes are proven by the type inventory this
#                       script itself records and cross-checks (a plain
#                       find -type f list can never prove them).
#
# Safety: <dest-dir> must NOT exist (no overwrite of previous products); all
# temporaries come from mktemp. Exit code is real: 0 only on complete
# verification + assembly + cross-check.
set -euo pipefail

OUT_DIR="${1:?usage: assemble-fs-overlay-8b97.sh <formal-output-dir> <dest-dir> <output-files.sha>}"
DEST_DIR="${2:?missing dest-dir}"
OUT_SHA="${3:?missing output-files.sha}"

readonly COMMIT=8b97a668c70e92dc1b9c1babff1083bfb1a971a9
readonly PROGS=(
  "function-proxy:function_proxy"
  "function-master:function_master"
  "runtime-manager:runtime_manager"
  "function-agent:function_agent"
)

TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT

if [ ! -d "$OUT_DIR" ]; then
  echo "ERROR: formal output dir not found: $OUT_DIR" >&2
  exit 2
fi
if [ -e "$DEST_DIR" ]; then
  echo "ERROR: destination already exists, refusing to overwrite: $DEST_DIR" >&2
  exit 2
fi

# 1. Re-verify the formal output's REGULAR files against the release inventory.
( cd "$OUT_DIR" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum ) > "$TMPD/out-files.sha"
if ! cmp -s "$TMPD/out-files.sha" "$OUT_SHA"; then
  echo "ERROR: formal output regular files do not match the release inventory $OUT_SHA" >&2
  diff "$OUT_SHA" "$TMPD/out-files.sha" | head -20 >&2
  exit 3
fi
echo "formal output regular files verified against $OUT_SHA ($(wc -l < "$OUT_SHA") files)"

# 2. Record the COMPLETE output inventory: path/type/mode/link-target for
#    every entry (regular, symlink, directory). This is the only form that
#    can prove the 38 lib symlinks and the permission bits survived.
out_inventory() { ( cd "$1" && find . -mindepth 1 -printf '%y\t%m\t%p\t%l\n' | LC_ALL=C sort ); }
out_inventory "$OUT_DIR" > "$TMPD/out-types.txt"
echo "output type inventory: $(grep -c '^l' "$TMPD/out-types.txt") symlinks, $(grep -c '^f' "$TMPD/out-types.txt") regular files, $(grep -c '^d' "$TMPD/out-types.txt") dirs"

# 3. Materialize the four program trees into a FRESH destination (no merge,
#    no overwrite): whole-content copy keeps structure, modes, timestamps and
#    symlinks identical to the formal output.
mkdir -p "$DEST_DIR"
for pair in "${PROGS[@]}"; do
  dir="${pair%%:*}"
  bin="${pair##*:}"
  if [ ! -f "$OUT_DIR/bin/$bin" ]; then
    echo "ERROR: formal binary missing: $OUT_DIR/bin/$bin" >&2
    exit 4
  fi
  mkdir -p "$DEST_DIR/$dir"
  cp -a "$OUT_DIR/bin/$bin" "$DEST_DIR/$dir/$bin"
  cp -a "$OUT_DIR/lib/." "$DEST_DIR/$dir/lib/"
done

# 4. Cross-check every assembled tree against the formal output: the lib
#    subtree must match path/type/mode/link-target EXACTLY, and regular-file
#    SHA exactly; the four lib copies must be identical in shape.
lib_types() { ( cd "$1" && find . -mindepth 1 -printf '%y\t%m\t%p\t%l\n' | LC_ALL=C sort ); }
lib_files() { ( cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum ); }
lib_types "$OUT_DIR/lib" > "$TMPD/lib-types.txt"
lib_files "$OUT_DIR/lib" > "$TMPD/lib-files.sha"
for pair in "${PROGS[@]}"; do
  dir="${pair%%:*}"
  lib_types "$DEST_DIR/$dir/lib" > "$TMPD/copy-types.txt"
  lib_files "$DEST_DIR/$dir/lib" > "$TMPD/copy-files.sha"
  if ! cmp -s "$TMPD/copy-types.txt" "$TMPD/lib-types.txt"; then
    echo "ERROR: $dir/lib type/mode/link inventory differs from the formal lib set" >&2
    diff "$TMPD/lib-types.txt" "$TMPD/copy-types.txt" | head -10 >&2
    exit 5
  fi
  if ! cmp -s "$TMPD/copy-files.sha" "$TMPD/lib-files.sha"; then
    echo "ERROR: $dir/lib regular-file hashes differ from the formal lib set" >&2
    exit 5
  fi
  bins=$(find "$DEST_DIR/$dir" -maxdepth 1 -type f | wc -l)
  links=$(find "$DEST_DIR/$dir/lib" -type l | wc -l)
  files=$(find "$DEST_DIR/$dir/lib" -type f | wc -l)
  echo "$dir: 1 binary + $files lib files + $links lib symlinks (type+sha verified)"
done

# 5. Emit the per-item manifests of the assembled context next to it (same
#    SHA format as fs-overlay-inputs.sha, plus the full type inventory).
( cd "$DEST_DIR" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum ) > "$DEST_DIR-inputs.sha"
out_inventory "$DEST_DIR" > "$DEST_DIR-types.txt"
echo "assembled 4 program trees for commit $COMMIT"
echo "manifest: $DEST_DIR-inputs.sha ($(wc -l < "$DEST_DIR-inputs.sha") entries), $DEST_DIR-types.txt ($(wc -l < "$DEST_DIR-types.txt") entries)"
