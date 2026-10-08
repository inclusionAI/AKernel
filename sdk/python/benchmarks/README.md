# 性能、压力与复合负载稳定性测试规划

状态：实施中，2026-09-24。原规划源码核对基于 AKernel `521ce87`、ADX
`9ba34e6`；本轮短测使用 AKernel `581c740` 的源码 overlay 和本地派生 ADX 镜像。
本文的规模、持续时间和退化阈值是建议初值，不是已验证的集群容量或产品 SLA。

## 1. 测什么、放在哪一层

以 AKernel 公开 SDK → Edge/API Server → ADX Master/Node Manager → sandboxd/RRT
的真实端到端操作为验收对象。调度库、Redis、网关独立基准用于解释瓶颈，不能替代端到端结果。

| 类型 | 回答的问题 | 负载方式 | 主要结果 |
|---|---|---|---|
| 性能基线 | 相同环境、负载下是否变慢 | 固定活跃 Sandbox 数、请求速率、数据量 | 延迟分布、成功吞吐、阶段耗时、单位操作资源成本 |
| 压力与容量 | 能承载多少，过载后能否恢复 | 阶梯增压、突发、资源耗尽、降压 | SLO 内的最大持续吞吐、拐点、拒绝行为、恢复时间 |
| 复合负载长稳 | 多种业务共存是否泄漏、互相拖慢 | 常驻工作负载与持续创建删除并行，稳定运行数小时 | 各业务尾延迟、资源趋势、完整性、账本与运行态一致性 |
| 带故障长稳 | 出故障时影响多大，恢复后是否收敛 | 在稳定复合负载上逐个注入故障 | 受影响请求/实例、恢复时间、重复执行、残留、错误契约 |

UT、组件测试和功能 E2E 继续独立存在。L0 是快速端到端功能门禁，不重新定义成静态检查；
性能短测、Standalone、Multi-VM、Full Deployment 是不同运行配置，不混成一条递增长流水线。

## 2. 仓库现状与复用边界

| 已有入口 | 实际覆盖 | 需要补齐 |
|---|---|---|
| `sandbox_pressure.py` | 多进程×多线程，完整创建/命令/删除、闭环或固定到达率、失败分阶段统计及 JSON 结果 | 创建/复用分离、分窗统计、进程资源采集、集群级对账门禁 |
| `mixed_pressure.py` | 两个独立负载池：常驻实例命令/文件校验与固定速率创建删除；有界在途数和清理 | 长时资源趋势、故障叠加 |
| `mixed_profile.py` | 七类事务独立到达率与在途上限，支持 interactive、io-heavy、churn-heavy；逐类校验结果和记录延迟 | 正式环境短测、长时资源趋势及故障叠加 |
| `bench_cp.py` | 可选 1 KiB/1 MiB/32 MiB，`copy_from_local` 与 `files.write`；每次验证远端读取、下载及 SHA256，失败退出并记录 JSON | 小文件目录、并发、双端资源与真实环境专项运行 |
| `bench_pty.py` | 对单个常驻实例反复建立 PTY，核对输出、退出码和关闭，分别记录建链、首个匹配输出与完整会话延迟 | 多会话并发、慢消费者和 FD 回收采集 |
| `bench_http.py` | 固定实例和端口，通过公开端口 URL 请求并校验实例专属响应，记录每次请求延迟 | 多目标、跨 worker、keepalive 与 TLS 首包拆分 |
| `bench_tunnel.py` | 固定实例向 SDK 侧 HTTP 服务发请求，校验每次 reverse tunnel 响应 | 真实环境专项运行、重连和并发吞吐 |
| `bench_checkpoint.py` | 通过 RRT Unix Socket 生成 checkpoint，SDK 同 ID reload 后校验文件回滚 | 真实环境专项运行、多次恢复和不同 checkpoint 后端 |
| `../tests/integration/test_sandbox.py` | 命令、文件、三类 PTY、checkpoint/reload/reverse tunnel、可选 OCI 写隔离 | 并发生命周期、常驻实例、压力下网络策略更新、故障与长期资源趋势 |
| `../examples/` 中 CI 选择的 10 个脚本 | basic_usage、stdin、自定义镜像、Dockerfile、命名实例、网络策略、端口转发、PTY、reverse tunnel、存储配额，含实际断言 | 将检查点形成独立用例与逐项报告；不能将 CI 配置中的入口等同于当前镜像已通过 |
| ADX `platform/master/tests/benchmark.rs` | 调度器及资源更新的进程内基准 | 作为定位实验；结果不冠以完整平台创建 QPS |
| ADX `gateway/tests/http_keepalive_bench.py` | HTTP keepalive、并发、多个目标 | 用于定位连接复用与路由热点；AKernel 层补 SDK 端到端测量 |
| AKernel `.github/workflows/ci.yml` | UT、制品契约、Standalone E2E | 独立性能/长稳入口和固定制品输入 |
| ADX `.buildkite/pipeline-full.yml` | 分组功能验收，可选 FC 验收 | 性能/长稳独立步骤或流水线，复用已构建产物 |

