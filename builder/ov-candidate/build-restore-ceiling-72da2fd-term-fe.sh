#!/bin/bash
# build-restore-ceiling-72da2fd-term-fe.sh — frontend 候选(frontend 实际
# 基座 d307f769 = canon-df2e-yr-signalfix-term, SIGTERM)。单一变体版,
# 复用 build-restore-ceiling-72da2fd-images.sh 的验收结构; 三项收口修正:
# 终态仅 EXIT trap 首写一次 + host-exit 另存; verify 输出/rc 独立落盘;
# 加载检查经真实 wrapper(带 LD_LIBRARY_PATH)外部 timeout, rc 门控终态。
set -uo pipefail -o noclobber

PARENT=$1
RECIPE_DIR=$2
REL=/data/work/72da2ffd-release-20260928T072812Z/functionsystem/build-release/bin
ART=/data/work/72da2ffd-release-20260928T072812Z/audit-72da2ffd-rel-a1-20260928T072812Z/artifacts.json
BASE=akernel-bm1/all-in-one@sha256:d307f76990fdff27770904b9aef6d2cdf8358dfd26828b2ebbb03aca321758b0
TAG=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term-fe-m1
DF=restore-ceiling-72da2fd-term-fe-m1.Dockerfile

[ -e "$PARENT" ] && { echo "FATAL: $PARENT exists" >&2; exit 1; }
mkdir -p "$PARENT/out" "$PARENT/ctx"
FINAL="$PARENT/final.rc"
write_final() { [ -e "$FINAL" ] && return 0; echo "$1" > "$FINAL"; }
HOST_EXIT="$PARENT/host-exit.rc"
trap 'rc=$?; write_final "$rc"; echo "$rc" > "$HOST_EXIT"; date -u +%Y-%m-%dT%H:%M:%S.%3NZ > "$PARENT/finished-at.txt" 2>/dev/null || true' EXIT

cp "$RECIPE_DIR/$DF" "$PARENT/ctx/"
sha256sum "$PARENT/ctx/$DF" > "$PARENT/out/recipe-sha.txt"
declare -A FULLSHA
while read -r n s; do FULLSHA[$n]=$s; done < <(python3 - "$ART" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for e in d["executables"]:
    print(e["path"].rsplit("/", 1)[-1], e["sha256"])
PY
)
mkdir -p "$PARENT/ctx/rootfs/opt/akernel-scheduler/function-proxy" \
         "$PARENT/ctx/rootfs/opt/akernel-scheduler/function-master" \
         "$PARENT/ctx/rootfs/opt/akernel-scheduler/function-agent" \
         "$PARENT/ctx/rootfs/opt/akernel-scheduler/runtime-manager" \
         "$PARENT/ctx/rootfs/home/yuanrong/functionsystem/bin"
