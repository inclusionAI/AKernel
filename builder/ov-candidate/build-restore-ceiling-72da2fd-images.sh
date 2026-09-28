#!/bin/bash
# build-restore-ceiling-72da2fd-images.sh — BM1 构建脚本(配方待根审)。
# 为 72da2ffd 正式Release(根独立通过)构建两个候选角色镜像:
#   node v3: 基座 canon-df2e-yr-signalfix-node-splitfsr-v2(SIGRTMIN+3)
#   term m2: 基座 canon-df2e-yr-signalfix-term-m1(SIGTERM, master/frontend)
# 每个镜像替换四程序到真实 /opt 运行路径与 wheel 副本
# (bin/*.wheel);wrapper/sandboxd/runsc/RRT/yr/插件/配置逐字节不变,
# runtime config 须与基座整体相等(核 SHA)。
# 动态库结论(构建前已核,libdiff-audit): 两基座现有 lib 目录即可完整
# 解析四个新程序(ldd rc=0, not-found=0), 无需替换任何库。
set -euo pipefail -o noclobber

PARENT=$1                      # 独占父目录(必须不存在)
RECIPE_DIR=$2                  # 干净配方归档目录
REL=/data/work/72da2ffd-release-20260928T072812Z/functionsystem/build-release/bin

# 产物完整 SHA 以 Release attempt 的 artifacts.json 为唯一权威
ART=/data/work/72da2ffd-release-20260928T072812Z/audit-72da2ffd-rel-a1-20260928T072812Z/artifacts.json

BASE_NODE=akernel-bm1/all-in-one@sha256:1bb51e0ec78b3e0932d48848ab68b3b937052b0796dc4c6af844fcdb2cf50d07
BASE_TERM=akernel-bm1/all-in-one@sha256:28e6e5fb707783feb1d90725a33983b71f5bbd7d6bb337e2a194614b19b0c266
TAG_NODE=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v3
TAG_TERM=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term-m2
DF_NODE_SHA=PENDING
DF_TERM_SHA=PENDING

[ -e "$PARENT" ] && { echo "FATAL: $PARENT exists" >&2; exit 1; }
mkdir "$PARENT"

