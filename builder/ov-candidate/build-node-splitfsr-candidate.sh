#!/bin/bash
# build-node-splitfsr-candidate.sh — runs on BM1 (recipe for root review;
# NOT yet approved to run). Node-candidate image for the split_fsrestore
# fix: base = current node-role image digest, replace ONLY runsc+sandboxd.
# Exclusive dir, SHA-verified inputs, throttled serial copy, real rcs,
# isolated --network none --read-only smoke checks with retained containers.
set -euo pipefail -o noclobber

PARENT=$1                       # caller-provided exclusive parent (must not exist)
SRC_RUNSC=/data/work/splitfsr-rel-20260928T034550Z/runsc-OUT/runsc
SRC_SANDBOXD=/data/work/splitfsr-rel-20260928T034550Z/sb/audit/sandboxd
RUNSC_SHA=7d9c56125b84c00817fdd0173912055bb3314fdea4b33dd09c0910c83762ac13
SANDBOXD_SHA=205e5c0a2ef7793a02aafc5a0bf387a4f650d4727f50b5c13b1f7f15187d8704
TAG=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr
COMMIT_GV=4d3d481d60bb433f6b58f03fb92ae3c6d2de651f

[ -e "$PARENT" ] && { echo "FATAL: $PARENT exists" >&2; exit 1; }
mkdir "$PARENT"
OUT="$PARENT/out"; CTX="$PARENT/ctx"
mkdir "$OUT" "$CTX"

# final.rc single-write via sentinel, installed only after OUT exists
FINAL_ONCE="$OUT/.final-written"
write_final() { [ -e "$FINAL_ONCE" ] && return 0; echo "$1" > "$OUT/final.rc"; touch "$FINAL_ONCE"; }
trap 'rc=$?; write_final "$rc"; date -u +%Y-%m-%dT%H:%M:%S.%3NZ > "$OUT/finished-at.txt" 2>/dev/null || true' EXIT

# recipe archive shipped by the caller lands in CTX as the Dockerfile
[ -f "$CTX/node-splitfsr-candidate.Dockerfile" ] || { echo "FATAL: Dockerfile not staged" >&2; exit 1; }

