# 节点候选镜像(node-splitfsr-candidate)配方

目的:落地 split_fsrestore 双仓修复(gvisor 4d3d481d60bb433f6b58f03fb92ae3c6d2de651f
与 sandboxd a37bf059f072be2f60e48d737403e9a7355c802e)到**仅节点角色**镜像。

## 固定输入(根已独立验收)

- 基座 = 当前节点角色镜像完整 digest
  `akernel-bm1/all-in-one@sha256:6c8054ed6e6fee0f81468e766735b64b3487e62cdddc6140e178f7eff566f33d`
  (SIGRTMIN+3 systemd 停止信号与其余全部配置字节保持不变)
- runsc 产物(远端 `/data/work/splitfsr-rel-20260928T034550Z/runsc-OUT/runsc`):
  SHA256 `7d9c56125b84c00817fdd0173912055bb3314fdea4b33dd09c0910c83762ac13`,
  105378723 字节
- sandboxd 产物(远端 `/data/work/splitfsr-rel-20260928T034550Z/sb/audit/sandboxd`):
  SHA256 `205e5c0a2ef7793a02aafc5a0bf387a4f650d4727f50b5c13b1f7f15187d8704`,
  82086941 字节

## Dockerfile(本目录 node-splitfsr-candidate.Dockerfile)

FROM 上述 digest + 两条纯 COPY:
`runsc → /usr/local/bin/runsc`、`sandboxd → /usr/local/bin/sandboxd`。
只替换这两个文件;0755 来自构建上下文文件(构建前 chmod+stat 核验;
legacy 构建器无 --chmod)。恰两个新文件层,不触碰其他字节。

## 执行配方(build-node-splitfsr-candidate.sh,随本 README 提交)

1. 独占构建目录(noclobber,拒绝已存在;脚本自建,调用方只给父目录);
2. 两个输入产物**先核 SHA256 再**进入上下文(不符即 FATAL),
   复制用 `ionice -c 3 rsync -a --bwlimit=65536` 串行低优先级限速;
3. chmod 0755 + stat 记录;上下文 input-SHA 清单落盘;
4. docker build(记录 manifest/config/层序;验收=基座层前缀+恰 2 个
   新文件层、仅 runsc/sandboxd 两个常规文件、runtime config 与基座
   整体相等**含 SIGRTMIN+3**、两文件 SHA 等于输入);
5. 两个隔离只读无网络容器:`--network none --read-only` 运行
   `runsc --version`(须载 4d3d481d 完整 commit)与 `sandboxd -h`,
   各自记录真实 rc 与容器终态(容器保留不删);
6. 全程真实 rc 独立落盘;失败保留原件另开 attempt。

## 边界

- 只准备配方待根审;**不导入 kind、不部署、不物理测试**;
- frontend/master 角色镜像不动;不带入平台仓任何 gitlink 工作树改动。