压力脚本已把删除纳入成功条件，读取 worker 异常，使用单调时钟和有界直方图，
分别报告尝试与成功吞吐。开环模式记录提交、在途拒绝及发压端错过的到达时隙。
它仍不能单独当容量门禁：尚缺正式制品、隔离多节点负载机、客户端资源采集、
预热/分窗、完整平台指标与产品 SLO。

压测前还需核对部署采集链：Master/Node 已配置 `19090/19091` 指标端口，但不能仅凭端口
认定 Prometheus 已采集。当前 Collector 的 Control 日志路径为 `/opt/adx/run/control/logs`，
与部署的 `/var/lib/adx/logs` 不一致，Control Pod 的采集链也需验证。
CI 的 ADX 服务契约已切换到现有的 `builder/scripts/test_adx_service.py`，Standalone
门禁会分别发现运行时集成目录和 `tests/e2e/standalone/` 公共 SDK 契约目录。
其余采集项仍是实施前置条件。

### 功能前置缺口

实施状态更新（2026-09-24）：功能用例已按 L0、Standalone、Multi-VM、Full
Deployment 分组落在 [`../tests/e2e/`](../tests/e2e/README.md)，并有独立结果汇总与
Redis 前后审计。隔离 standalone 的最新回归为 22/22 组通过，47 项 Python 用例中
5 项按环境条件跳过；随后补跑 OCI 写隔离使该项通过。针对 SWR 上固定 digest 的
专用 OCI 镜像，继承 ENTRYPOINT、私有写层、只读镜像挂载和自定义镜像示例也全部通过。
隔离环境 Redis 前后均无 held 或 Failed+held 资源；这一结果基于未发布的本地
ADX 派生镜像及 AKernel SDK 源码 overlay，不能当作 cn-north-4 已升级验收。
完整证据见本地 `out/e2e/cn4-20260923/final-acceptance.md`。
仍需两节点、S3、GPU 和 Firecracker 等专项环境；真实网络断包和节点故障也需单独编排。
共享 cn-north-4 Redis 的只读复查仍有 10 条 Failed+held；下列短测仅在隔离
standalone 环境执行，尚未启动集群容量门禁。
下表保留原始覆盖目标，具体通过或跳过状态以对应 E2E `summary.json` 为准。

### 隔离 standalone 诊断短测

2026-09-24 的真实 SDK → ADX → sandboxd/runsc 测试，均执行完整创建、`/bin/true`、
删除。每档前后检查 Redis，均无新增 held 或 Failed+held；已完成短测的用例执行
时间累计约 212 秒（不含前述功能 E2E）。日志与结果位于本地
`out/e2e/cn4-20260923/isolated-20260924/perf-*/`，远端对应
`/var/log/akernel-e2e-20260924/perf-*/`。

| 场景 | 时长 | 完成/失败 | 到达与拒绝 | 结论 |
|---|---:|---:|---|---|
| 闭环 1/2/4 线程 | 20/30/30 s | 93/226/355 完成，均 0 失败 | 4.62/7.48/11.72 成功 RPS | 单节点短基线；不是容量 |
| 开环 8 RPS、4 在途 | 20 s | 158/0 | 160 时隙，2 拒绝 | 有轻微在途上限拒绝 |
| 开环 30 RPS、4 在途 | 20 s | 214/0 | 600 时隙，386 拒绝 | 受客户端在途上限保护；不能解释为服务端最大吞吐 |
| 过载后降至 4 RPS | 30 s | 120/0 | 120 时隙，0 拒绝 | 短时降压恢复；无残留 |
| 基础复合负载 | 61 s | 常驻命令/文件 466/0；流转 181/0 | 2 个常驻实例，流转 3 RPS、0 拒绝 | 两类负载并行且无残留；这轮未运行后来新增的七类模型 |

此处开环拒绝是**发压端**达到 `max_inflight` 后不提交的请求，不是平台返回的拒绝码。
日志中出现可恢复的路由查询 HTTP 503 重试，最终生命周期操作无失败。

cn-north-4 共享集群上的专项驱动短测也已执行：文件 1 KiB/1 MiB、两种上传方式
共 4/4 次完成且回读/下载 SHA256 一致；PTY 10/10 次会话完成；固定 SWR digest
上的公开端口 HTTP 请求 20/20 次返回预期内容。另一个独立的跨节点放置用例
2/2 通过。各次 Redis 前后审计均为 `no-new-held`，但共享集群已有 10 条 held
记录，仍不满足正式容量验收前置条件。专项数据经过本机 `kubectl port-forward`
接入，只用于核对测试驱动和真实链路，不能作为生产入口的延迟或吞吐基线。
证据位于 `out/e2e/cn4-20260923/benchmark-smoke-swr/` 与
`out/e2e/cn4-20260923/node-placement-cn4/`。

### 隔离环境长稳与专项回归

两小时混合负载在独立 standalone 中完成 7,200 次流转创建、常驻命令/文件
25,680 次。流转成功 7,195 次、失败 5 次，均为创建返回后首次 `process.list`
遇到路由缓存暂不可用的 HTTP 503；因此该轮结果为**失败**，不能当作长稳门禁通过。
Redis 前后审计无新增资源占用。完整结果见
`out/e2e/cn4-20260923/isolated-20260924/perf-mixed-2h/`。

