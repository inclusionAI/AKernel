#!/bin/bash
# build-restore-ceiling-sandboxd.sh — node v4(=v3+新sandboxd 1cf3e1fa)。
# 验收结构沿用 v2 已验配方: 基座层前缀+恰1新层、config与基座整体相等、
# 新层恰一个常规文件 sandboxd 0755/SHA; 终态一次写入+host-exit另存。
set -uo pipefail -o noclobber
PARENT=$1
RECIPE_DIR=$2
SB=$(ls /data/work/sbd-1cf3e1fa-build-*/sandboxd 2>/dev/null | head -1)
SB_SHA=f2245b42a1ca3e124e70c1581012a15be259b8a70d90efa221a7036770f6be87
BASE=akernel-bm1/all-in-one@sha256:709a40a543e4c2164c8e663ab478a1ff0451b962ddc090275355da743df0524a
TAG=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v4
DF=restore-ceiling-sandboxd-1cf3e1fa.Dockerfile

[ -e "$PARENT" ] && { echo "FATAL: $PARENT exists" >&2; exit 1; }
mkdir -p "$PARENT/out" "$PARENT/ctx"
FINAL="$PARENT/final.rc"
write_final() { [ -e "$FINAL" ] && return 0; echo "$1" > "$FINAL"; }
trap 'rc=$?; write_final "$rc"; echo "$rc" > "$PARENT/host-exit.rc"; date -u +%Y-%m-%dT%H:%M:%S.%3NZ > "$PARENT/finished-at.txt"' EXIT

cp "$RECIPE_DIR/$DF" "$PARENT/ctx/"
[ -f "$SB" ] || { echo "FATAL: missing sandboxd binary" >&2; exit 1; }
got=$(sha256sum "$SB" | awk '{print $1}')
[ "$got" = "$SB_SHA" ] || { echo "FATAL: sandboxd sha mismatch" >&2; exit 1; }
ionice -c 3 rsync -a --bwlimit=65536 "$SB" "$PARENT/ctx/sandboxd"
chmod 0755 "$PARENT/ctx/sandboxd"
sha256sum "$PARENT/ctx/$DF" "$PARENT/ctx/sandboxd" > "$PARENT/out/input-sha256.txt"
stat -c "%a %n" "$PARENT/ctx/sandboxd" > "$PARENT/out/ctx-modes.txt"

BASE_CANON=$(docker image inspect "$BASE" --format "{{json .Config}}" | python3 -c "import json,sys,hashlib; print(hashlib.sha256(json.dumps(json.load(sys.stdin), sort_keys=True).encode()).hexdigest())")
echo "base_runtime_config_canonical=$BASE_CANON" > "$PARENT/out/base-config-canon.txt"

set +e
docker build -t "$TAG" -f "$PARENT/ctx/$DF" "$PARENT/ctx" > "$PARENT/out/build.log" 2>&1
BRC=$?
set -e
echo "$BRC" > "$PARENT/out/build.rc"
[ "$BRC" -eq 0 ] || exit "$BRC"
docker image inspect "$TAG" --format "inspect_id={{.Id}}" > "$PARENT/out/image-id.txt"

BASE="$BASE" TAG="$TAG" SB_SHA="$SB_SHA" OUTDIR="$PARENT/out" \
timeout 300 python3 - > "$PARENT/out/verify.txt" 2> "$PARENT/out/verify.err" <<'PY'
import hashlib, json, os, subprocess, tarfile, sys
BASE=os.environ["BASE"]; TAG=os.environ["TAG"]
SB_SHA=os.environ["SB_SHA"]; OUT=os.environ["OUTDIR"]
def ins(r):
    return json.loads(subprocess.run(
        ["docker","image","inspect",r],
        capture_output=True,text=True,check=True).stdout)[0]
errs=[]
base,cand=ins(BASE),ins(TAG)
bl,cl=base["RootFS"]["Layers"],cand["RootFS"]["Layers"]
if cl[:len(bl)]!=bl or len(cl)-len(bl)!=1:
    errs.append(f"layers base={len(bl)} cand={len(cl)}")
bc=hashlib.sha256(json.dumps(base["Config"],sort_keys=True).encode()).hexdigest()
cc=hashlib.sha256(json.dumps(cand["Config"],sort_keys=True).encode()).hexdigest()
if cc!=bc:
    errs.append("config != base canonical")
tp=os.path.join(OUT,"cand.tar")
with open(tp,"wb") as f:
    subprocess.run(["docker","save",TAG],stdout=f,check=True)
t=tarfile.open(tp)
man=json.load(t.extractfile("manifest.json"))[0]
files=[]
for L in man["Layers"][len(bl):]:
    lt=tarfile.open(fileobj=t.extractfile(L))
    for m in lt.getmembers():
        p=m.name.lstrip("./").rstrip("/")
        if m.isfile():
            files.append((p,m.mode,
                hashlib.sha256(lt.extractfile(m).read()).hexdigest()))
        elif not m.isdir():
            errs.append(f"non-file {p}")
if sorted(files)!=[("usr/local/bin/sandboxd",0o755,SB_SHA)]:
    errs.append(f"files={sorted(files)}")
os.remove(tp)
print("inspect_id:",cand["Id"])
print(f"layers {len(bl)}->{len(cl)} (+1)")
print("config:","EQUAL" if cc==bc else "MISMATCH")
print("VERIFY_OK" if not errs else "VERIFY_FAILED: "+"; ".join(errs))
sys.exit(0 if not errs else 1)
PY
VRC=$?
echo "$VRC" > "$PARENT/out/verify.rc"
[ "$VRC" -eq 0 ] || exit "$VRC"

# 有限加载冒烟: sandboxd -h(rc=0 为真实help, v2配方先例)
TS=$(date -u +%Y%m%dT%H%M%SZ)
set +e
docker run --rm --network none "$TAG" /usr/local/bin/sandboxd -h \
  > "$PARENT/out/sandboxd-help.txt" 2>&1
R1=$?
set -e
echo "$R1" > "$PARENT/out/sandboxd-help.rc"
[ "$R1" -eq 0 ] || { echo "FATAL: sandboxd -h rc=$R1" >&2; exit "$R1"; }

echo "NODE V4 READY: $TAG"
exit 0
