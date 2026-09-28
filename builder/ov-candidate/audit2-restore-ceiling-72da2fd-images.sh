#!/bin/bash
# audit2-restore-ceiling-72da2fd-images.sh — 强化独立审计 v2
# (root 复核: 仅排除 124/126/127 会漏过 139 等崩溃; timeout 只中止
# docker 客户端可能留下容器; 审计须登记自身来源)。
# 对三个已产出候选标签做独立复核:
#   A. 结构: 层序=基座前缀+1; runtime config 与基座整体相等;
#      manifest/inspect 交叉核对; 新层恰 8 个常规文件 0755 且
#      SHA=Release artifacts.json;
#   B. 强化加载判据(经真实 wrapper, 外部 timeout):
#      1) 容器终态必须 exited——若 timeout 后仍在运行, 仅对本审计
#         命名容器有界 docker stop 后重取终态并登记;
#      2) 真实退出码取 docker inspect ExitCode: 拒绝 >=128(信号死,
#         含139)与负值;
#      3) 输出证据: 程序输出须含可识别的参数解析痕迹(usage/Usage/
#         flag/--ip/参数)且非空——空输出按 FAIL;
#      三者全过才算该程序加载通过, 且门控本审计最终 rc。
#   C. 角色基座对证: 各角色 pod 实际 image TAG 与本审计登记的
#      基座一致(frontend=…-term/d307f769, master=…-term-m1/28e6e5fb,
#      nodes=…-splitfsr-v2/1bb51e0e)。
# 来源登记: 本脚本 SHA、配方归档 SHA/commit、被审镜像 digest 全部
# 写入 provenance.txt。终态一次写入; host-exit 另存。
set -uo pipefail -o noclobber

PARENT=$1

ART=/data/work/72da2ffd-release-20260928T072812Z/audit-72da2ffd-rel-a1-20260928T072812Z/artifacts.json
BASE_NODE=akernel-bm1/all-in-one@sha256:1bb51e0ec78b3e0932d48848ab68b3b937052b0796dc4c6af844fcdb2cf50d07
BASE_MASTER=akernel-bm1/all-in-one@sha256:28e6e5fb707783feb1d90725a33983b71f5bbd7d6bb337e2a194614b19b0c266
BASE_FE=akernel-bm1/all-in-one@sha256:d307f76990fdff27770904b9aef6d2cdf8358dfd26828b2ebbb03aca321758b0
TAG_NODE=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v3
TAG_MASTER=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term-m2
TAG_FE=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term-fe-m1
RECIPE_COMMIT=bd1f9c9d10ee25159247536dc588fe1c5aa9264e
RECIPE_ARCHIVE_SHA=$(cat /tmp/rc72da-audit2-recipe.tar.sha256 2>/dev/null || echo unregistered)

[ -e "$PARENT" ] && { echo "FATAL: $PARENT exists" >&2; exit 2; }
mkdir "$PARENT"

FINAL="$PARENT/audit2-final.rc"
write_final() { [ -e "$FINAL" ] && return 0; echo "$1" > "$FINAL"; }
HOST_EXIT="$PARENT/host-exit.rc"
trap 'rc=$?; write_final "$rc"; echo "$rc" > "$HOST_EXIT"; date -u +%Y-%m-%dT%H:%M:%S.%3NZ > "$PARENT/finished-at.txt" 2>/dev/null || true' EXIT

SELF_SHA=$(sha256sum "$0" | awk '{print $1}')
{
  echo "audit_script=$0"
  echo "audit_script_sha256=$SELF_SHA"
  echo "recipe_commit=$RECIPE_COMMIT"
  echo "recipe_archive_sha256=$RECIPE_ARCHIVE_SHA"
  echo "release_artifacts=$ART"
  for t in TAG_NODE TAG_MASTER TAG_FE BASE_NODE BASE_MASTER BASE_FE; do
    echo "$t=${!t}"
  done
  for T in "$TAG_NODE" "$TAG_MASTER" "$TAG_FE"; do
    docker image inspect "$T" --format "image $T id={{.Id}}" 2>/dev/null
  done
} > "$PARENT/provenance.txt"

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
print("config canonical:", cc[:16], "EQUAL" if cc == bc else "MISMATCH")
print("new regular files:", len(files),
      "all 0755 release SHAs:", sorted(files) == want)
print("STRUCTURE_OK" if not errs else "STRUCTURE_FAILED: "
      + "; ".join(errs))
sys.exit(0 if not errs else 1)
PY
}