随后在 AKernel ADX backend 的创建返回前增加有界的只读路由就绪检查；出现
可重试的 409/503 时沿用同一个已创建 Sandbox，等待路由发布，不重复创建。
定向的 Python HTTP OCI 端口用例 20/20 通过，Redis 审计 `no-new-held`；这
尚不能替代两小时负载复测。测试镜像固定为
`swr.cn-north-4.myhuaweicloud.com/openyuanrong/e2e-oci@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b`，
是 Linux/amd64 的 Python HTTP 服务夹具。证据见
`out/e2e/cn4-20260923/isolated-20260924/perf-http-routefix/`。

同一修正源码的 release 优化版派生镜像上，15 分钟复合负载通过：常驻
命令/文件 3,186/3,186，流转创建 1,797/1,797；1,800 个发压时隙中
3 个被客户端 `max_inflight` 拒绝，没有平台失败或清理错误。期间仍出现
短暂路由 503，但由同一请求重试收敛。Redis 审计 `no-new-held` 且
`cluster_clean=true`。证据见
`out/e2e/cn4-20260923/isolated-20260924/perf-mixed-routefix-15m/`。
这是一轮较短的修复回归，原两小时长稳失败仍需在发布制品上重跑。

文件上传/下载与 SHA256 回读、PTY 会话、反向隧道的专项回归分别通过 4/4、
3/3、10/10。RRT Unix socket `/checkpoint` → SDK 同 ID `reload()` → 文件
回滚在优化构建的本地 ADX 派生镜像上通过 1/1，Redis 审计均无新增 held。
这些结果的目录为 `out/e2e/cn4-20260923/isolated-20260924/perf-*-catalog/`
和 `perf-checkpoint-reload-release/`。此前 debug 派生镜像在约 8,700 条
已删除历史记录下不能及时完成节点对账；同源码 release 优化版重启后通过准入
检查。所有派生镜像及 SDK overlay 均未发布，正式包仍需独立回归。

规划时的功能测试尚未完整覆盖公开 SDK。当时 CI 配置运行上述 10 个示例，再单独执行
`SandboxReloadIntegrationTest`；不是默认运行整个 integration 模块。该次 Kubernetes
升级验证运行的是整个模块：6 项通过、1 项 OCI 隔离检查跳过，没有同时执行全部 10 个示例。
因此示例覆盖、UT 覆盖、当前制品实测覆盖必须分别记录。

在单项性能验收之前增加功能覆盖矩阵，逐项标明 SDK 参数/接口、正常/异常/边界断言、
Standalone/Multi-VM/K8s、runtime/镜像、执行入口、制品版本和最近验证证据：

| 功能族 | 现有基础 | 必须补齐或明确实际验证范围 |
|---|---|---|
| 生命周期 | 创建/查询/kill、命名实例示例 | detached 客户端关闭后仍存活、重新连接、重复删除、空闲回收及活动续期、创建超时与未知结果、并发同名/同ID冲突、最终 backend 清理 |
| 命令 | 同步/后台/列举/kill、stdin 示例 | 超时与取消、非零退出码、stderr、EOF、cwd/env覆盖、大输出、重连与进程状态收敛 |
| 文件 | 读写/exists/info及单文件双向复制 | list/depth、mkdir/rename/remove、bytes、目录复制、空文件/大文件、缺失路径、权限错误与写隔离 |
| PTY与网络访问 | PTY交互/resize/退出/互不串流/中断，端口与tunnel示例 | resize实际窗口值、EOF、主动关闭、断线清理、错误鉴权、并发会话、不同入口配置及 internal URL |
| 创建配置 | 自定义OCI、Dockerfile、部分cwd/env与配额示例 | OCI原生entrypoint/WORKDIR、S3 rootfs、OCI/S3 mounts、只读与错误制品、CPU/内存request和limit分别生效、指定节点及调度超时 |
| 网络隔离 | block/DNS/allowlist/动态更新示例 | 新流/既有流、规则冲突与优先级、多实例隔离、集群CIDR默认隔离；后者是实现缺口，不能只补测试便宣称支持 |
| 恢复 | RRT checkpoint、同ID reload、文件回滚与tunnel恢复 | 无checkpoint/过期/损坏、不同恢复存储、并发生命周期、进程退出自动恢复；跨节点策略放ADX专项验证 |
| 认证和资源 | API Key错误401的本次额外检查；SDK资源查询UT | 多租户越权、到期/吊销及缓存窗口、资源账本、真实GPU/NPU与不支持runtime的错误 |
| 多节点与故障 | ADX另有分组验收；本次四节点注册与Control重启 | 通过AKernel部署重跑placement/local-first、容量耗尽与释放、sandboxd/Node重启、worker失联与迟到清理 |

上表列的是待补齐/核验证据的场景，不表示对应能力没有任何UT或ADX底层测试。
AKernel适配后的同一制品仍需要自己的端到端回归。每个准备压测的场景先通过功能前置检查；
不要求等所有专项齐备才写压力驱动，但不能以压力结果代替缺失的功能断言。

