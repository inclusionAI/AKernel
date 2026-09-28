#!/bin/bash
# build-node-splitfsr-candidate.sh — 在 BM1 执行的节点候选镜像构建脚本
# (配方待根审；未获准运行)。split_fsrestore 修复仅替换节点角色的
# runsc 与 sandboxd 两个文件,基座固定为当前节点镜像完整 digest,
# SIGRTMIN+3 及其余配置保持不变。
set -euo pipefail -o noclobber

PARENT=$1                       # 调用方给出的独占父目录(必须不存在)
RECIPE_DIR=$2                   # 本次干净配方归档解包目录(含 Dockerfile 与本脚本)
SRC_RUNSC=/data/work/splitfsr-rel-20260928T034550Z/runsc-OUT/runsc
SRC_SANDBOXD=/data/work/splitfsr-rel-20260928T034550Z/sb/audit/sandboxd
RUNSC_SHA=7d9c56125b84c00817fdd0173912055bb3314fdea4b33dd09c0910c83762ac13
SANDBOXD_SHA=205e5c0a2ef7793a02aafc5a0bf387a4f650d4727f50b5c13b1f7f15187d8704
BASE_REF=akernel-bm1/all-in-one@sha256:6c8054ed6e6fee0f81468e766735b64b3487e62cdddc6140e178f7eff566f33d
BASE_CONFIG_CANON_SHA=54b05d9b25c5514227b3d0c9365efe92416f49c5097098b47c75e55e90e151c7
TAG=akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr
COMMIT_GV=4d3d481d60bb433f6b58f03fb92ae3c6d2de651f
DOCKERFILE_SHA=b02f6510fa46ed2f327420f9a3beb8c68a60c5928fc8067a618a0b3838cf66f4

[ -e "$PARENT" ] && { echo "FATAL: $PARENT exists" >&2; exit 1; }
mkdir "$PARENT"
OUT="$PARENT/out"; CTX="$PARENT/ctx"
mkdir "$OUT" "$CTX"

FINAL_ONCE="$OUT/.final-written"
write_final() { [ -e "$FINAL_ONCE" ] && return 0; echo "$1" > "$OUT/final.rc"; touch "$FINAL_ONCE"; }
trap 'rc=$?; write_final "$rc"; date -u +%Y-%m-%dT%H:%M:%S.%3NZ > "$OUT/finished-at.txt" 2>/dev/null || true' EXIT

# Dockerfile 从本次干净配方归档目录复制进 CTX,先核其 SHA
[ -f "$RECIPE_DIR/node-splitfsr-candidate.Dockerfile" ] || { echo "FATAL: Dockerfile not in recipe dir" >&2; exit 1; }
got=$(sha256sum "$RECIPE_DIR/node-splitfsr-candidate.Dockerfile" | awk '{print $1}')
[ "$got" = "$DOCKERFILE_SHA" ] || { echo "FATAL: Dockerfile sha $got != pin" >&2; exit 1; }
cp "$RECIPE_DIR/node-splitfsr-candidate.Dockerfile" "$CTX/"

