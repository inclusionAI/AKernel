#!/bin/bash
# Build + verify + serial-import the per-role STOPSIGNAL(SIGTERM) variant.
# v2 (root review fixes, 2026-09-28):
#   - image config comparison reads the REAL OCI config blobs from the
#     containerd content store (/var/lib/containerd/io.containerd.content.
#     v1.content/blobs/sha256), not `docker image inspect` (which has no
#     History and reports RootFS.Layers, not diff_ids);
#   - runtime-config equality is STRICT: every config key must be equal,
#     only stop_signal may differ (no Image/Container-style whitelists);
#     rootfs diff_ids chain must be identical; history may only append one
#     empty layer;
#   - kind cluster name comes from `kind get clusters` (NOT the kubectl
#     context name);
#   - import is per-explicit-node SERIAL with ionice; the save pipe is
#     pv-limited to 64m. The kind-load READ path is NOT rate limited and
#     ionice is not guaranteed to be inherited by the daemon side — keep
#     imports serial, no concurrent large copies, wait for io PSI to
#     recover before deploying. (Registered boundary, do not backfill.)
# The d307f769 candidate already exists from recipe f35db282; v2 exists so
# the next reproduction matches the audited interface. Do not rebuild the
# same tag to fake a fresh build.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
BASE_TAG=akernel-bm1/all-in-one:canon-df2e-yr-signalfix
NEW_TAG=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term
REPO=akernel-bm1/all-in-one
CHART_RECORD=/root/akernel-bm1/chart-of-record.json
CONTENT_STORE=/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256
BUILD_LOG="$HERE/build.log"
exec > >(tee -a "$BUILD_LOG") 2>&1
echo "== build-stopsignal-term-image.sh v2 $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 1) target must not exist
docker image inspect "$NEW_TAG" >/dev/null 2>&1 && { echo "FATAL: target exists: $NEW_TAG" >&2; exit 10; }

# 2) base digest cross-check (resolved, never hand-typed)
BASE_REPO_DIGEST=$(docker image inspect "$BASE_TAG" --format '{{index .RepoDigests 0}}')
CHART_DIGEST=$(python3 -c "import json;print(json.load(open('$CHART_RECORD'))['image']['manifest_digest'])")
[ "${BASE_REPO_DIGEST#*@}" = "$CHART_DIGEST" ] || { echo "FATAL: base != chart-of-record" >&2; exit 11; }
awk -v b="$BASE_REPO_DIGEST" '$1=="FROM" && $2!=b {print "FATAL: Dockerfile FROM mismatch"; exit 1}' \
    "$HERE/stopsignal-term.Dockerfile" || exit 12

# 3) build (minimal context)
CTX=$(mktemp -d); cp "$HERE/stopsignal-term.Dockerfile" "$CTX/"
docker build --file "$CTX/stopsignal-term.Dockerfile" --tag "$NEW_TAG" "$CTX"; rm -rf "$CTX"

# 4) verification against REAL OCI config blobs ----------------------------
python3 - "$BASE_TAG" "$NEW_TAG" "$CONTENT_STORE" <<'PY'
import hashlib, json, os, subprocess, sys
base_tag, new_tag, store = sys.argv[1:4]
def config_blob_path(tag):
    cfg_digest = subprocess.run(["docker","image","inspect",tag,"--format","{{.Id}}"],
                                capture_output=True, text=True, check=True).stdout.strip()
    d = cfg_digest.split(":",1)[1]
    p = os.path.join(store, d)
    if not os.path.isfile(p):
        sys.exit(f"config blob not in content store: {p}")
    raw = open(p,"rb").read()
    if hashlib.sha256(raw).hexdigest() != d:
        sys.exit(f"content-store blob hash mismatch: {p}")
    return json.loads(raw), cfg_digest
base, bd = config_blob_path(base_tag)
new, nd = config_blob_path(new_tag)
errs = []
# strict runtime-config equality: only stop_signal may differ
bc, nc = base.get("config",{}), new.get("config",{})
keys = set(bc) | set(nc)
diffs = sorted(k for k in keys if bc.get(k) != nc.get(k))
if diffs != ["stop_signal"]:
    errs.append(f"config diffs beyond stop_signal: {diffs}")
elif bc.get("stop_signal") != "SIGRTMIN+3" or nc.get("stop_signal") != "SIGTERM":
    errs.append(f"stop_signal values wrong: {bc.get('stop_signal')} -> {nc.get('stop_signal')}")
# rootfs identical (diff_ids chain, from the real OCI config)
if base.get("rootfs") != new.get("rootfs"):
    errs.append("rootfs diff_ids chain differs — filesystem was touched")
# history: only one appended empty STOPSIGNAL layer
bh, nh = base.get("history",[]), new.get("history",[])
extra = nh[len(bh):]
if nh[:len(bh)] != bh or len(extra) != 1 or not extra[0].get("empty_layer") \
        or "STOPSIGNAL SIGTERM" not in json.dumps(extra[0]):
    errs.append(f"history delta unexpected: {json.dumps(extra)[:200]}")
# arch/os
for k in ("architecture","os","os.version","variant"):
    if base.get(k) != new.get(k):
        errs.append(f"{k} differs")
print(f"base config: {bd}\nnew  config: {nd}")
print("config key diffs:", diffs)
print("rootfs identical:", base.get("rootfs") == new.get("rootfs"))
if errs:
    print("VERIFY_FAILED:", *errs, sep="\n  "); sys.exit(20)
print("VERIFY_OK: runtime config identical except stop_signal; filesystem unchanged")
PY

# 5) save (ionice + pv 64m on the write path) ------------------------------
TAR="$HERE/${NEW_TAG##*:}.image.tar"
ionice -c 3 docker save "$NEW_TAG" | pv -L 64m > "$TAR" 2>"$HERE/save-pv.log"
sha256sum "$TAR" | tee "$HERE/image-tar.sha256"

# 6) serial per-explicit-node import (cluster name from kind, not context) --
CLUSTER=$(kind get clusters | head -1)
[ -n "$CLUSTER" ] || { echo "FATAL: no kind cluster" >&2; exit 30; }
for n in $(kubectl get nodes -o json | python3 -c \
        'import json,sys;print(" ".join(i["metadata"]["name"] for i in json.load(sys.stdin)["items"]))'); do
    echo "-- node $n $(date -u +%H:%M:%S)"
    ionice -c 3 kind load image-archive "$TAR" --name "$CLUSTER" --nodes "$n" \
        > "$HERE/kind-load-$n.log" 2>&1
    echo $? > "$HERE/kind-load-$n.rc"
    docker exec "$n" ctr -n k8s.io images ls 2>/dev/null | grep "${NEW_TAG##*:}" \
        > "$HERE/kind-load-verify-$n.txt" || true
done
echo "ALL_STEPS_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