## 3. 先统一测试驱动与结果口径

保留现有脚本作为使用入口，逐步抽出小型 Python 驱动；复用真实 SDK，不另写一套调用协议。
当前已落地的脚本与测试如下，其他规划目录仍待实施：

```text
sdk/python/benchmarks/
  sandbox_pressure.py       # 保留已有入口，调用公共驱动
  mixed_pressure.py         # 常驻 IO + 创建删除复合短测
  mixed_profile.py          # 七类事务并行的完整复合模型
  bench_cp.py               # 已补内容校验的文件专项入口
  bench_pty.py              # 已补会话校验的 PTY 专项入口
  bench_http.py             # 已补真实响应校验的端口专项入口
  bench_tunnel.py           # guest 到 SDK 宿主服务的 reverse tunnel 专项入口
  bench_checkpoint.py       # RRT Unix Socket checkpoint/reload 专项入口
  create_throughput/        # Create 到 running 的单节点/集群吞吐与清理审计
  direct_command/           # 路由健康和 Direct Command 分层吞吐
  harness/load.py           # 固定到达率和有界在途调度
  harness/stats.py          # 有界延迟直方图
  tests/                    # 驱动自身的错误传播、清理和统计测试
```

驱动单测随 `make sdk-test` / `make sdk-check` 运行，使用仓库已有的 `unittest`。
真实短测在独立 standalone 环境中执行，示例：

```bash
cd sdk/python
python3 -m benchmarks.sandbox_pressure --processes 1 --threads 4 \
  --duration 30 --target-rps 4 --output out/performance/lifecycle.json
python3 -m benchmarks.mixed_pressure --duration 60 --residents 2 \
  --churn-rps 1 --output out/performance/mixed.json
python3 -m benchmarks.mixed_profile --profile interactive --duration 60 \
  --target-rps 2 --image "$AKERNEL_TEST_HTTP_IMAGE" \
  --output out/performance/mixed-interactive.json
python3 -m benchmarks.bench_cp --sizes 1024,1048576 --iterations 1 \
  --output out/performance/file.json
python3 -m benchmarks.bench_pty --iterations 20 \
  --output out/performance/pty.json
python3 -m benchmarks.bench_http --image "$AKERNEL_TEST_HTTP_IMAGE" \
  --iterations 20 --output out/performance/http.json
python3 -m benchmarks.bench_tunnel --iterations 10 \
  --output out/performance/tunnel.json
python3 -m benchmarks.bench_checkpoint --image "$AKERNEL_TEST_HTTP_IMAGE" \
  --iterations 1 --output out/performance/checkpoint.json
```

这些命令要求已配置 AKernel SDK 的地址和 API Key；`bench_http.py` 还要求镜像
内有 Python 3 HTTP server。cn-north-4 可设置
`AKERNEL_TEST_HTTP_IMAGE=swr.cn-north-4.myhuaweicloud.com/openyuanrong/e2e-oci@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b`。
该镜像已通过实际端口转发功能 E2E（1/1），专项性能入口仍需单独运行。
正式门禁必须同时保存环境清单、Redis 前后审计和资源采样。
完整复合模型要求该镜像提供 Python HTTP server 和 RRT checkpoint 所需的
Python 标准库，并要求对应运行时的 checkpoint/reload 已单独通过功能验收。
驱动把命令、HTTP、文件、生命周期、PTY、reverse tunnel、checkpoint
分别记为逻辑事务；每类独立发压与计数。任何类别无提交或有失败，都使整轮失败。
`io-heavy` 把文件及 checkpoint 状态数据提高到 1 MiB，并使命令输出
64 KiB；`churn-heavy` 提高创建删除事务权重。三档均保留七类事务。
运行结果仍需配合部署侧 Redis 前后审计，不能只凭驱动的清理返回判定资源释放。
首次隔离 standalone 试跑于 2026-09-24 停在夹具准备阶段，状态为
`driver_error`，七类事务未开始：sandboxd 拉取私有 SWR 镜像时缺少凭证，
SDK 随后对同一创建请求重试并收到 `environment already exists`。补齐拉取
凭证后的短测中，双 Sandbox 同端口路由用例 2/2 通过，但混合负载准备
自定义端口的反向隧道时再次停住：API Server 返回的 WebSocket 路径仍走
默认端口，导致 HTTP 502 和 60 秒连接超时。两次失败均未产生七类事务统计。
API Server 修正路径后，用未发布的本地派生镜像跑通 60 秒 `interactive`：
命令 19、HTTP 12、文件 9、生命周期 9、PTY 6、反向隧道 3、checkpoint 3，
全部成功。随后在同一镜像上分别跑通约 60 秒的 `io-heavy` 和 `churn-heavy`：
前者命令 9、HTTP 9、文件 19、生命周期 6、PTY 6、反向隧道 3、checkpoint 9；
后者命令 12、HTTP 9、文件 6、生命周期 22、PTY 3、反向隧道 3、checkpoint 6。
三档各 61/61 成功、无在途拒绝或清理错误；各轮 Redis 前后均无 held 记录。
第二次运行结束后测试容器已移除，临时 SWR 拉取凭证已恢复。
三档的 PTY 完整会话均约 10.26 秒；这轮仅证明操作成功，不能把该数值
当作正常 PTY 性能基线，需单独拆分建链、首个输出与退出等待阶段定位。
这些短测不替代先前失败的两小时长稳门禁。驱动 5 项单测和 SDK 全套检查通过。
日志分别在本地 `out/e2e/cn4-20260923/isolated-20260924/` 下的
`perf-mixed-profile-60/`、`perf-mixed-profile-auth/` 和
`perf-mixed-profile-tunnel-port/`、`perf-mixed-profile-io-churn/`。

