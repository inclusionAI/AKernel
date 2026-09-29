#!/bin/bash
# build-e555d534-role-images.sh — 三角色镜像(e555d534程序+node含v4 sandboxd+v5 runsc)
# node-v6 = v5(已含sandboxd f2245b42+runsc ebb3c7a8) + e555d534程序8文件
# term-m3 = term-m2基座 + e555d534程序8文件
# term-fe-m2 = term-fe-m1基座 + e555d534程序8文件
set -uo pipefail -o noclobber
PARENT=$1; RECIPE_DIR=$2
REL=/data/work/e555d534-release-20260929T105534Z/functionsystem/build-release/bin
ART=/data/work/e555d534-release-20260929T105534Z/audit-e555d534-rel-a2-20260929T105534Z/artifacts.json
declare -A BASE TAG
BASE[node]=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v5
TAG[node]=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v6
BASE[master]=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term-m2
TAG[master]=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term-m3
BASE[frontend]=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term-fe-m1
TAG[frontend]=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term-fe-m2

[ -e "$PARENT" ] && { echo "FATAL: $PARENT exists" >&2; exit 1; }
mkdir "$PARENT"
FINAL="$PARENT/final.rc"
write_final() { [ -e "$FINAL" ] && return 0; echo "$1" > "$FINAL"; }
trap 'rc=$?; write_final "$rc"; echo "$rc" > "$PARENT/host-exit.rc"; date -u +%Y-%m-%dT%H:%M:%S.%3NZ > "$PARENT/finished-at.txt"' EXIT

declare -A FULLSHA
while read -r n s; do FULLSHA[$n]=$s; done < <(python3 - "$ART" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for e in d["executables"]:
    print(e["path"].rsplit("/", 1)[-1], e["sha256"])
PY
)

build_role() {  # $1=role
  local V=$1
  local OUT="$PARENT/$V" CTX="$PARENT/$V/ctx"
  mkdir -p "$OUT" "$CTX"
  cat > "$CTX/Dockerfile" <<DF
FROM ${BASE[$V]}
# e555d534 four-program refresh (recovery-baseline operationId). Only the
# four scheduler programs at their real /opt paths and wheel copies
# change; everything else inherited unchanged. Mode 0755 from context.
COPY rootfs/ /
DF
  mkdir -p "$CTX/rootfs/opt/akernel-scheduler/function-proxy" \
           "$CTX/rootfs/opt/akernel-scheduler/function-master" \
           "$CTX/rootfs/opt/akernel-scheduler/function-agent" \
           "$CTX/rootfs/opt/akernel-scheduler/runtime-manager" \
           "$CTX/rootfs/home/yuanrong/functionsystem/bin"
  for pair in function_proxy:function-proxy function_master:function-master \
              function_agent:function-agent runtime_manager:runtime-manager; do
    PROG=${pair%%:*}; DIR=${pair##*:}
    got=$(sha256sum "$REL/$PROG" | awk '{print $1}')
    [ "$got" = "${FULLSHA[$PROG]}" ] || { echo "FATAL: $PROG sha" >&2; exit 1; }
    ionice -c 3 rsync -a --bwlimit=65536 "$REL/$PROG" "$CTX/rootfs/opt/akernel-scheduler/$DIR/$PROG"
    ionice -c 3 rsync -a --bwlimit=65536 "$REL/$PROG" "$CTX/rootfs/home/yuanrong/functionsystem/bin/$PROG.wheel"
  done
  find "$CTX/rootfs" -type f -exec chmod 0755 {} \;
  ( cd "$CTX/rootfs" && find . -type f | LC_ALL=C sort | xargs sha256sum ) > "$OUT/ctx-inputs.sha"
  set +e
  docker build -q -t "${TAG[$V]}" -f "$CTX/Dockerfile" "$CTX" > "$OUT/build.log" 2>&1
  local BRC=$?
  set -e
  echo "$BRC" > "$OUT/build.rc"
  [ "$BRC" -eq 0 ] || { echo "BUILD FAILED $V" >&2; exit "$BRC"; }
  # 独立结构复核
  BASE_IMG="${BASE[$V]}" TAG_IMG="${TAG[$V]}" ART="$ART" OUT="$OUT" timeout 300 python3 - > "$OUT/verify.txt" 2>&1 <<'PY'
import hashlib, json, os, subprocess, tarfile, sys
BASE=os.environ["BASE_IMG"]; TAG=os.environ["TAG_IMG"]; ART=os.environ["ART"]; OUT=os.environ["OUT"]
NAMES={"function_proxy":"function-proxy","function_master":"function-master",
       "function_agent":"function-agent","runtime_manager":"runtime-manager"}
art=json.load(open(ART))
FULL={e["path"].rsplit("/",1)[-1]:e["sha256"] for e in art["executables"]}
EXPECT={}
for n,d in NAMES.items():
    EXPECT[f"opt/akernel-scheduler/{d}/{n}"]=FULL[n]
    EXPECT[f"home/yuanrong/functionsystem/bin/{n}.wheel"]=FULL[n]
def ins(r):
    return json.loads(subprocess.run(["docker","image","inspect",r],capture_output=True,text=True,check=True).stdout)[0]
errs=[]
base,cand=ins(BASE),ins(TAG)
bl,cl=base["RootFS"]["Layers"],cand["RootFS"]["Layers"]
if cl[:len(bl)]!=bl or len(cl)-len(bl)!=1: errs.append(f"layers {len(bl)}->{len(cl)}")
bc=hashlib.sha256(json.dumps(base["Config"],sort_keys=True).encode()).hexdigest()
cc=hashlib.sha256(json.dumps(cand["Config"],sort_keys=True).encode()).hexdigest()
if cc!=bc: errs.append("config drift")
tp=os.path.join(OUT,"c.tar")
with open(tp,"wb") as f: subprocess.run(["docker","save",TAG],stdout=f,check=True)
t=tarfile.open(tp); man=json.load(t.extractfile("manifest.json"))[0]
files=[]
for L in man["Layers"][len(bl):]:
    lt=tarfile.open(fileobj=t.extractfile(L))
    for m in lt.getmembers():
        p=m.name.lstrip("./").rstrip("/")
        if m.isfile(): files.append((p,m.mode,hashlib.sha256(lt.extractfile(m).read()).hexdigest()))
        elif not m.isdir(): errs.append(f"non-file {p}")
if sorted(files)!=sorted([(k,0o755,v) for k,v in EXPECT.items()]): errs.append("files mismatch")
os.remove(tp)
print(f"layers {len(bl)}->{len(cl)} config {'EQUAL' if cc==bc else 'MISMATCH'} files={len(files)}")
print("VERIFY_OK" if not errs else "VERIFY_FAILED: "+"; ".join(errs))
sys.exit(0 if not errs else 1)
PY
  local VRC=$?
  echo "$VRC" > "$OUT/verify.rc"
  [ "$VRC" -eq 0 ] || { echo "VERIFY FAILED $V" >&2; exit "$VRC"; }
  echo "$V READY: ${TAG[$V]}"
}

for V in node master frontend; do build_role "$V"; done
echo 0 > "$FINAL"
echo "ALL THREE READY"