# 1) 两个输入产物先核 SHA 再复制
for pair in "$SRC_RUNSC:$RUNSC_SHA" "$SRC_SANDBOXD:$SANDBOXD_SHA"; do
  src=${pair%%:*}; want=${pair##*:}
  [ -f "$src" ] || { echo "FATAL: missing $src" >&2; exit 1; }
  got=$(sha256sum "$src" | awk '{print $1}')
  [ "$got" = "$want" ] || { echo "FATAL: sha mismatch $src: $got" >&2; exit 1; }
done

# 2) 低优先级串行限速复制;0755;输入清单
ionice -c 3 rsync -a --bwlimit=65536 "$SRC_RUNSC" "$CTX/runsc" > "$OUT/copy-runsc.log" 2>&1
ionice -c 3 rsync -a --bwlimit=65536 "$SRC_SANDBOXD" "$CTX/sandboxd" > "$OUT/copy-sandboxd.log" 2>&1
chmod 0755 "$CTX/runsc" "$CTX/sandboxd"
sha256sum "$CTX/node-splitfsr-candidate.Dockerfile" "$CTX/runsc" "$CTX/sandboxd" > "$OUT/input-sha256.txt"
stat -c "%a %n" "$CTX/runsc" "$CTX/sandboxd" > "$OUT/ctx-modes.txt"

# 3) 构建
set +e
docker build -t "$TAG" -f "$CTX/node-splitfsr-candidate.Dockerfile" "$CTX" > "$OUT/build.log" 2>&1
BRC=$?
set -e
echo "$BRC" > "$OUT/build.rc"
[ "$BRC" -eq 0 ] || { echo "BUILD FAILED rc=$BRC" >&2; exit "$BRC"; }
# 本后端 .Id 即 OCI manifest digest;存 inspect_id,真实 config digest
# 稍后从已校验 manifest 的 config.digest 取得(见 verify 步)。
docker image inspect "$TAG" --format "inspect_id={{.Id}}" > "$OUT/image-id.txt"

# 4) 保存 tar(pipefail 管道,真实 rc,非 0 即退;限速)
set +e
ionice -c 3 sh -c "docker save '$TAG' | pv -L 64m | gzip > '$OUT/node-candidate.tar.gz'"
SRC_RC=$?
set -e
echo "$SRC_RC" > "$OUT/save.rc"
[ "$SRC_RC" -eq 0 ] || { echo "FATAL: docker save pipeline rc=$SRC_RC" >&2; exit "$SRC_RC"; }
sha256sum "$OUT/node-candidate.tar.gz" > "$OUT/image-tar.sha256"

# 5) 层/config/文件验证:基座前缀+恰 2 层;完整 runtime config 相等
#    (内部比较,不输出 env);新层常规文件恰两个预期路径,拒绝额外
#    文件/链接/whiteout;0755+SHA
set +e
python3 - "$OUT/node-candidate.tar.gz" > "$OUT/verify.txt" 2>&1 <<'PYEOF'
import tarfile, json, hashlib, subprocess, sys
BASE_REF = "akernel-bm1/all-in-one@sha256:6c8054ed6e6fee0f81468e766735b64b3487e62cdddc6140e178f7eff566f33d"
BASE_CANON = "54b05d9b25c5514227b3d0c9365efe92416f49c5097098b47c75e55e90e151c7"
EXPECT = {
    "usr/local/bin/runsc": "7d9c56125b84c00817fdd0173912055bb3314fdea4b33dd09c0910c83762ac13",
    "usr/local/bin/sandboxd": "205e5c0a2ef7793a02aafc5a0bf387a4f650d4727f50b5c13b1f7f15187d8704",
}
def inspect(ref):
    out = subprocess.run(["docker", "image", "inspect", ref],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)[0]
base = inspect(BASE_REF)
cand = inspect(sys.argv[1].rsplit("/", 1)[0] and "akernel-bm1/all-in-one:canon-df2e-yr-signalfix-node-splitfsr")
errs = []
# 5a) rootfs.diff_ids: 候选 = 基座完整前缀 + 恰 2 层
bl = base["RootFS"]["Layers"]; cl = cand["RootFS"]["Layers"]
if cl[:len(bl)] != bl or len(cl) - len(bl) != 2:
    errs.append(f"diff_ids: base={len(bl)} cand={len(cl)} prefix+2 mismatch")
# 5b) 完整 runtime config 相等(内部哈希比较,不打印内容)
bc = hashlib.sha256(json.dumps(base["Config"], sort_keys=True).encode()).hexdigest()
cc = hashlib.sha256(json.dumps(cand["Config"], sort_keys=True).encode()).hexdigest()
if bc != BASE_CANON or cc != BASE_CANON:
    errs.append("runtime config canonical sha mismatch")
# 5c) 新层 tar 内容:常规文件恰两个预期路径;拒绝额外文件/链接/whiteout
t = tarfile.open(sys.argv[1])
man = json.load(t.extractfile("manifest.json"))[0]
layers = man["Layers"]
extra_new = layers[len(bl):] if len(layers) >= len(bl) else []
files, others = [], []
for L in extra_new:
    lt = tarfile.open(fileobj=t.extractfile(L))
    for m in lt.getmembers():
        p = m.name.lstrip("./")
        base_p = p.split("/")[-1]
        if m.isfile():
            files.append((p, m.mode, hashlib.sha256(lt.extractfile(m).read()).hexdigest()))
        elif m.isdir():
            continue  # 父目录条目允许
        else:
            others.append((p, m.type))
if sorted(files) != sorted([(k, 0o755, v) for k, v in EXPECT.items()]):
    errs.append(f"new regular files mismatch: {files}")
if others:
    errs.append(f"non-file non-dir entries in new layers: {others}")
# 5d) manifest 交叉核对:tar index manifest digest 必须等于 inspect .Id
#     (本后端 .Id=OCI manifest);真实 config digest 从该 manifest 的
#     config.digest 取得并记录。
idx = json.load(t.extractfile("index.json"))
md = idx["manifests"][0]["digest"]
iid = cand["Id"]
if "sha256:" + md.split(":")[1] != iid and md != iid:
    errs.append(f"manifest digest {md} != inspect Id {iid}")
inner = json.load(t.extractfile("manifest.json"))[0]
cfg_digest = "sha256:" + inner["Config"].split("/")[-1]
print("inspect_id:", iid)
print("manifest digest:", md, "true config digest:", cfg_digest)
print(f"base diff_ids={len(bl)} candidate={len(cl)} (+{len(cl)-len(bl)})")
print("runtime-config canonical: base", bc[:16], "cand", cc[:16])
print("new regular files:", [(p, oct(m), h[:16]) for (p, m, h) in files])
print("VERIFY_OK" if not errs else "VERIFY_FAILED: " + "; ".join(errs))
sys.exit(0 if not errs else 1)
PYEOF
VRC=$?
set -e
echo "$VRC" > "$OUT/verify.rc"
[ "$VRC" -eq 0 ] || { echo "VERIFY FAILED" >&2; exit "$VRC"; }

# 6) 隔离只读无网络冒烟:容器保留,先取 State 再判 rc;非 0 必败
TS=$(date -u +%Y%m%dT%H%M%SZ)
set +e
docker run --name node-cand-runsc-$TS --network none --read-only \
  --entrypoint /usr/local/bin/runsc "$TAG" --version > "$OUT/runsc-version.txt" 2>&1
R1=$?
set -e
docker inspect node-cand-runsc-$TS --format "State={{.State.Status}} ExitCode={{.State.ExitCode}}" > "$OUT/runsc-check-state.txt"
echo "$R1" > "$OUT/runsc-version.rc"
if [ "$R1" -ne 0 ] || ! grep -q "$COMMIT_GV" "$OUT/runsc-version.txt"; then
  echo "FATAL: runsc --version rc=$R1 or missing commit" >&2; exit 1
fi

set +e
docker run --name node-cand-sandboxd-$TS --network none --read-only \
  --entrypoint /usr/local/bin/sandboxd "$TAG" -h > "$OUT/sandboxd-help.txt" 2>&1
R2=$?
set -e
docker inspect node-cand-sandboxd-$TS --format "State={{.State.Status}} ExitCode={{.State.ExitCode}}" > "$OUT/sandboxd-check-state.txt"
echo "$R2" > "$OUT/sandboxd-help.rc"
if [ "$R2" -ne 0 ]; then
  echo "FATAL: sandboxd -h rc=$R2" >&2; exit 1
fi

echo "NODE CANDIDATE READY: $TAG (config-digest in image-id.txt)"