for pair in function_proxy:function-proxy function_master:function-master \
            function_agent:function-agent runtime_manager:runtime-manager; do
  PROG=${pair%%:*}; DIR=${pair##*:}
  got=$(sha256sum "$REL/$PROG" | awk '{print $1}')
  [ "$got" = "${FULLSHA[$PROG]}" ] || { echo "FATAL: $PROG sha" >&2; exit 1; }
  ionice -c 3 rsync -a --bwlimit=65536 "$REL/$PROG" "$PARENT/ctx/rootfs/opt/akernel-scheduler/$DIR/$PROG"
  ionice -c 3 rsync -a --bwlimit=65536 "$REL/$PROG" "$PARENT/ctx/rootfs/home/yuanrong/functionsystem/bin/$PROG.wheel"
done
find "$PARENT/ctx/rootfs" -type f -exec chmod 0755 {} \;
( cd "$PARENT/ctx/rootfs" && find . -type f | LC_ALL=C sort | xargs sha256sum ) > "$PARENT/out/ctx-inputs.sha"
BASE_CANON=$(docker image inspect "$BASE" --format "{{json .Config}}" | python3 -c "import json,sys,hashlib; print(hashlib.sha256(json.dumps(json.load(sys.stdin), sort_keys=True).encode()).hexdigest())")
echo "base_runtime_config_canonical=$BASE_CANON" > "$PARENT/out/base-config-canon.txt"

set +e
docker build -t "$TAG" -f "$PARENT/ctx/$DF" "$PARENT/ctx" > "$PARENT/out/build.log" 2>&1
BRC=$?
set -e
echo "$BRC" > "$PARENT/out/build.rc"
[ "$BRC" -eq 0 ] || exit "$BRC"
docker image inspect "$TAG" --format "inspect_id={{.Id}}" > "$PARENT/out/image-id.txt"
set +e
ionice -c 3 docker save "$TAG" | pv -L 64m | gzip > "$PARENT/out/term-fe.tar.gz"
SRC_RC=$?
set -e
echo "$SRC_RC" > "$PARENT/out/save.rc"
[ "$SRC_RC" -eq 0 ] || exit "$SRC_RC"
sha256sum "$PARENT/out/term-fe.tar.gz" > "$PARENT/out/image-tar.sha256"

BASE="$BASE" TAG="$TAG" ART="$ART" OUTDIR="$PARENT/out" \
timeout 300 python3 - > "$PARENT/out/verify.txt" 2> "$PARENT/out/verify.err" <<'PYV'
import hashlib, json, os, subprocess, sys, tarfile
BASE, TAG, ART = (os.environ[k] for k in ("BASE", "TAG", "ART"))
OUT = os.environ["OUTDIR"]
NAMES = {"function_proxy": "function-proxy",
         "function_master": "function-master",
         "function_agent": "function-agent",
         "runtime_manager": "runtime-manager"}
art = json.load(open(ART))
FULL = {e["path"].rsplit("/", 1)[-1]: e["sha256"]
        for e in art["executables"]}
EXPECT = {}
for n, d in NAMES.items():
    EXPECT[f"opt/akernel-scheduler/{d}/{n}"] = FULL[n]
    EXPECT[f"home/yuanrong/functionsystem/bin/{n}.wheel"] = FULL[n]
def inspect(ref):
    o = subprocess.run(["docker", "image", "inspect", ref],
                       capture_output=True, text=True, check=True).stdout
    return json.loads(o)[0]
errs = []
base, cand = inspect(BASE), inspect(TAG)
bl, cl = base["RootFS"]["Layers"], cand["RootFS"]["Layers"]
if cl[:len(bl)] != bl or len(cl) - len(bl) != 1:
    errs.append(f"diff_ids base={len(bl)} cand={len(cl)} prefix+1 mismatch")
bc = hashlib.sha256(json.dumps(base["Config"], sort_keys=True)
                    .encode()).hexdigest()
cc = hashlib.sha256(json.dumps(cand["Config"], sort_keys=True)
                    .encode()).hexdigest()
if cc != bc:
    errs.append("candidate runtime config != base canonical")
tar_path = os.path.join(OUT, "cand.tar")
with open(tar_path, "wb") as f:
    subprocess.run(["docker", "save", TAG], stdout=f, check=True)
t = tarfile.open(tar_path)
man = json.load(t.extractfile("manifest.json"))[0]
files = []
for L in man["Layers"][len(bl):]:
    lt = tarfile.open(fileobj=t.extractfile(L))
    for m in lt.getmembers():
        p = m.name.lstrip("./").rstrip("/")
        if m.isfile():
            files.append((p, m.mode,
                          hashlib.sha256(lt.extractfile(m).read()).hexdigest()))
        elif not m.isdir():
            errs.append(f"non-file non-dir entry: {p} {m.type}")
want = sorted([(k, 0o755, v) for k, v in EXPECT.items()])
if sorted(files) != want:
    errs.append(f"new regular files mismatch: "
                f"got {sorted(f[0] for f in files)}")
idx = json.load(t.extractfile("index.json"))
md = idx["manifests"][0]["digest"]
iid = cand["Id"]
if "sha256:" + md.split(":")[1] != iid and md != iid:
    errs.append(f"manifest digest {md} != inspect Id {iid}")
os.remove(tar_path)
print("inspect_id:", iid)
print(f"base diff_ids={len(bl)} candidate={len(cl)} (+{len(cl)-len(bl)})")
print("config canonical:", cc[:16],
      "EQUAL" if cc == bc else "MISMATCH")
print("new regular files:", len(files),
      "all 0755 release SHAs:", sorted(files) == want)
print("VERIFY_OK" if not errs else "VERIFY_FAILED: " + "; ".join(errs))
sys.exit(0 if not errs else 1)
PYV
VRC=$?
echo "$VRC" > "$PARENT/out/verify.rc"
[ "$VRC" -eq 0 ] || exit "$VRC"

# wrapper 语义有界加载检查(门控终态)
LOAD_FAIL=0
TS=$(date -u +%Y%m%dT%H%M%SZ)
for PROG in function_proxy function_master function_agent runtime_manager; do
  set +e
  timeout 60 docker run --name "rc72da-fe-$PROG-$TS" --network none --read-only \
    --entrypoint "/home/yuanrong/functionsystem/bin/$PROG" "$TAG" --help \
    > "$PARENT/out/load-$PROG.txt" 2>&1
  R1=$?
  set -e
  echo "$R1" > "$PARENT/out/load-$PROG.rc"
  docker inspect "rc72da-fe-$PROG-$TS" --format "State={{.State.Status}} ExitCode={{.State.ExitCode}}" > "$PARENT/out/load-$PROG.state.txt" 2>/dev/null || true
  [ "$R1" -eq 126 ] || [ "$R1" -eq 127 ] || [ "$R1" -eq 124 ] || continue
  LOAD_FAIL=1; echo "LOAD-FAIL $PROG rc=$R1" >> "$PARENT/out/load-failures.txt"
done
echo "$LOAD_FAIL" > "$PARENT/out/load-final.rc"
[ "$LOAD_FAIL" -eq 0 ] || exit 90

echo "FRONTEND CANDIDATE READY: $TAG"
exit 0