同日追加的三项功能门禁分别验证 guest 内 PTY 窗口尺寸、活动续期后空闲删除、同端口
实例删除后的路由撤销与存活实例可访问性；隔离 standalone 定向运行各 1/1 通过，
Redis 审计无新增占用且集群账本无 held。证据在
`out/e2e/cn4-20260923/isolated-20260924/functional-coverage-0924/`；仍使用
未发布的本地派生镜像，不能代替正式包回归。

必要契约：

1. 一个 run ID、固定随机种子、固定版本与环境清单。记录 SDK/wheel 哈希、AKernel/ADX 提交、
   sandboxd/runtime 版本、镜像 digest、内核、节点资源、部署模式、Redis AOF、认证和 Trace 采样配置。
   正式发布包与本地 overlay 结果分别标记，不能混为同一基线。
2. 使用单调时钟。分别记录 SDK 创建返回、首次命令成功、执行、checkpoint、reload 后首次成功、
   删除返回、实际资源释放；完整生命周期到清理确认才结束。API 释放与物理释放各有时间界限。
3. 同时支持闭环并发和开环固定到达率。开环记录计划发送、实际发送、完成时间及未发出的请求；
   压测端排队、丢弃和超载不能隐藏。晚到请求的等待计入用户观察延迟，避免漏计拥塞时的等待。
4. 报告 offered/started/completed/succeeded/failed/timed-out/canceled/outcome-unknown；
   同时输出成功请求延迟和失败/超时耗时，不能用成功 P99 隐藏失败。逻辑操作和重试次数分开。
5. SDK 自带重试不关闭也不隐瞒，记录其配置；驱动默认不额外重试写操作。结果未知按既有身份
   查询/收敛，不能悄悄新建另一个实例补成功数。无法追踪未知结果时判定验收证据不足。
6. 固定大小直方图和有界错误样本，按分钟落盘；不把全量请求或 Sandbox ID 放进 Prometheus 标签。
   Request ID、实例 ID、generation、错误详情写日志/Trace 关联。
7. 明确 max-inflight、max-live-sandboxes、数据与磁盘预算、总时间和 drain 时间。
   中止后停止发压，排空或标记在途操作，并仅清理本次运行拥有的资源；清理失败使运行失败。
8. 每次发压前验证压测机余量。CPU、FD、连接池、网卡或目标回显服务达到瓶颈时，标记
   `load_generator_limited`，此结果不能用于宣称平台容量。

产物至少包括 `manifest.json`、每分钟 `windows.jsonl`、`summary.json`、JUnit、错误样本、
资源/账本前后快照、相关 Trace/日志引用、清理结果。阈值判定和事实数据同时保留。

## 4. 性能基线：先拆开单项，再组合

下表每行是场景族，不把矩阵的每种参数组合都塞进快速门禁。

| 场景 | 主要变量 | 必须测量与断言 |
|---|---|---|
| 创建/删除 | runsc；Local-first 本地命中、fallback、中心调度 | 创建返回与可执行延迟；删除及物理回收；无重复 backend、资源账本收敛 |
| 镜像启动 | 默认本地 EROFS、自定义 OCI 冷缓存/热缓存 | rootfs 准备与启动成本；镜像拉取独立计时；不在混合结果中掩盖缓存状态 |
| 常驻实例命令 | 短命令、长命令、stdin、输出量 1 KiB/64 KiB/1 MiB | 每操作 P50/P95/P99、首字节、完整结束；退出码与完整内容 |
| 文件 | 上传/下载、1 KiB/1 MiB/32 MiB、千个小文件目录 | MiB/s、操作延迟、文件数、哈希；发送端/接收端资源；配额边界单列 |
| PTY | 常驻会话、反复开关、慢消费者、交互小包 | 建链/回显延迟、会话容量、输出完整性、关闭后 FD/任务释放 |
| 端口转发 | 短连接/keepalive、1/多目标、本机/跨 worker | 请求与字节吞吐、TLS 建连/首包/稳态分别计时；目标身份正确 |
| Reverse tunnel | 建链/常驻传输/重连、多 Sandbox 共存 | 建链与恢复时间、吞吐、连接数量、无串流/串实例 |
| 查询与目录 | 100/1,000/10,000 元数据规模分档，实际规模受环境容量限制 | `get_info`/资源查询延迟；目录增量发布滞后；热路径不退化成每请求全量同步 |
| checkpoint/reload | runsc/FC 分开；不同有效数据量；有/无前台 I/O | RRT Unix socket checkpoint → SDK reload；持久化完成、文件回滚、恢复后首次请求成功 |
| 网络策略 | 创建时策略、运行时更新、规则数/连接数 | 更新延迟与吞吐影响；新流和既有流按协议分别断言；平台 CIDR 隔离待实现后补必测 |
| 镜像入口与挂载 | 默认环境、继承 entrypoint、mounts、不同 rootfs | 启动就绪、入口退出、挂载可见性和不同实例写隔离 |
| 资源/调度干扰 | 混合 CPU/内存、亲和规则、后台资源上报 | 满足约束、无超分配；本地准入与中心排队分别统计，不能假设本地创建经过全局评分 |

