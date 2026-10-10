# agent-dx 调度器的最小开发环境

此配置在 BareMetal1 上建立单独的开发容器，不需要 Kubernetes、YuanRong、DataSystem、Kata、Firecracker 或 OpenTelemetry 收集器。它使用一个 Coordinator、一个节点服务、一个 Redis 和 gVisor。构建产物先冻结，镜像构建只安装依赖及校验输入 SHA256，不在部署时重新编译源码。

`builder/scheduler-dev.Dockerfile` 的构建上下文包含 `payload/`、根目录相对路径的 `SHA256SUMS` 和固定 AKernel 提交的 `source/`。`payload/opt/adx/current/` 是经过 `agent-dx/build/release/package.py verify` 验证的完整发布包，`payload/usr/local/bin/` 包含对应 sandboxd、sbox、完整 gVisor 发布包及静态 distill-fs。版本台账记录这些独立组件的完整提交、归档和二进制身份。

运行 `python3 deploy/standalone/scheduler-dev.py --image sha256:<完整镜像身份>` 查看计划；加 `--apply` 执行。启动器只创建 `akernel-agent-dx-dev` 网络与 `akernel-agent-dx-dev-node` 容器，状态位于 `/data/akernel-agent-dx-dev/state`。不自动删除已有容器、物理沙箱或预算数据。

容器限制为 4 核、16 GiB，CPU 绑定范围为 0–7，控制程序与运行时缓存预留 4 GiB。单个节点只通告 3 核、12 GiB 和 16 GiB 磁盘容量，容量文件一天后过期；后续长期实验应增加有界容量续期及宿主总预算控制。此配置目前不支持动态超卖，不能作为高倍率吞吐证明。

主机只在回环地址开放 19443 和 19080，分别映射 HTTPS 控制入口与 HTTP 数据入口。远程 SDK 使用独立 SSH 隧道访问，禁止复用其他实验入口。容器内部服务使用独立网络的网络隔离认证模式；公网入口仍验证管理员 API key。密钥只在首次启动时生成，写入权限为 0600 的持久文件，不进入日志和构建上下文。私有镜像仓库认证由操作员放入持久配置文件，不打进镜像。

首次运行必须验证实际父 cgroup 限制、每个进程的二进制 SHA256、创建与删除回读、命令和文件接口，以及手动暂停恢复的内存和文件状态。已有内存扩展尚未接入 agent-dx 控制面；本配置的启动成功不代表资源调整或预算故障验收通过。

检查点使用 `/home/akernel/sandboxd/root/checkpoints/adx` 的真实持久目录，使节点存储位于 sandboxd 授权的检查点根目录之下。节点请求还必须携带检查点身份、保存可写文件系统的标志，并检查 sandboxd 明确返回完成成功；仅有公共 protobuf 字段兼容不足以证明暂停接口兼容。