load_check() {  # $1=OUT $2=TAG $3=VARIANT  — 强化判据, 门控终态
  local OUT=$1 TAG=$2 V=$3
  local LOAD_FAIL=0
  local TS=$(date -u +%Y%m%dT%H%M%SZ)
  for PROG in function_proxy function_master function_agent runtime_manager; do
    local CNAME="rc72da-a2-$V-$PROG-$TS"
    set +e
    timeout 60 docker run --name "$CNAME" --network none --read-only \
      --entrypoint "/home/yuanrong/functionsystem/bin/$PROG" \
      "$TAG" --help > "$OUT/load-$PROG.txt" 2>&1
    local CLIENT_RC=$?
    set -e
    echo "$CLIENT_RC" > "$OUT/load-$PROG.client-rc"
    # timeout 只中止 docker 客户端: 确认容器终态; 仍运行则仅对本审计
    # 命名容器有界停止后重取
    local ST=$(docker inspect "$CNAME" --format "{{.State.Status}}" 2>/dev/null || echo missing)
    if [ "$ST" != "exited" ]; then
      echo "still-$ST-stop-bounded" >> "$OUT/load-$PROG.stop.txt"
      timeout 30 docker stop -t 10 "$CNAME" >> "$OUT/load-$PROG.stop.txt" 2>&1 || true
      ST=$(docker inspect "$CNAME" --format "{{.State.Status}}" 2>/dev/null || echo missing)
    fi
    local EC=$(docker inspect "$CNAME" --format "{{.State.ExitCode}}" 2>/dev/null || echo -1)
    local OOM=$(docker inspect "$CNAME" --format "{{.State.OOMKilled}}" 2>/dev/null || echo unknown)
    { echo "Status=$ST ExitCode=$EC OOMKilled=$OOM ClientRc=$CLIENT_RC"; } \
      > "$OUT/load-$PROG.state.txt"
    # 判据: exited + 非负且 <128 的真实退出码 + 非空且含参数解析痕迹
    local OUTTXT=$(head -c 2000 "$OUT/load-$PROG.txt")
    local VERDICT=ok
    [ "$ST" = "exited" ] || VERDICT="not-exited($ST)"
    case "$EC" in
      ''|*[!0-9-]*) VERDICT="exit-code-unreadable($EC)";;
      *) if [ "$EC" -lt 0 ] || [ "$EC" -ge 128 ]; then
           VERDICT="signal-death-or-invalid($EC)"
         fi;;
    esac
    [ "$OOM" = "false" ] || VERDICT="oom($OOM)"
    if ! printf '%s' "$OUTTXT" | grep -qiE "usage|flag|--ip|参数|ip address"; then
      VERDICT="no-arg-parse-evidence"
    fi
    [ -n "$OUTTXT" ] || VERDICT="empty-output"
    echo "$VERDICT" > "$OUT/load-$PROG.verdict"
    if [ "$VERDICT" != "ok" ]; then
      LOAD_FAIL=1
      echo "LOAD-FAIL $V/$PROG $VERDICT" >> "$OUT/load-failures.txt"
    fi
  done
  echo "$LOAD_FAIL" > "$OUT/load-final.rc"
  [ "$LOAD_FAIL" -eq 0 ] || return 90
  return 0
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
  set +e
  load_check "$OUT" "$TAG" "$V"
  local LRC=$?
  set -e
  [ "$LRC" -eq 0 ] || return "$LRC"
  return 0
}

# C. 角色基座对证(pod 实际 image TAG == 本审计登记基座 TAG)
role_pod_images() {
  kubectl -n akernel get pods -o json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for i in d['items']:
    n = i['metadata']['name']
    if n.startswith(('akernel-master', 'akernel-frontend', 'akernel-node')):
        print(n, i['status']['containerStatuses'][0].get('image', ''))"
}
role_pod_images > "$PARENT/role-pod-images.txt"
ROLE_BAD=0
grep -q "akernel-frontend-.* akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term$" "$PARENT/role-pod-images.txt" || ROLE_BAD=1
grep -q "akernel-master-0 akernel-bm1/all-in-one:canon-df2e-yr-signalfix-term-m1$" "$PARENT/role-pod-images.txt" || ROLE_BAD=1
[ "$(grep -c 'akernel-node-.* akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v2$' "$PARENT/role-pod-images.txt")" -ge 1 ] || ROLE_BAD=1
echo "$ROLE_BAD" > "$PARENT/role-base-match.rc"
[ "$ROLE_BAD" -eq 0 ] || { echo 91 > "$FINAL"; exit 91; }

NRC=0; audit_image node "$BASE_NODE" "$TAG_NODE" || NRC=$?
[ "$NRC" -eq 0 ] || { echo "$NRC" > "$FINAL"; exit "$NRC"; }
MRC=0; audit_image master "$BASE_MASTER" "$TAG_MASTER" || MRC=$?
[ "$MRC" -eq 0 ] || { echo "$MRC" > "$FINAL"; exit "$MRC"; }
FRC=0; audit_image frontend "$BASE_FE" "$TAG_FE" || FRC=$?
[ "$FRC" -eq 0 ] || { echo "$FRC" > "$FINAL"; exit "$FRC"; }

echo 0 > "$FINAL"
echo "AUDIT2_OK node+master+frontend"