首轮优先：创建/删除、常驻命令、文件、PTY、端口转发、reverse tunnel。
checkpoint/reload 与网络策略列入专项；GPU/NPU 独立设备环境，不能以模拟卡算性能验收。
AKernel 没有公开的 ADX pause/resume、快照目录接口不伪造为 AKernel SDK 用例，放 ADX 专项。

建议采样：预热 2 分钟、测量 5–10 分钟、交替运行基线/候选至少 3 轮。并发从 1/4/16 开始，
满足预算再增加 32/64/128；不同操作使用不同并发上限。样本少于 1,000 的档位将 P99 标成
低置信度，增加采样后再判定，不能仅拉长慢操作压力而突破资源上限。

冷缓存只在独立测试环境构造并记录缓存范围，不清理共享集群镜像缓存。压测镜像预装工具，
测量期间不执行 apt/pip 安装，也不把外部公网下载速度混进平台启动基线。

## 5. 压力与容量：找到可持续区间

每类负载先找自己的容量，不定义一个适用于所有场景的“集群 QPS”。

| 实验 | 发压方式 | 通过条件 |
|---|---|---|
| 创建阶梯 | 闭环并发逐档提高，每档稳定 5–10 分钟 | 成功/失败吞吐、尾延迟、队列和资源拐点可解释，清理债务不增长 |
| 固定到达率 | 按初测可持续速率的 30/50/70/90/110% 开环发压 | 记录未发出、排队、拒绝和超时；过载不造成重复执行或账本错误 |
| 活跃容量 | 预创建并保持 Sandbox，逐步接近资源预算 | 区分存活实例数、资源预留、实际 CPU/RSS；新建受控拒绝，原有实例仍可服务 |
| 数据面压力 | 固定实例池，提高命令、文件、HTTP 或会话并发 | 不让创建耗时掩盖 Edge/Proxy/RRT 的性能；明确客户端是否成为瓶颈 |
| 突发/降压 | 稳态速率短时升到 2–3 倍后回落 | 积压能排空、拒绝有边界、成功吞吐恢复；恢复时间单独输出 |
| 清理压力 | 批量终止、客户端退出、请求结果未知 | 限额内资源最终释放，不存在后台删除失败却显示测试成功 |

标量资源理论上限仅用作发压护栏：对满足 runtime/放置约束的每个节点，计算
`min(floor(可分配CPU/每实例请求CPU), floor(可分配内存/每实例请求内存), 其他限制)` 后求和。
GPU/磁盘/端口/进程预算还会降低上限。实际内存与 limit 可能高于 request，不能拿账本容量
当实际安全容量；另设主机 RSS、CPU、磁盘、inode、PID、FD、连接跟踪表护栏。

初始停止规则：出现非注入 OOM、数据串写、重复运行代次、不可回收资源立即停止增压；
内存/磁盘/FD 到达预设预算则停止发送并排空。过载返回与系统错误分别分类，不能全部计为通过。

## 6. 复合负载长稳：固定背景池与持续流转并行

先运行 2 小时，再升级到 8/24 小时；72 小时用于重要版本候选。
常驻池初始使用约 40% 的安全可分配资源，流转池最多再用 20%，保留恢复和波动空间。
这里是资源占用目标，不是 CPU 利用率或请求占比。节点压力和 runtime 开销另设限制。

建议第一套混合模型如下。权重表示被调度的**逻辑事务次数**，不是字节量、连接数或耗时比例。
每类有独立速率及并发上限，避免大文件操作或 reload 占满所有工作线程。

| 事务 | 权重 | 行为与正确性 |
|---|---:|---|
| 短命令 | 30% | 常驻 Sandbox 运行带唯一序号的小命令，检查 stdout/exit code |
| HTTP 端口访问 | 20% | keepalive 混合新连接，返回实例标识和内容校验值 |
| 文件操作 | 15% | 写/读/复制/删，混合小文件和大文件，核对哈希 |
| 生命周期流转 | 15% | 创建、执行、保持可配置时间、删除、确认释放 |
| PTY | 10% | 常驻与短会话并行，含慢消费者，核对交互序列和退出 |
| Reverse tunnel | 5% | 请求受控回显服务、核对流隔离和关闭回收 |
| checkpoint/reload | 5% | 独立实例子池，先写 A、checkpoint、写 B、reload 后验证 A 和链路 |