# artifacts.json 中四个产物的完整 SHA 为唯一权威
readarray -t FULL < <(python3 - "$ART" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
names = {"function_proxy", "function_master", "function_agent",
         "runtime_manager"}
got = {e["path"].rsplit("/", 1)[-1]: e["sha256"]
       for e in d["executables"]}
missing = names - set(got)
assert not missing, f"artifacts.json missing {missing}"
for n in sorted(names):
    print(n + " " + got[n])
PY
)
declare -A FULLSHA
for line in "${FULL[@]}"; do FULLSHA[${line%% *}]=${line#* }; done

FINAL_ONCE="$PARENT/.final-written"
write_final() { [ -e "$FINAL_ONCE" ] && return 0; echo "$1" > "$PARENT/final.rc"; touch "$FINAL_ONCE"; }
trap 'rc=$?; write_final "$rc"; date -u +%Y-%m-%dT%H:%M:%S.%3NZ > "$PARENT/finished-at.txt" 2>/dev/null || true' EXIT

# Dockerfile SHA 核对后复制
for f in restore-ceiling-72da2fd-node-v3.Dockerfile restore-ceiling-72da2fd-term-m2.Dockerfile; do
  [ -f "$RECIPE_DIR/$f" ] || { echo "FATAL: $f missing" >&2; exit 1; }
done
DF_NODE_SHA=$(sha256sum "$RECIPE_DIR/restore-ceiling-72da2fd-node-v3.Dockerfile" | awk '{print $1}')
DF_TERM_SHA=$(sha256sum "$RECIPE_DIR/restore-ceiling-72da2fd-term-m2.Dockerfile" | awk '{print $1}')

# 公共上下文树: 8 个目标文件,全部 0755(模式来自上下文)
make_ctx() {  # $1=ctxdir
  local C=$1
  mkdir -p "$C/rootfs/opt/akernel-scheduler/function-proxy" \
           "$C/rootfs/opt/akernel-scheduler/function-master" \
           "$C/rootfs/opt/akernel-scheduler/function-agent" \
           "$C/rootfs/opt/akernel-scheduler/runtime-manager" \
           "$C/rootfs/home/yuanrong/functionsystem/bin"
  local PROG DIR
  for pair in function_proxy:function-proxy function_master:function-master \
              function_agent:function-agent runtime_manager:runtime-manager; do
    PROG=${pair%%:*}; DIR=${pair##*:}
    [ -f "$REL/$PROG" ] || { echo "FATAL: missing $REL/$PROG" >&2; exit 1; }
    got=$(sha256sum "$REL/$PROG" | awk '{print $1}')
    [ "$got" = "${FULLSHA[$PROG]}" ] || { echo "FATAL: $PROG sha $got != release" >&2; exit 1; }
    ionice -c 3 rsync -a --bwlimit=65536 "$REL/$PROG" "$C/rootfs/opt/akernel-scheduler/$DIR/$PROG"
    ionice -c 3 rsync -a --bwlimit=65536 "$REL/$PROG" "$C/rootfs/home/yuanrong/functionsystem/bin/$PROG.wheel"
  done
  find "$C/rootfs" -type f -exec chmod 0755 {} \;
  ( cd "$C/rootfs" && find . -type f | LC_ALL=C sort | xargs sha256sum ) > "$C/ctx-inputs.sha"
  find "$C/rootfs" -type f -exec stat -c "%a %n" {} \; | LC_ALL=C sort > "$C/ctx-modes.txt"
}

build_one() {  # $1=variant(node|term) $2=base $3=tag $4=dockerfile $5=dfsha
  local V=$1 BASE=$2 TAG=$3 DF=$4 DFS=$5
  local OUT="$PARENT/$V" CTX="$PARENT/$V/ctx"
  mkdir -p "$OUT" "$CTX"
  got=$(sha256sum "$RECIPE_DIR/$DF" | awk '{print $1}')
  [ "$got" = "$DFS" ] || { echo "FATAL: $DF sha drift" >&2; exit 1; }
  cp "$RECIPE_DIR/$DF" "$CTX/"
  make_ctx "$CTX"
  local BASE_CANON
  BASE_CANON=$(docker image inspect "$BASE" --format "{{json .Config}}" | python3 -c "import json,sys,hashlib; print(hashlib.sha256(json.dumps(json.load(sys.stdin), sort_keys=True).encode()).hexdigest())")
  echo "base_runtime_config_canonical=$BASE_CANON" > "$OUT/base-config-canon.txt"
  echo "$DFS  $DF" > "$OUT/recipe-sha.txt"
  set +e
  docker build -t "$TAG" -f "$CTX/$DF" "$CTX" > "$OUT/build.log" 2>&1
  local BRC=$?
  set -e
  echo "$BRC" > "$OUT/build.rc"
  [ "$BRC" -eq 0 ] || { echo "BUILD FAILED $V rc=$BRC" >&2; exit "$BRC"; }
  docker image inspect "$TAG" --format "inspect_id={{.Id}}" > "$OUT/image-id.txt"
  set +e
  ionice -c 3 docker save "$TAG" | pv -L 64m | gzip > "$OUT/$V.tar.gz"
  local SRC_RC=$?
  set -e
  echo "$SRC_RC" > "$OUT/save.rc"
  [ "$SRC_RC" -eq 0 ] || { echo "FATAL: save $V rc=$SRC_RC" >&2; exit "$SRC_RC"; }
  sha256sum "$OUT/$V.tar.gz" > "$OUT/image-tar.sha256"
  # verify: 基座层前缀+恰 1 新层;config 与基座整体相等;新层恰 8 个
  # 常规文件(4 程序 x /opt+wheel), 0755/SHA 与 Release 一致
  BASE_CANON="$BASE_CANON" TAG="$TAG" BASE="$BASE" \
  python3 - "$OUT/$V.tar.gz" "$OUT/verify.txt" <<'PYEOF'
import tarfile, json, hashlib, os, subprocess, sys
BASE = os.environ["BASE"]; TAG = os.environ["TAG"]
BASE_CANON = os.environ["BASE_CANON"]
NAMES = ["function_proxy", "function_master", "function_agent",
         "runtime_manager"]
DIRS = {"function_proxy": "function-proxy",
        "function_master": "function-master",
        "function_agent": "function-agent",
        "runtime_manager": "runtime-manager"}
FULLSHA = {}
art = json.load(open("/data/work/72da2ffd-release-20260928T072812Z/"
                     "audit-72da2ffd-rel-a1-20260928T072812Z/"
                     "artifacts.json"))
for e in art["executables"]:
    FULLSHA[e["path"].rsplit("/", 1)[-1]] = e["sha256"]
EXPECT = {}
for n in NAMES:
    EXPECT[f"opt/akernel-scheduler/{DIRS[n]}/{n}"] = FULLSHA[n]
    EXPECT[f"home/yuanrong/functionsystem/bin/{n}.wheel"] = FULLSHA[n]
def inspect(ref):
    out = subprocess.run(["docker", "image", "inspect", ref],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)[0]
base, cand = inspect(BASE), inspect(TAG)
errs = []
bl = base["RootFS"]["Layers"]; cl = cand["RootFS"]["Layers"]
if cl[:len(bl)] != bl or len(cl) - len(bl) != 1:
    errs.append(f"diff_ids: base={len(bl)} cand={len(cl)} prefix+1 mismatch")
cc = hashlib.sha256(json.dumps(cand["Config"], sort_keys=True).encode()).hexdigest()
if cc != BASE_CANON:
    errs.append("candidate runtime config != base canonical")
t = tarfile.open(sys.argv[1])
man = json.load(t.extractfile("manifest.json"))[0]
files = []
for L in man["Layers"][len(bl):]:
    lt = tarfile.open(fileobj=t.extractfile(L))
    for m in lt.getmembers():
        p = m.name.lstrip("./")
        if p.endswith("/"):
            p = p.rstrip("/")
        if m.isfile():
            files.append((p, m.mode,
                          hashlib.sha256(lt.extractfile(m).read()).hexdigest()))
        elif not m.isdir():
            errs.append(f"non-file non-dir entry: {p} {m.type}")
want = sorted([(k, 0o755, v) for k, v in EXPECT.items()])
if sorted(files) != want:
    got_keys = sorted(f[0] for f in files)
    errs.append(f"new regular files mismatch: got {got_keys} "
                f"want {sorted(EXPECT)}")
idx = json.load(t.extractfile("index.json"))
md = idx["manifests"][0]["digest"]; iid = cand["Id"]
if "sha256:" + md.split(":")[1] != iid and md != iid:
    errs.append(f"manifest digest {md} != inspect Id {iid}")
print("inspect_id:", iid)
print("manifest digest:", md)
print(f"base diff_ids={len(bl)} candidate={len(cl)} (+{len(cl)-len(bl)})")
print("runtime-config canonical:", cc[:16],
      "(== base)" if cc == BASE_CANON else "MISMATCH")
print("new regular files:", len(files),
      "all 0755 release SHAs:", sorted(files) == want)
print("VERIFY_OK" if not errs else "VERIFY_FAILED: " + "; ".join(errs))
sys.exit(0 if not errs else 1)
PYEOF
  local VRC=$?
  echo "$VRC" > "$OUT/verify.rc"
  [ "$VRC" -eq 0 ] || { echo "VERIFY FAILED $V" >&2; exit "$VRC"; }
  # 有限加载冒烟(同 m1 先例): 每程序 --help, 只证 ELF+动态库加载到达
  # main 参数解析; 不称启动成功; 容器保留
  local TS=$(date -u +%Y%m%dT%H%M%SZ)
  for pair in function_proxy:function-proxy function_master:function-master \
              function_agent:function-agent runtime_manager:runtime-manager; do
    local PROG=${pair%%:*} DIR=${pair##*:}
    set +e
    docker run --name "rc72da-$V-$PROG-$TS" --network none --read-only \
      --entrypoint "/opt/akernel-scheduler/$DIR/$PROG" "$TAG" --help \
      > "$OUT/loadcheck-$PROG.txt" 2>&1
    local R1=$?
    set -e
    echo "$R1" > "$OUT/loadcheck-$PROG.rc"
    docker inspect "rc72da-$V-$PROG-$TS" --format \
      "State={{.State.Status}} ExitCode={{.State.ExitCode}}" \
      > "$OUT/loadcheck-$PROG.state.txt"
  done
  echo "$V CANDIDATE READY: $TAG"
}

build_one node "$BASE_NODE" "$TAG_NODE" \
  restore-ceiling-72da2fd-node-v3.Dockerfile "$DF_NODE_SHA"
build_one term "$BASE_TERM" "$TAG_TERM" \
  restore-ceiling-72da2fd-term-m2.Dockerfile "$DF_TERM_SHA"

echo 0 > "$PARENT/final.rc"
echo "BOTH ROLE CANDIDATES READY"
