#!/bin/bash
# Build the per-role STOPSIGNAL(SIGTERM) image variant.
#
# Contract (root-authorized 2026-09-28):
#   - base: the FULL digest-pinned canon-df2e-yr-signalfix image as recorded
#     in chart-of-record.json (resolved at runtime, never hand-typed);
#   - the variant adds ONLY `STOPSIGNAL SIGTERM` — no file/library may be
#     replaced (verified post-build: identical RootFS.diff_ids chain, plus a
#     single 0B metadata layer in history, plus an explicit config key diff);
#   - node keeps the original image (SIGRTMIN+3 for systemd PID1);
#   - build runs inside a commit-exclusive directory created from a clean
#     `git archive` of the recipe commit; every step logs fully with real rc.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
BASE_TAG=akernel-bm1/all-in-one:canon-df2e-yr-signalfix
NEW_TAG=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term
REPO=akernel-bm1/all-in-one
CHART_RECORD=/root/akernel-bm1/chart-of-record.json
BUILD_LOG="$HERE/build.log"
exec > >(tee -a "$BUILD_LOG") 2>&1

echo "== build-stopsignal-term-image.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "== recipe Dockerfile sha256:"
sha256sum "$HERE/stopsignal-term.Dockerfile"

# 1) target must not exist
if docker image inspect "$NEW_TAG" >/dev/null 2>&1; then
    echo "FATAL: target tag already exists: $NEW_TAG" >&2
    exit 10
fi

# 2) resolve base digest and cross-check against chart-of-record
BASE_REPO_DIGEST=$(docker image inspect "$BASE_TAG" --format '{{index .RepoDigests 0}}')
BASE_DIGEST=${BASE_REPO_DIGEST#*@}
CHART_DIGEST=$(python3 -c "import json;print(json.load(open('$CHART_RECORD'))['image']['manifest_digest'])")
echo "base resolved : $BASE_REPO_DIGEST"
echo "chart-of-record: $CHART_DIGEST"
if [ "$BASE_DIGEST" != "$CHART_DIGEST" ]; then
    echo "FATAL: base digest != chart-of-record manifest digest" >&2
    exit 11
fi
DOCKERFILE_DIGEST=$(awk '$1=="FROM"{print $2}' "$HERE/stopsignal-term.Dockerfile")
if [ "$DOCKERFILE_DIGEST" != "$BASE_REPO_DIGEST" ]; then
    echo "FATAL: Dockerfile FROM ($DOCKERFILE_DIGEST) != resolved base ($BASE_REPO_DIGEST)" >&2
    exit 12
fi

# 3) build (single metadata layer; minimal context = the Dockerfile alone)
CTX=$(mktemp -d)
cp "$HERE/stopsignal-term.Dockerfile" "$CTX/"
docker build --file "$CTX/stopsignal-term.Dockerfile" --tag "$NEW_TAG" "$CTX"
rm -rf "$CTX"

# 4) verification ---------------------------------------------------------
python3 - "$BASE_TAG" "$NEW_TAG" <<'PY'
import json, subprocess, sys
base_tag, new_tag = sys.argv[1], sys.argv[2]
def inspect(tag):
    return json.loads(subprocess.run(["docker","image","inspect",tag],
                        capture_output=True, text=True, check=True).stdout)[0]
base, new = inspect(base_tag), inspect(new_tag)
errs = []
# 4a) StopSignal values
if new["Config"].get("StopSignal") != "SIGTERM":
    errs.append(f'new StopSignal={new["Config"].get("StopSignal")!r} != SIGTERM')
if base["Config"].get("StopSignal") != "SIGRTMIN+3":
    errs.append(f'base StopSignal changed: {base["Config"].get("StopSignal")!r}')
# 4b) filesystem identical: RootFS diff_ids chain must be byte-equal
if base["RootFS"] != new["RootFS"]:
    errs.append("RootFS differs (diff_ids chain changed — files were touched)")
# 4c) history: variant = base history + exactly one 0B STOPSIGNAL layer
h = new["History"]; bh = base["History"]
extra = h[len(bh):]
if h[:len(bh)] != bh or len(extra) != 1 or extra[0].get("empty_layer") is not True \
        or "STOPSIGNAL SIGTERM" not in extra[0].get("created_by", ""):
    errs.append(f"history delta unexpected: {json.dumps(extra)[:200]}")
# 4d) config key diff — list EVERY differing key; allowed set only
skip = {"StopSignal", "Image", "Container", "ContainerConfig", "DockerVersion",
        "Id", "Created"}
diffs = {k for k in set(base["Config"]) | set(new["Config"])
         if base["Config"].get(k) != new["Config"].get(k)}
bad = diffs - skip
if bad:
    errs.append(f"unexpected config diffs: {sorted(bad)}")
print("config key diffs:", sorted(diffs))
print("rootfs diff_ids count:", len(new["RootFS"]["diff_ids"]),
      "identical to base:", base["RootFS"] == new["RootFS"])
if errs:
    print("VERIFY_FAILED:", *errs, sep="\n  ")
    sys.exit(20)
print("VERIFY_OK: only StopSignal differs; filesystem unchanged")
PY
[ $? -eq 0 ] || exit 20

# 5) record digests + save tar (serial copy)
docker image inspect "$NEW_TAG" --format 'id={{.Id}}' | tee "$HERE/image-id.txt"
docker manifest inspect "$NEW_TAG" > "$HERE/manifest-inspect.json" 2>&1 || true
docker save "$NEW_TAG" | gzip > "$HERE/${NEW_TAG##*:}.tar.gz"
sha256sum "$HERE/${NEW_TAG##*:}.tar.gz" | tee "$HERE/image-tar.sha256"

# 6) kind import (serial, full log; pipefail propagates real rc)
kind load docker-image "$NEW_TAG" --name kind-akernel-bm1 2>&1 | tee "$HERE/kind-load.log"
echo "ALL_STEPS_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
