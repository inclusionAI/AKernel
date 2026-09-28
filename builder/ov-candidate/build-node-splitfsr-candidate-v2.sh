#!/bin/bash
# build-node-splitfsr-candidate-v2.sh — 在 BM1 执行的节点候选镜像 v2 构建
# 脚本(配方待根审;未获准运行)。基于 rev29 节点镜像(75b136c8...),仅替换
# /usr/local/bin/sandboxd 为组合检查点修复产物(8f43b1cb);runsc(7d9c5612...)
# 与全部配置/SIGRTMIN+3 保持不变。
#
# 与 v1 配方(41a83e34)的差异:基座换成 rev29 节点镜像 digest;单文件校验
# (恰一个新层、恰一个常规文件);真实构建终止时间以容器/进程 State 为准,
# 包装器时间只作记录不作终态。
set -euo pipefail -o noclobber

PARENT=$1                       # 调用方给出的独占父目录(必须不存在)
RECIPE_DIR=$2                   # 干净配方归档目录(含 Dockerfile 与本脚本)
SRC_SANDBOXD=/data/work/sb-rel-8f43b1cb-run2-20260928T044821Z/build/audit/sandboxd
SANDBOXD_SHA=25c36b2a673b0d15901e2dadf450ce9eb8a68ca21e3c494a8db1e9ff16b3d931
BASE_REF=akernel-bm1/all-in-one@sha256:75b136c8dd04a6e624eec8f88f6c8847dbbc3f4173a05823c0362080f90f72a1
TAG=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v2
COMMIT_SB=8f43b1cb8aa8da293fdc0decd94893ff7585b653
DOCKERFILE_SHA=9d8e93bd8892b250d9b2dd438624cdbe594b4e775eba84acc61a24e565421893
CPUS="0-11"

[ -e "$PARENT" ] && { echo "FATAL: $PARENT exists" >&2; exit 1; }
mkdir "$PARENT"
OUT="$PARENT/out"; CTX="$PARENT/ctx"
mkdir "$OUT" "$CTX"

FINAL_ONCE="$OUT/.final-written"
write_final() { [ -e "$FINAL_ONCE" ] && return 0; echo "$1" > "$OUT/final.rc"; touch "$FINAL_ONCE"; }
trap 'rc=$?; write_final "$rc"; date -u +%Y-%m-%dT%H:%M:%S.%3NZ > "$OUT/finished-at.txt" 2>/dev/null || true' EXIT

# Dockerfile 从配方归档复制进 CTX,先核其 SHA
[ -f "$RECIPE_DIR/node-splitfsr-candidate-v2.Dockerfile" ] || { echo "FATAL: Dockerfile missing" >&2; exit 1; }
got=$(sha256sum "$RECIPE_DIR/node-splitfsr-candidate-v2.Dockerfile" | awk '{print $1}')
[ "$got" = "$DOCKERFILE_SHA" ] || { echo "FATAL: Dockerfile sha $got != pin" >&2; exit 1; }
cp "$RECIPE_DIR/node-splitfsr-candidate-v2.Dockerfile" "$CTX/"

# 基座 runtime config 规范哈希从实际 inspect 取得并登记(不输出内容)
BASE_CANON=$(docker image inspect "$BASE_REF" --format "{{json .Config}}" | python3 -c "import json,sys,hashlib; print(hashlib.sha256(json.dumps(json.load(sys.stdin), sort_keys=True).encode()).hexdigest())")
echo "base_runtime_config_canonical=$BASE_CANON" > "$OUT/base-config-canon.txt"

# 输入产物先核 SHA 再复制
[ -f "$SRC_SANDBOXD" ] || { echo "FATAL: missing $SRC_SANDBOXD" >&2; exit 1; }
got=$(sha256sum "$SRC_SANDBOXD" | awk '{print $1}')
[ "$got" = "$SANDBOXD_SHA" ] || { echo "FATAL: sandboxd sha mismatch: $got" >&2; exit 1; }

ionice -c 3 rsync -a --bwlimit=65536 "$SRC_SANDBOXD" "$CTX/sandboxd" > "$OUT/copy-sandboxd.log" 2>&1
chmod 0755 "$CTX/sandboxd"
sha256sum "$CTX/node-splitfsr-candidate-v2.Dockerfile" "$CTX/sandboxd" > "$OUT/input-sha256.txt"
stat -c "%a %n" "$CTX/sandboxd" > "$OUT/ctx-modes.txt"

set +e
docker build -t "$TAG" -f "$CTX/node-splitfsr-candidate-v2.Dockerfile" "$CTX" > "$OUT/build.log" 2>&1
BRC=$?
set -e
echo "$BRC" > "$OUT/build.rc"
[ "$BRC" -eq 0 ] || { echo "BUILD FAILED rc=$BRC" >&2; exit "$BRC"; }
docker image inspect "$TAG" --format "inspect_id={{.Id}}" > "$OUT/image-id.txt"