同一个 Sandbox 的写入/恢复事务由驱动协调，避免把测试自身的并发写当数据损坏。
故意制造的同实例并发生命周期冲突另设场景，不混进普通业务成功率。
checkpoint 未通过功能前置检查时，对应 profile 判定未满足依赖，不能静默移除后仍称完整混合通过。

至少三套 profile：

- `interactive`：上表模型，测用户交互与生命周期互相影响。
- `io-heavy`：提高文件、输出和 checkpoint 数据量，验证网络/磁盘争用、日志滚动压缩及缓存回收。
- `churn-heavy`：提高创建删除、查询、短连接、目录更新，验证控制面、认证缓存、Redis 和路由同步。

另外以真实实例工作负载控制 CPU duty cycle、内存 working set 和磁盘 I/O，区分 request、limit
与实际使用；只让 `/bin/true` 循环不代表高资源利用率。流转池按 Little's Law 估算
`平均存活数 ≈ 创建到达率 × 平均存活时间`，长命任务必须有实例数量上限，避免无意无限增长。

按小时比较各类操作的 P99、成功率、RSS/FD/PID/连接数、路由与快照记录、磁盘/日志空间。
每轮 drain 后检查回到相同空闲条件，区分预期缓存增长与持续泄漏。运行时长和完成事务数都达标
才判长稳完成；空转 24 小时不算 24 小时稳定性验收。

## 7. 稳态叠加故障

先通过无故障混合，再每次注入一种故障，并保留故障前、故障窗口和恢复窗口三个独立统计段。
故障按目标及 run ID 白名单执行，仅作用测试部署，不注入现有共享业务节点。

| 故障 | 验证目标 |
|---|---|
| sandboxd 重启/暂不可用 | 停止新准入，区分 backend 保留与丢失；按 restart policy 恢复，禁止无限重启 |
| Node Manager 短时重启 | 心跳期限内恢复，对账完成后准入；验证 backend、归属与资源无重复 |
| Node Manager 长时间不恢复/worker 失联 | 失效、撤路由、恢复策略；无 checkpoint/local-only 明确失败，共享 checkpoint 兼容时恢复同 ID；迟到节点清理旧执行 |
| Master/Redis 重启 | 认证、队列和新建受影响范围清晰；AOF 恢复、epoch/归属一致；降级结果按契约收敛 |
| API Server/Edge/Proxy 重启 | 目录重建、连接恢复、写请求结果未知处理；会话能否恢复按实际协议判定，不能要求透明恢复所有 TCP |
| 可控网络延迟/丢包/短暂中断 | deadline、重试上限、连接重建；故障影响不扩散到健康 worker |
| Collector 后端不可用 | 业务不被采集拖住，队列/丢弃可见；恢复后持续采集，日志轮转不耗尽磁盘 |

每种先做一次确定性验收，再在 8/24 小时运行中循环，建议起步 10 个恢复周期。
恢复时间的界限由心跳、RPC deadline、启动/就绪预算和试验 SLO 推导并在 profile 中固定，
不把所有故障统一写成任意的“30 秒恢复”。故障窗口允许的错误码和结果必须事先声明，
健康节点流量的错误与恢复窗口超时仍算失败。

## 8. 环境、采集与判定

### 环境

| 环境 | 拓扑与用途 | 边界 |
|---|---|---|
| 本地 Standalone | 单 Linux 主机；小规模场景与驱动正确性 | 不能发布跨宿主容量结论；Mac/Lima 数据单列 |
| Multi-VM | 控制 VM + 两个 worker VM，负载机尽量独立 | 验证跨节点与故障；同物理机的 VM 不能代表物理机故障隔离 |
| 可比较的容量基线 | 独立 control/Redis 主机 + 至少两 worker + 独立 load generator，即至少四台主机/VM | 固定实例类型、CPU/内存限额、磁盘和网卡；压测机和 worker 不争抢宿主资源 |
| 扩展基线 | worker 1→2→4，固定控制资源；再单独改变控制资源 | 一次只改一个变量，判断扩展效率和中心瓶颈 |
| FC/GPU/NPU | 专项 worker 和固定 runtime/驱动 | FC 需要可用 KVM；设备测试需要真实设备；不与 runsc 混算容量 |

刚完成验收的 cn-north-4 四-worker 部署可用于分布式实验，但并不自动等于隔离后的性能实验场。
必须核对 control、worker、load generator、采集服务的实际宿主放置与其他租户负载。
正式发压从集群内独立负载机/Pod 直连 Service 或正式入口，不能经过 `kubectl port-forward`。
直连 Service 基线和包含外部 LB 的用户路径分别报告。每档环境先通过功能 E2E。

记录 Linux 发行版和精确内核、cgroup、runtime/KVM版本、磁盘、CNI/MTU和资源限制。
本规划不凭经验新增统一内核最低版本；按固定 sandboxd/runtime 的已验证组合预检。
FC 与共享 checkpoint 兼容性必须实测；已知 FC 双克隆问题未关闭时，不宣称该场景通过。

### 采集

- 客户端：逻辑操作/尝试、分阶段时间、错误码/retry/outcome、字节和数据校验；压测端自身资源。
- 平台：API 延迟与错误、排队/准入/启动、Local-first 命中与 fallback、watch revision/重同步、
  各状态实例数、请求/预留/可分配资源、Redis 命令/连接/内存/AOF延迟、降级日志待同步量。
