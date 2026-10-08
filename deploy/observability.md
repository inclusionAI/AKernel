# ADX 可观测接入

AKernel 使用 ADX 原生指标和链路埋点；部署层负责配置 Collector 和后端地址。
SDK 的调用方式、Token 和服务入口不变。

## 采集链路

| 位置 | 采集方式 | 数据来源 |
| --- | --- | --- |
| node Pod | 现有 `otel_collector.service` | 本机 adxlet `127.0.0.1:19091/metrics`、内嵌 Relay `127.0.0.1:18443/metrics`、组件文件日志、runtime 输出、OTLP |
| Coordinator Pod | 本地 Collector sidecar | `127.0.0.1:19090/metrics`、共享日志目录、OTLP |
| Ingress/API Server Pod | 本地 Collector sidecar | Ingress `127.0.0.1:18080/metrics`、两组件共享日志目录、OTLP |

节点 Collector 与 sandboxd、adxlet 位于现有 node 容器内，由 systemd 管理。
Relay 的采集入口保持本机访问。组件的 CPU、内存、FD、线程等指标优先使用
ADX 原生进程指标（当前为 Coordinator 和 adxlet）；`hostmetrics/process` 补充 sandboxd、adxctl、Redis 和
Collector 等节点进程。保留 sandboxd、DistillFS 和 runsc 的原有采集链路。
进程命令行在导出前删除，避免将参数作为指标标签。
API Server 没有独立 Prometheus 端点，其日志和 trace 由网关 Pod 的 Collector 采集。
当前 Ingress 的端点只有请求、路由和连接指标，没有独立进程 CPU/内存/FD 指标；
内嵌 Relay 与 adxlet 共进程，进程资源计入 adxlet，不重复作为 Relay 进程计算。
因此进程看板只展示实际存在的组件序列，不表示每个逻辑组件都有独立进程数据。

Collector 将指标 remote-write 到 Prometheus，将日志 OTLP/HTTP 写入 Loki，
将 trace OTLP/gRPC 写入 Tempo。组件的 trace 先发到同 Pod 的 Collector；
Execd 使用节点 Pod IP 的 Collector HTTP 端口。新增部署不需要 metrics Service
或集群独立 Collector Deployment。控制 Pod 原先没有 Collector，文件日志采集
需要本地 sidecar 和共享日志卷；node 的容器结构保持不变。

## 配置

继续使用现有 Helm `monitoring` 参数，例如：

```yaml
monitoring:
  akernelEnv: production
  prometheusEndpoint: prometheus.akernel-monitor.svc.cluster.local:9090
  lokiEndpoint: loki.akernel-monitor.svc.cluster.local:3100
  tempoEndpoint: tempo.akernel-monitor.svc.cluster.local:4317
```

配置 `tempoEndpoint` 后，ADX 组件与 Execd 启用 trace，默认采样率为 0.1。
未配置该字段时不启用新增 ADX trace 导出。节点 Collector 启动包装器从容器
PID 1 环境中读取监控地址和 Pod/Node 标识，解决 systemd 不自动继承容器环境
的问题；不读取 Redis 密码、API Key 等凭据。

指标抓取周期为 15 秒，Collector 批处理最长等待 5 秒。指标使用 `adx_env`、
`component_name` 和 Kubernetes 身份属性；原有 `akernel_env` 标签保留。
Loki 将 `adx_env` 和 `component_name` 作为索引标签，实例 ID 保留在日志内容
或结构化元数据中，避免为每个实例建立独立索引标签。

## 日志

- node：`/home/akernel/adx/run/node/logs/*.log`。
- Coordinator：`/home/akernel/adx/run/coordinator/logs/*.log`。
- 网关：`/home/akernel/adx/run/ingress-api/logs/*.log`。
- runtime 主进程 stdout/stderr：`/home/akernel/adx/run/node/logs/runtime/*.out`、`*.err`。

ADX 组件在本地输出 JSON；adxctl 保留完整日志行、执行滚动和压缩。
Collector 将 ADX 的 `fields.message`（缺少时使用 `fields.event`）提取为字符串正文，将 level、target、业务字段
及 Trace/操作/Environment ID 放入 Loki 结构化元数据，并使用原始日志时间和级别。
Explore 普通查询直接显示消息，不需要 `json | line_format`。普通文本、损坏 JSON
及既无消息也无事件名的 JSON 保留原正文；已有历史日志不会被重写。
Schedule 的沙箱日志搜索面板只查询 `adx-runtime` 主进程 stdout/stderr，保留
原文，支持环境、沙箱运行记录多选及关键词搜索；不显示控制面组件日志。
日志面板默认独立查看最近 6 小时，绝对时间选择仍生效；近期没有主进程输出时为空是正常情况。
运行记录下拉候选来自所选时间范围内的原生资源采样 `runtime_id` 标签，
可搜索 Sandbox ID 选择对应运行记录；缺少采样的运行记录通过 All 查看。
关键词按原文包含关系匹配，区分大小写，留空不限制。运行 ID 仍为 Loki 结构化元数据，
不增加逐实例索引；控制面日志仍可在 Loki Explore 查询。
Collector 从文件采集完整记录并跟踪偏移，包含未压缩的滚动文件，不重复扫描
压缩归档。单条组件日志上限为 64 KiB，压缩延迟为 300 秒。
runtime 主进程输出与通过 SDK 返回的 command stdout/stderr 是不同数据流；
不能把 runtime 日志目录当作所有 SDK 命令结果的归档。

