#!/bin/bash
# audit-restore-ceiling-72da2fd-images.sh — 独立补齐审计(root 复核
# 7851c9bd 三项收口后新增; 不改不动已执行构建的原件与证据)。
# 对已产出的两个候选标签做独立复核:
#   A. 镜像结构独立复核(不依赖构建脚本内建 verify): 层序=基座前缀+1,
#      runtime config 与基座整体相等, manifest/inspect 交叉核对, 新层
#      恰 8 个常规文件 0755 且 SHA=Release artifacts.json;
#   B. 按 wrapper 语义的有界加载检查: 经 /home/yuanrong/functionsystem/
#      bin/<prog>(真实 wrapper, 自带 LD_LIBRARY_PATH)以外部 timeout 运行
#      --help, --network none --read-only, 容器保留; 记录 ExitCode 与输出;
#      126/127/124(加载或超时失败)判 FAIL 并门控本审计最终 rc(修复
#      7851c9bd 中 loadcheck 不影响 final 的问题), 其余退出码仅为
#      "到达 main 参数解析"的有限加载证据;
#   C. term 复用于 frontend 的另证: frontend 实际运行镜像 digest 必须
#      等于 term 基座(term-m1)。
# 权威终态只有一个写入点(EXIT trap); 另存宿主真实退出码。
set -uo pipefail -o noclobber

PARENT=$1   # 独立审计输出父目录(必须不存在)

ART=/data/work/72da2ffd-release-20260928T072812Z/audit-72da2ffd-rel-a1-20260928T072812Z/artifacts.json
BASE_NODE=akernel-bm1/all-in-one@sha256:1bb51e0ec78b3e0932d48848ab68b3b937052b0796dc4c6af844fcdb2cf50d07
BASE_TERM=akernel-bm1/all-in-one@sha256:28e6e5fb707783feb1d90725a33983b71f5bbd7d6bb337e2a194614b19b0c266
TAG_NODE=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v3
TAG_TERM=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term-m2

[ -e "$PARENT" ] && { echo "FATAL: $PARENT exists" >&2; exit 2; }
mkdir "$PARENT"

FINAL="$PARENT/audit-final.rc"
write_final() { [ -e "$FINAL" ] && return 0; echo "$1" > "$FINAL"; }
HOST_EXIT="$PARENT/host-exit.rc"
trap 'rc=$?; write_final "$rc"; echo "$rc" > "$HOST_EXIT"; date -u +%Y-%m-%dT%H:%M:%S.%3NZ > "$PARENT/finished-at.txt" 2>/dev/null || true' EXIT
# 终态恰好写一次: 各失败路径与成功路径直接首写 FINAL(noclobber 下
# 二次写会失败, trap 的 write_final 见已存在即返回)

structure_check() {  # $1=OUT $2=BASE $3=TAG
  BASE="$2" TAG="$3" ART="$ART" OUTDIR="$1" timeout 300 python3 - <<'PY'
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
# 保存候选 tar 并从 tar 复核新层(独立于 inspect)
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
    errs.append(f"new regular files mismatch: got {sorted(f[0] for f in files)}")
idx = json.load(t.extractfile("index.json"))
md = idx["manifests"][0]["digest"]
iid = cand["Id"]
if "sha256:" + md.split(":")[1] != iid and md != iid:
    errs.append(f"manifest digest {md} != inspect Id {iid}")
os.remove(tar_path)
print("inspect_id:", iid)
print(f"base diff_ids={len(bl)} candidate={len(cl)} (+{len(cl)-len(bl)})")
print("config canonical base:", bc[:16], "cand:", cc[:16],
      "EQUAL" if cc == bc else "MISMATCH")
print("new regular files:", len(files),
      "all 0755 release SHAs:", sorted(files) == want)
print("STRUCTURE_OK" if not errs else "STRUCTURE_FAILED: "
      + "; ".join(errs))
sys.exit(0 if not errs else 1)
PY
}

audit_image() {  # $1=variant $2=base $3=tag
  local V=$1 BASE=$2 TAG=$3
  local OUT="$PARENT/$V"
  mkdir -p "$OUT"
  set +e
  structure_check "$OUT" "$BASE" "$TAG" \
    > "$OUT/structure.txt" 2> "$OUT/structure.err"
  local SRC=$?
  set -e
  echo "$SRC" > "$OUT/structure.rc"
  [ "$SRC" -eq 0 ] || return 80
  local LOAD_FAIL=0
  local TS=$(date -u +%Y%m%dT%H%M%SZ)
  for PROG in function_proxy function_master function_agent runtime_manager; do
    set +e
    timeout 60 docker run --name "rc72da-audit-$V-$PROG-$TS" \
      --network none --read-only \
      --entrypoint "/home/yuanrong/functionsystem/bin/$PROG" \
      "$TAG" --help > "$OUT/load-$PROG.txt" 2>&1
    local R1=$?
    set -e
    echo "$R1" > "$OUT/load-$PROG.rc"
    docker inspect "rc72da-audit-$V-$PROG-$TS" --format \
      "State={{.State.Status}} ExitCode={{.State.ExitCode}}" \
      > "$OUT/load-$PROG.state.txt" 2>/dev/null || true
    if [ "$R1" -eq 126 ] || [ "$R1" -eq 127 ] || [ "$R1" -eq 124 ]; then
      LOAD_FAIL=1
      echo "LOAD-FAIL $V/$PROG rc=$R1" >> "$OUT/load-failures.txt"
    fi
  done
  echo "$LOAD_FAIL" > "$OUT/load-final.rc"
  [ "$LOAD_FAIL" -eq 0 ] || return 90
  return 0
}

# C. frontend 另证: frontend 实际镜像 digest == term 基座
FE_IMG=$(kubectl -n akernel get pods -o json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(next(i['status']['containerStatuses'][0]['imageID'] for i in d['items'] if i['metadata']['name'].startswith('akernel-frontend')))" 2>/dev/null)
FE_SHA=$(printf '%s' "$FE_IMG" | grep -o 'sha256:[0-9a-f]\{64\}' | head -1)
echo "frontend_imageid=$FE_IMG" > "$PARENT/frontend-imageid.txt"
FE_WANT="sha256:28e6e5fb707783feb1d90725a33983b71f5bbd7d6bb337e2a194614b19b0c266"
if [ "$FE_SHA" != "$FE_WANT" ]; then
  echo "FATAL: frontend 运行镜像 $FE_SHA != term 基座 $FE_WANT" >&2
  echo 91 > "$FINAL"; exit 91
fi
echo 0 > "$PARENT/frontend-base-match.rc"

NRC=0; audit_image node "$BASE_NODE" "$TAG_NODE" || NRC=$?
if [ "$NRC" -ne 0 ]; then echo "$NRC" > "$FINAL"; exit "$NRC"; fi
TRC=0; audit_image term "$BASE_TERM" "$TAG_TERM" || TRC=$?
if [ "$TRC" -ne 0 ]; then echo "$TRC" > "$FINAL"; exit "$TRC"; fi

echo 0 > "$FINAL"
echo "AUDIT_OK node+term"
