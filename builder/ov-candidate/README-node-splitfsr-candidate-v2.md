# 节点候选镜像 v2(node-splitfsr-candidate-v2)配方

目的:在 rev29 节点镜像之上仅替换 sandboxd,落地组合检查点修复
(components/sandboxd commit 8f43b1cb8aa8da293fdc0decd94893ff7585b653,
产物 SHA 25c36b2a673b0d15901e2dadf450ce9eb8a68ca21e3c494a8db1e9ff16b3d931,
82089013 字节,来自 /data/work/sb-rel-8f43b1cb-run2-20260928T044821Z/build/audit/)。

## 固定输入

- 基座 = rev29 节点镜像完整 digest `akernel-bm1/all-in-one@sha256:75b136c8dd04a6e624eec8f88f6c8847dbbc3f4173a05823c0362080f90f72a1`(runsc 7d9c5612...、SIGRTMIN+3、其余全部字节保持)
- sandboxd = 上述 25c36b2a... 产物

## Dockerfile(node-splitfsr-candidate-v2.Dockerfile,SHA 9d8e93bd...)

FROM 上述 digest + 单条纯 COPY `sandboxd → /usr/local/bin/sandboxd`;0755 来自上下文(先核 SHA、chmod+stat);恰一个新文件层。

## 执行配方(build-node-splitfsr-candidate-v2.sh)

复用已审 v1(41a83e34)骨架,差异:基座 digest 更新;单文件校验(基座前缀+恰 1 层、恰一个常规文件 0755/SHA、runtime config 与基座规范哈希整体相等含 SIGRTMIN+3、manifest 交叉核对、真实 config 从 manifest config.digest 取得);基座 config 规范哈希运行时从实际 inspect 派生;冒烟一个隔离 `--network none --read-only` 容器(`sandboxd -h`,真实 rc+State 含 FinishedAt,容器保留);docker save 用父 shell pipefail 管道;真实构建终止时间以容器/进程 State 为准,包装器时间仅记录;独占目录、Dockerfile/产物 SHA 先核、限速串行复制、全真实 rc。

## 边界

- 仅准备配方待根审;不导入 kind、不部署、不物理测试;frontend/master 不动;gvisor 与旧 sandbox-logger 保持现值;不依赖陈旧 /tmp 指针(路径全部写死绝对路径)。