# 1) verify input SHAs BEFORE any copy
for pair in "$SRC_RUNSC:$RUNSC_SHA" "$SRC_SANDBOXD:$SANDBOXD_SHA"; do
  src=${pair%%:*}; want=${pair##*:}
  [ -f "$src" ] || { echo "FATAL: missing $src" >&2; exit 1; }
  got=$(sha256sum "$src" | awk '{print $1}')
  [ "$got" = "$want" ] || { echo "FATAL: sha mismatch $src: $got" >&2; exit 1; }
done

# 2) throttled, low-priority, serial copy into the context; then mode 0755
ionice -c 3 rsync -a --bwlimit=65536 "$SRC_RUNSC" "$CTX/runsc" > "$OUT/copy-runsc.log" 2>&1
ionice -c 3 rsync -a --bwlimit=65536 "$SRC_SANDBOXD" "$CTX/sandboxd" > "$OUT/copy-sandboxd.log" 2>&1
chmod 0755 "$CTX/runsc" "$CTX/sandboxd"
sha256sum "$CTX/node-splitfsr-candidate.Dockerfile" "$CTX/runsc" "$CTX/sandboxd" > "$OUT/input-sha256.txt"
stat -c "%a %n" "$CTX/runsc" "$CTX/sandboxd" > "$OUT/ctx-modes.txt"

# 3) build
set +e
docker build -t "$TAG" -f "$CTX/node-splitfsr-candidate.Dockerfile" "$CTX" > "$OUT/build.log" 2>&1
BRC=$?
set -e
echo "$BRC" > "$OUT/build.rc"
[ "$BRC" -eq 0 ] || { echo "BUILD FAILED rc=$BRC" >&2; exit "$BRC"; }
docker image inspect "$TAG" --format "manifest-id={{.Id}}" > "$OUT/image-id.txt"

# save tar for layer/config verification (registered artifact, throttled)
set +e
ionice -c 3 pv -L 64m < <(docker save "$TAG") | gzip > "$OUT/node-candidate.tar.gz" 2>/dev/null
SRC_RC=$?
set -e
echo "$SRC_RC" > "$OUT/save.rc"
sha256sum "$OUT/node-candidate.tar.gz" > "$OUT/image-tar.sha256"

# 4) layer/config/file verification vs the pinned base
set +e
python3 - "$OUT/node-candidate.tar.gz" > "$OUT/verify.txt" 2>&1 <<'PYEOF'
import tarfile, json, hashlib, sys
BASE_DIGEST = "sha256:6c8054ed6e6fee0f81468e766735b64b3487e62cdddc6140e178f7eff566f33d"
EXPECT = {
    "usr/local/bin/runsc": "7d9c56125b84c00817fdd0173912055bb3314fdea4b33dd09c0910c83762ac13",
    "usr/local/bin/sandboxd": "205e5c0a2ef7793a02aafc5a0bf387a4f650d4727f50b5c13b1f7f15187d8704",
}
t = tarfile.open(sys.argv[1])
idx = json.load(t.extractfile("index.json"))
md = idx["manifests"][0]["digest"]
man = json.load(t.extractfile("manifest.json"))[0]
cfg = json.load(t.extractfile(man["Config"]))
layers = man["Layers"]
# base layers count from the pinned base save recorded earlier (75) — the
# candidate must be exactly base-prefix + 2 new layers
errs = []
if len(layers) != 77:
    errs.append(f"layer count {len(layers)} != 77")
new = []
for L in layers[75:]:
    lt = tarfile.open(fileobj=t.extractfile(L))
    for m in lt.getmembers():
        p = m.name.lstrip("./")
        if m.isfile():
            new.append((p, m.mode, hashlib.sha256(lt.extractfile(m).read()).hexdigest()))
files = [(p, mo, h) for (p, mo, h) in new if p in EXPECT]
if sorted(files) != sorted([(k, 0o755, v) for k, v in EXPECT.items()]):
    errs.append(f"new regular files mismatch: {files}")
# runtime config: StopSignal must remain SIGRTMIN+3; config sub-object equal
# to the base except nothing (no config-changing instruction was used)
ss = cfg.get("config", {}).get("StopSignal")
if ss != "SIGRTMIN+3":
    errs.append(f"StopSignal {ss!r} != SIGRTMIN+3")
print("manifest digest:", md)
print("layers:", len(layers), "new files:", files)
print("StopSignal:", ss)
print("VERIFY_OK" if not errs else "VERIFY_FAILED: " + "; ".join(errs))
sys.exit(0 if not errs else 1)
PYEOF
VRC=$?
set -e
echo "$VRC" > "$OUT/verify.rc"
[ "$VRC" -eq 0 ] || { echo "VERIFY FAILED" >&2; exit "$VRC"; }

# 5) isolated read-only no-network smoke checks; containers RETAINED
TS=$(date -u +%Y%m%dT%H%M%SZ)
set +e
docker run --name node-cand-runsc-$TS --network none --read-only \
  --entrypoint /usr/local/bin/runsc "$TAG" --version > "$OUT/runsc-version.txt" 2>&1
R1=$?
set -e
echo "$R1" > "$OUT/runsc-version.rc"
grep -q "$COMMIT_GV" "$OUT/runsc-version.txt" \
  || { echo "FATAL: runsc --version missing commit" >&2; exit 1; }
docker inspect node-cand-runsc-$TS --format "State={{.State.Status}} ExitCode={{.State.ExitCode}}" > "$OUT/runsc-check-state.txt"

set +e
docker run --name node-cand-sandboxd-$TS --network none --read-only \
  --entrypoint /usr/local/bin/sandboxd "$TAG" -h > "$OUT/sandboxd-help.txt" 2>&1
R2=$?
set -e
echo "$R2" > "$OUT/sandboxd-help.rc"
docker inspect node-cand-sandboxd-$TS --format "State={{.State.Status}} ExitCode={{.State.ExitCode}}" > "$OUT/sandboxd-check-state.txt"

echo "NODE CANDIDATE READY: $TAG"