- 数据面/后端：活跃连接、流量/背压、RRT执行和文件/PTY、sandboxd启动/恢复、runtime RSS、
  CPU、FD、线程/进程、网络设备、磁盘/inode、镜像与checkpoint缓存。
- 日志/Trace：按 run ID 关联，固定采样率，记录导出队列丢弃、轮转/压缩/清理成本。
  基线固定可观测配置；专门做开/关采集 A/B 才讨论采集开销，不把两种配置混比。

以上是所需指标清单，不代表均已接入。预检逐项确认采集目标 `up`、标签、计数变化及日志可查；
缺失关键证据的结果标为不完整。优先补采集接线，不擅自重做已有 Grafana 仪表盘。

### 初始判定

- 所有运行：数据错乱、跨实例串流、重复有效归属、不可解释账本差额、清理失败一律失败。
- 固定容量内短基线：功能错误为零；记录实际样本数。与同环境基线比较，吞吐下降超过 10%
  或 P99 上升超过 20% 先告警，重复配对试验确认后才阻断；这些阈值需用噪声数据校准。
- 正式容量：同时满足明确操作 SLO、错误预算、无持续积压、无资源越界的最大持续负载。
  产品尚无统一绝对延迟目标，先产出容量曲线再决定 SLO，不能仅凭“未崩溃”验收。
- 无故障长稳：建议可用性目标 99.9% 起步，另设数据完整性和清理错误为零；每分钟/小时报告，
  不能靠全天均值掩盖连续失败。业务 SLO 未确认前，这是评估目标而非既定承诺。
- 泄漏：对相同负载与 drain 后的 RSS、FD、进程、连接、磁盘建立有界区间，检查持续增长斜率；
  不能要求 OS page cache 完全归零，也不能只比较两个偶然时刻。
- 运行结果区分 passed/failed/incomplete/unsupported/canceled；环境缺能力的专项不计为通过。

## 9. 流水线与实施次序

| 入口 | 建议频率/时长 | 是否阻断 |
|---|---|---|
| 基础出包 + L0 | 每次提交；保留原有快速功能门禁 | 功能、驱动UT、制品契约阻断；不加入长稳 |
| `perf-smoke`（拟新增） | 候选制品触发，5–10 分钟，少量核心场景 | 正确性/清理阻断；初期性能只告警 |
| `perf-baseline`（拟新增） | 夜间或手动，约30–90分钟 | 固定环境建立历史基线，噪声确认后启用回归门槛 |
| `capacity`（拟新增） | 手动或周度，约1–2小时 | 生成容量曲线，不在普通共享CI自动压满机器 |
| `mixed-soak`（拟新增） | 夜间2/8小时，周度24小时，候选版本72小时 | 对应级别发布验收；独立资源和超时 |
| `fault-soak`（拟新增） | 专用环境，先单故障后长稳 | 故障恢复契约阻断；与普通full功能验收分开 |

AKernel 的源码CI在 GitHub Actions，ADX full 在 Buildkite。场景驱动留在 AKernel 仓库，
两端CI都调用同一入口并消费同一份不可变镜像/SDK；不在压力Job重新编译产品。
新的定时计划和流水线均尚未创建。服务升级与发压分步记录，失败保留部署、用例和资源证据。

实施先做功能前置阶段，再进入五个可独立评审/提交的性能阶段：

0. **功能覆盖矩阵与回归**：把上述接口和创建参数映射到带断言的真实E2E；
   优先生命周期、命令/文件、网络隔离、配置覆盖，再补恢复、跨节点和设备专项。
   复用10个示例中的验证逻辑，拆成能单独选择与报告的用例，不以脚本数量作为覆盖率。

1. **驱动与证据**：修复当前压力脚本统计/清理；公共计时与有界输出、run ID、预检；
   补驱动UT（线程异常、删除失败、时钟、未知结果、停止清理），修正采集接线和失效CI引用。
   验收：真实集群小并发创建/命令/删除有完整结果与无残留证明。
2. **单项基线**：六个首轮场景与冷/热缓存、runsc profile；加入 perf-smoke 和配对报告。
   验收：同镜像重复运行稳定，客户端不会先饱和，可定位主要阶段耗时。
3. **压力/容量**：开环、阶梯、突发、活跃实例容量、单/双/四worker曲线。
   验收：得到SLO内容量、过载行为和降压后的资源回收证据。
4. **复合负载**：三套混合profile、分池/事务协调、2小时→8/24小时、日志与缓存回收。
   验收：按业务分别判定成功率/延迟；完整性、账本、后端与资源清理一致。
5. **恢复与发布**：逐项故障叠加、FC专项、候选版本72小时；加入独立CI入口和发布报告。
   验收：故障矩阵明确成功/缺口，无checkpoint和不兼容恢复不得用冷启动伪装成功。

执行顺序先证明测试驱动可信，再测平台容量。此前的6项SDK通过只是功能验收证据，
不构成此计划的性能、容量或长稳结果。