节点读取偏移保存在 `/home/akernel/otel/state`，跟随现有节点数据盘；控制 Pod
的日志和偏移使用 `emptyDir`，Pod 删除后不保留本地副本。Collector 停止期间
日志仍受本地滚动保留上限约束，超过保留窗口后不能保证补采。

## Grafana

三份看板同步自 ADX 子仓的
`build/observability/grafana/dashboards/`；AKernel 保持相同查询表达式：

- `adx-schedule`：CPU/内存/磁盘容量、预留、可用和分配率，实例数量与分布、队列、节点健康、沙箱日志搜索和 Trace。
- `adx-data-plane`：Ingress/Relay 数据面。
- `adx-process-resources`：组件 CPU、内存、FD、线程。

Schedule 增加“接口请求链路”列表，只查询 API Server/Ingress 的 HTTP 方法和接口模板 Span，避开
健康探测及其他非 HTTP Trace。列表只展示开始时间、接口、实际 Span 组件、响应状态、Span 耗时和 Trace ID 六列，
过滤未识别的 `/unmatched` 接口及健康探测，不依赖上游根 Span 存在；点击 Trace ID 查看整条链路。
表格每行是一个匹配的 HTTP Span，同一 Trace 的不同处理阶段可以出现多行。
Span 耗时是当前 HTTP 处理阶段的时间；流式响应可能先返回，后台创建任务仍继续执行，
不能将其当作 SDK 创建完成总耗时。完整链路时长与阶段分解通过 Trace ID 打开查看。
新版 HTTP Span 用 `方法 + 接口模板` 命名，并包含
`http.request.method`、`http.route`，Ingress/API Server 另有响应状态属性；
Execd `/invoke` 通过 `rpc.method` 区分规范化操作，不采集请求正文或命令内容。
Loki 的 `trace_id` 元数据字段提供 Tempo 跳转；历史 Span 保留历史名称。

### Explore 查看接口链路

ADX Schedule 顶部的 **Explore 接口查询** 直接打开原生 Tempo Explore，带入当前 `adx_env`，
默认查看最近 6 小时、50 条 Trace，每条最多显示 3 个匹配 Span；需要时在 Search Options 调整。
预设使用 **Table Format → Spans**，选出接口名 `Name`、组件 `service.name`、
`http.response.status_code` 和 Execd 的 `rpc.method`，不显示 Traces 表的 `nested` JSON。
点击 **Span ID** 打开完整链路和对应 Span，可继续修改 TraceQL 或时间范围。

手工进入 Explore 时，选择 Tempo → TraceQL，使用下列查询，并在 Search Options 将 Table Format 改为 Spans：

```traceql
{ resource.adx_env = "akernel-cce" && name =~ "^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|CONNECT|OTHER) /.*"
  && span.http.route != "/unmatched" && name != "GET /healthz" && name != "GET /metrics" }
| select(name, resource.service.name, span.http.response.status_code, span.rpc.method)
```

`Name` 是实际匹配 Span 的接口名；`Trace Name` / `Trace Service` 仍是整条链路的根 Span 信息。
当前原生 Explore 没有与看板等价的列隐藏设置，查询入口不会修改全局默认表格式。
上游注入 `traceparent` 但没有导出父 Span 时，根信息会显示 `<root span not yet received>`；
这是未上报父 Span 的诊断提示，不能通过改名或伪造根 Span 消除，仍可从 Name 和 service.name 查看接口。
单行 Duration 为 Span 耗时，流式响应接口应打开完整链路分析创建总耗时。

monitor chart 自动挂载并 provision 这些 JSON。旧 AKernel Schedule
（UID `yuanrong-complete-monitoring`）已移除，使用 ADX Schedule 查看调度与资源。数据源 UID 应为
`prometheus`、`loki` 和 `tempo`，用 `adx_env` 选择部署环境。
Schedule 使用 Coordinator 原生 `adx_coordinator_environments` 统计 Running。
已有 `sandbox_running` 兼容 recording rule 仍保留供外部查询使用。
磁盘面板展示资源源上报的调度账本，不是文件系统实际占用。资源分配量读取
`reserved`，不用容量减可用量推导；维护/压力保护会使可用量为零。节点和
沙箱的实际使用量归各自的资源/详情看板，Schedule 聚焦调度账本。

更新 ADX 时，从子仓重新复制上述三个文件，并核对新的指标和标签契约。

## 验证

```bash
python3 -m unittest discover -s deploy/akernel/charts/core/tests -v
helm lint deploy/akernel/charts/core
helm lint deploy/akernel/charts/monitor

# 使用部署内的真实 Collector，验证日志正文与元数据（只创建隔离临时目录）
python3 deploy/tests/observability/collector_log_contract.py \
  --kubeconfig /path/to/kubeconfig --pod <node-pod> --namespace akernel
```

部署后必须实际验证 Prometheus 指标的节点标签和实例数量/资源变化、Loki 日志、
Tempo trace、Grafana provisioning，以及 SDK 创建、命令和删除。
Collector 配置校验或 Helm 渲染通过不能代替后端接收和真实 SDK 验收。

本次集群部署与验收记录见 [2026-10-08 验收记录](observability-validation-20261008.md)。