set +e
ionice -c 3 docker save "$TAG" | pv -L 64m | gzip > "$OUT/node-candidate-v2.tar.gz"
SRC_RC=$?
set -e
echo "$SRC_RC" > "$OUT/save.rc"
[ "$SRC_RC" -eq 0 ] || { echo "FATAL: save pipeline rc=$SRC_RC" >&2; exit "$SRC_RC"; }
sha256sum "$OUT/node-candidate-v2.tar.gz" > "$OUT/image-tar.sha256"

# 验证:基座前缀+恰 1 层;runtime config 与基座整体相等(含 SIGRTMIN+3);
# 新层恰一个常规文件 sandboxd 0755/SHA;manifest 交叉核对;真实 config 从
# manifest config.digest 取得
set +e
python3 - "$OUT/node-candidate-v2.tar.gz" "$BASE_CANON" > "$OUT/verify.txt" 2>&1 <<'PYEOF'
import tarfile, json, hashlib, subprocess, sys
BASE_REF = "akernel-bm1/all-in-one@sha256:75b136c8dd04a6e624eec8f88f6c8847dbbc3f4173a05823c0362080f90f72a1"
BASE_CANON = sys.argv[2]
EXPECT = {"usr/local/bin/sandboxd": "25c36b2a673b0d15901e2dadf450ce9eb8a68ca21e3c494a8db1e9ff16b3d931"}
TAG = "akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr-v2"
def inspect(ref):
    out = subprocess.run(["docker", "image", "inspect", ref],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)[0]
base, cand = inspect(BASE_REF), inspect(TAG)
errs = []
bl = base["RootFS"]["Layers"]; cl = cand["RootFS"]["Layers"]
if cl[:len(bl)] != bl or len(cl) - len(bl) != 1:
    errs.append(f"diff_ids: base={len(bl)} cand={len(cl)} prefix+1 mismatch")
cc = hashlib.sha256(json.dumps(cand["Config"], sort_keys=True).encode()).hexdigest()
if cc != BASE_CANON:
    errs.append("candidate runtime config != base canonical")
t = tarfile.open(sys.argv[1])
man = json.load(t.extractfile("manifest.json"))[0]
layers = man["Layers"]
files, others = [], []
for L in layers[len(bl):]:
    lt = tarfile.open(fileobj=t.extractfile(L))
    for m in lt.getmembers():
        p = m.name.lstrip("./")
        if m.isfile():
            files.append((p, m.mode, hashlib.sha256(lt.extractfile(m).read()).hexdigest()))
        elif m.isdir():
            continue
        else:
            others.append((p, m.type))
if sorted(files) != sorted([(k, 0o755, v) for k, v in EXPECT.items()]):
    errs.append(f"new regular files mismatch: {files}")
if others:
    errs.append(f"non-file non-dir entries: {others}")
idx = json.load(t.extractfile("index.json"))
md = idx["manifests"][0]["digest"]; iid = cand["Id"]
if "sha256:" + md.split(":")[1] != iid and md != iid:
    errs.append(f"manifest digest {md} != inspect Id {iid}")
cfg_digest = "sha256:" + man["Config"].split("/")[-1]
print("inspect_id:", iid)
print("manifest digest:", md, "true config digest:", cfg_digest)
print(f"base diff_ids={len(bl)} candidate={len(cl)} (+{len(cl)-len(bl)})")
print("runtime-config canonical:", cc[:16], "(== base)" if cc == BASE_CANON else "MISMATCH")
print("new regular files:", [(p, oct(m), h[:16]) for (p, m, h) in files])
print("VERIFY_OK" if not errs else "VERIFY_FAILED: " + "; ".join(errs))
sys.exit(0 if not errs else 1)
PYEOF
VRC=$?
set -e
echo "$VRC" > "$OUT/verify.rc"
[ "$VRC" -eq 0 ] || { echo "VERIFY FAILED" >&2; exit "$VRC"; }

# 冒烟:隔离只读无网络,容器保留,先取 State 再判 rc
TS=$(date -u +%Y%m%dT%H%M%SZ)
set +e
docker run --name node-cand2-sandboxd-$TS --network none --read-only \
  --entrypoint /usr/local/bin/sandboxd "$TAG" -h > "$OUT/sandboxd-help.txt" 2>&1
R1=$?
set -e
docker inspect node-cand2-sandboxd-$TS --format "State={{.State.Status}} ExitCode={{.State.ExitCode}} FinishedAt={{.State.FinishedAt}}" > "$OUT/sandboxd-check-state.txt"
echo "$R1" > "$OUT/sandboxd-help.rc"
if [ "$R1" -ne 0 ]; then
  echo "FATAL: sandboxd -h rc=$R1" >&2; exit 1
fi
echo "NODE CANDIDATE V2 READY: $TAG (inspect/manifest id in image-id.txt; true config in verify.txt)"
