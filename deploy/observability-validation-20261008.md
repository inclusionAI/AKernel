# ADX 可观测集群验收（2026-10-08）

本文保留各轮验收历史；当前部署与最新结果见文末“Explore 接口查询验收”。

## 部署身份

- 集群：cn-north-4 `akernel-cce`；业务命名空间 `akernel`，监控命名空间 `akernel-monitor`。
- ADX：`4d6d3f1bb88cac82fc3b0bb3e37df45fcea84d7d`。
- sandboxd：沿用 AKernel 固定的 `31d0749a85e269396cfc1c469c948345f9610906`，未修改源码。
- Collector：`otelcol-contrib 0.120.1`，两份配置使用该版本实际校验。
- 最终镜像：`swr.cn-north-4.myhuaweicloud.com/openyuanrong/cluster-all-in-one:pr78-otel-4d6d3f1-20261008-final@sha256:c6264081665149a450a9976fc59a089bbcdbc303ecbebb9376a58b6b3a78414a`。

镜像以集群已有不可变镜像为基础，替换 OBS 下载并校验的 ADX 发布包、Execd、
AKernel 自己生成的 runtime rootfs 和本次 Collector 配置；其余执行后端沿用基础镜像。
本轮没有从头重新编译完整 AKernel 的所有执行后端，也没有执行完整运行时功能矩阵。

最终 Helm 发布：`akernel-core` revision 92、`akernel-monitor` revision 21。
Coordinator/Ingress Pod 均 2/2 Ready，4 个 node Pod 均 1/1 Ready；本次滚动后的业务 Pod 无重启。

## 验收方法

通过现有 AKernel SDK 和既有 API/数据入口访问，分别在四个节点上创建 runsc
Sandbox（CPU 500m、内存 256 MiB），执行命令、文件写入/读取，最后删除。
客户端为请求注入 sampled W3C traceparent，以可重复检查同一条实际调用链；
没有修改 SDK 使用方式、Token 或公开入口。

- Prometheus：4 个 adxlet、4 个内嵌 Relay、1 个 Coordinator、1 个 Ingress，
  共 10 个最近 45 秒内有效的抓取目标。使用时间过滤，避免滚动前旧 Pod 样本重复计数。
- 数量和预留：创建期间 Running 增加 4、CPU 预留增加 2000m，删除后恢复基线。
- 指标：检查 Coordinator/adxlet 的原生进程指标、sandboxd/Collector 宿主进程指标，
  以及 Ingress 请求和 Relay 连接指标。
- Loki：查询 Coordinator、API Server、adxlet 和 runtime 四类日志；组件日志为 JSON。
- Tempo：按实际请求 Trace ID 查询，包含 API Server、Coordinator、adxlet、Execd。
- Grafana：验证四份看板的 UID、provisionedExternalId，以及文件 provider 和挂载文件。
  Helm 的块文本去掉末尾换行，其余文件字节与 ADX 原看板相同。

Grafana 本次 API 的 `meta.provisioned` 返回 false，但 `provisionedExternalId`
指向正确 JSON；同时文件 provider、生效文件和自动导入的 UID 均验证，未手工导入。

## 验收结果

最终镜像上的四节点 SDK 冒烟全部通过，10 个抓取目标正常，Running 从 0 增至 4
再回到 0，CPU 预留从 0 增至 2000m 再回到 0。Loki 四类日志通过，Tempo
同一 Trace 包含 57 个 Span 和上述四个服务，Grafana 四份看板检查通过。
部署 chart 测试 34/34 通过；最终配置使用 Collector 0.120.1 校验通过。
部署脚本语法检查（macOS 指定 `SHELL=/bin/bash`）、core/monitor Helm lint 和
`git diff --check` 均通过。Redis 控制目录的 Environment 字段为 0；删除去重
回执仍有正 TTL，属于短期幂等记录，不是长期保留的实例目录。

## 边界

本轮是可观测接入与四节点 SDK 冒烟，不覆盖压力、长期稳定性、故障注入、
Kata/Firecracker 全量验收。Ingress 当前没有独立进程资源指标；内嵌 Relay
的进程资源包含在 adxlet 中。实际用量面板依赖 ADX/sandboxd 的采样数据，
资源预留指标不代表实际消耗。本轮未验证 standalone 的采集完整性。

验收日志保存在执行工作区 `out/otel-adx-20261008/`，包含 SDK、后端查询、
Collector 配置校验、镜像 digest 校验及 Helm 发布记录；凭据不进入仓库。

## 看板整合验收

后续 monitor revision 22 将 Observability 内容整合到 Schedule，删除旧看板。
看板同步 ADX `1e5b10f`，执行二进制仍为上述 `4d6d3f1`，无需重建业务镜像。
Schedule 的 21 个面板逐一通过 Prometheus/Loki 查询验证；Grafana 旧 UID
返回 404，三份看板的导航已同步。四节点磁盘调度容量与可用合计
212635766784 bytes（约 198 GiB），预留及分配率为 0。CPU/内存/磁盘
预留直接查询 `reserved`，不将禁止调度造成的可用量为零误算为分配。

34 项 chart 测试、monitor Helm lint、ADX 文档检查及两仓库 diff 检查通过。
本轮只变更看板与文档，未重复创建 Sandbox；实例实际用量面板在无运行实例
时无数据属于正常情况。验收日志：`dashboard-merge-live.log`，结果文件：
`dashboard-merge-result.json`（均在同一执行工作区证据目录）。

## 旧调度看板移除

monitor revision 23 移除旧 AKernel Schedule 的文件 provisioning。
UID `yuanrong-complete-monitoring` 在 Grafana API 返回 404，搜索中已不再出现；
ADX Schedule、Data Plane、Process Resources 三张看板仍可正常访问。
本轮 Helm lint 和 diff 检查通过，未改动指标采集或业务部署，未重复 SDK 用例。
证据日志：`legacy-dashboard-retire-live.log`。

## Schedule 重复面板清理

monitor revision 24 同步 ADX `44e5d68`，移除 Schedule 的节点 CPU 实际使用率、
沙箱 CPU 实际使用量、沙箱内存实际使用量三个重复面板，保留磁盘调度账本。
Grafana 的 18 个面板与当前源码一致，所有剩余 Prometheus/Loki 查询通过，
磁盘指标非空。既有节点资源、沙箱详情看板的文件、查询和变量均保持原样。

本轮只调整 Schedule 与文档，未改动业务部署或执行 Sandbox 用例。
monitor Helm lint、ADX 文档检查和两仓库 diff 检查通过。证据：
`dashboard-boundaries-live.log`、`dashboard-boundaries-result.json`。

## 日志阅读格式与环境筛选

monitor revision 26 同步 ADX `abd9b6d` 的 Schedule 日志查询。JSON 组件日志
显示级别与消息；无消息字段或非 JSON 日志保留原文，Loki 存储内容不变。
环境 All 改为 `.+`，避免空匹配表达式导致 Loki 返回 400。

固定五分钟窗口验证 3016 条日志：7 条格式化、3009 条原文回退，记录未减少；
All 查询通过，Grafana 查询与变量与源码一致。四个其他看板的文件、查询和
变量未改变。monitor Helm lint、ADX 文档和两仓库 diff 检查通过，
未重启业务组件或创建 Sandbox。证据：`log-format-live.log`、
`log-format-result.json`。

共享 Loki 中的旧 `yuanrong-*` 日志来自仍运行的 `akernel-test` 部署，
环境为 `cn-north-4-benchmark`；当前 `akernel-cce` 日志查询未发现旧组件。
本轮未修改该测试部署或删除历史日志。

## 沙箱日志搜索

monitor revision 27 同步 ADX `6ff870e`，Schedule 的日志面板替换为沙箱日志
搜索，仅查询 `adx-runtime` 主进程 stdout/stderr，支持运行记录多选及原文
关键词匹配。控制面日志从该面板移除，采集链路、其他看板和调度查询未改变。

固定六小时时间窗口验证 8 条运行记录共 314 行日志，原文保持完整；
资源采样候选有 5 条。选择一个有采样记录返回 59 行、选择两个返回 80 行，
均与原始日志的 ID 过滤结果一致。关键词匹配返回 24 行；空关键词、无匹配
关键词、引号与反斜杠及 All 环境查询均通过。缺少采样的运行记录不出现在
下拉候选中，可选择 All 查看已采集日志；SDK command 输出不是该归档的数据源。

Grafana panel/variables 与源码一致，四个其他看板的文件、查询和变量未改动。
Helm lint、ADX 文档与两仓库 diff 检查通过，本轮未创建 Sandbox。证据：
`sandbox-log-search-live.log`、`sandbox-log-search-result.json`。

## 日志正文与接口 Trace 验收

当前 `akernel-core` revision **95**、`akernel-monitor` revision **29**，均 deployed。
Coordinator、Ingress 与四个 Node 主容器 Ready，Redis/监控服务 Ready。
业务镜像为：

`swr.cn-north-4.myhuaweicloud.com/openyuanrong/cluster-all-in-one:pr78-otel-readable-musl-20261008@sha256:4ca4630a97db2e3ba87a27a0065a4404142bdd14d5087fe55cb6431133e56e64`

这是本次源码验收镜像：在前述 `4d6d3f1` 镜像上替换 API Server、Ingress、Execd
及 AKernel runtime rootfs，HTTP 改动基于 ADX `6ff870e45738d0f4d825144fd4f401fb3d4e18c8`
工作树；记录源码归档摘要和三个二进制摘要的 `observability-patch.json` 随镜像保存。
不是已发布的新版 OBS 正式包。sandboxd、Coordinator/adxlet 二进制和 Collector
0.120.1 沿用基础镜像，Collector 配置采用本轮修改。
最初 GNU 静态链接验收镜像的 API Server 启动失败，Helm atomic 已回滚；
当前三个 HTTP 二进制采用 musl 构建并通过启动及集群验收。

- 部署内真实 Collector 的隔离用例：节点/控制各六条，共 **12/12** 通过。
  验证 message/event 正文、元数据、原始时间、级别，以及普通文本、损坏 JSON、
  无消息字段和空消息的处理。普通 Explore 查询已验证 Coordinator、adxlet、
  API Server 的新日志正文为消息，target 等字段仍可检索。
- 真实 AKernel SDK 完成一个 runsc Sandbox 的创建、命令、文件写入/读取、删除。
  删除后 Running 回到原基线 0。将标识写到沙箱主进程 stdout/stderr，Loki
  返回三行，Schedule 的实际查询和 Grafana 数据帧均找到两种输出。
  主进程无近期输出仍会显示无数据；这不等同于 SDK command 返回流的归档。
- 六条 SDK 请求 Trace 在 Tempo 可检索。HTTP Span 包含方法与接口模板，
  包括创建、删除、`/invoke`、`/upload`、`/download`；Execd 的 invoke 子 Span
  另有 `cmd_list`、`cmd_run` 的 `rpc.method`。接口名称不包含沙箱 ID 或查询参数。
- Schedule 的接口表使用匹配 Span，Grafana 数据帧确认有接口名、方法、路由、
  组件、响应状态、耗时和 Trace ID。测试只注入远端父上下文、未上传客户端父 Span，
  仍可识别接口；不依赖缺失的根 Span 名称。日志 `trace_id` 到 Tempo 的关联配置已核对。

本轮只调整 Schedule、新日志正文处理与 Loki 关联，四张既有其他看板保持原样。
历史 JSON 日志和旧 Span 不被重写；没有再次执行完整运行时/压力/长稳矩阵。
最终面板核对复用上述 SDK 数据，没有重复创建实例。源码未提交或推送。

执行工作区证据目录 `out/otel-adx-20261008/normalize/`：
`event-green.log`、`live.log`/`live-result.json`（SDK 与日志/Trace 结果；初版脚本的
根 Span 字段假设导致其最后展示检查中止）、`panels-live.log`/`panels-result.json`
（修正为 Span 展示后的实际 Grafana 查询通过）、`core-deploy-final.log`、
`monitor-spans-deploy.log`、`deployed-images-final.json`。
ADX HTTP 合约及 Clippy 日志位于 ADX 工作树 `out/otel-readability/`。

## Trace 表格实际页面修正

`akernel-monitor` revision **32** 已 deployed。仅更新 Schedule 的展示与查询，
业务镜像保持 revision 95 的前述验收镜像；后台心跳/调度轮次埋点裁剪仍未部署。

此前 API 数据帧验收不能证明前端表格可读。真实浏览器页面重现了默认根 Trace
Service/Name 列的缺失父 Span 提示、重复的接口属性列，以及前端字段名与 API
数据帧不同导致的排序/标题问题。Schedule 现在按匹配 Span 展示六列：开始时间、
接口、实际 Span 组件、状态、Span 耗时、Trace ID；去掉根 Trace 元数据与
内部 nested 字段，过滤未识别接口探测记录。每行仍代表一个 Span，同一请求
经过多阶段可有多个相同 Trace ID 的行。

截图中的六条 SDK Trace 来自前述验收，仅注入远端父上下文、没有采集客户端父
Span；因此 Explore 的原始 Traces 表缺少根 Service/Name。该原始表与看板配置
独立，仍适用于原始诊断；Schedule 使用实际匹配 Span 的信息。

单个 HTTP Span 时长不能当作流式创建的总耗时：本轮创建 Trace 的 HTTP Span
约 1–2 ms，而完整调用链为约 181 ms，包括后续创建任务、adxlet 启动和就绪。
列名明确为 Span 耗时；点击 Trace ID 打开的完整链路能查看实际阶段分解。

真实无头 Chrome 页面验证通过：六列表头和创建、删除、invoke、upload、download
接口行可读，没有根 Span 占位提示或 nested JSON 列；Trace ID 点击后打开含
API Server、Coordinator、adxlet 的完整创建链路。测试复用既有 Trace，没有创建
新实例。镜像/运行时及其他看板未修改。Helm lint、两仓库 diff、ADX 文档检查通过。

证据仍位于 `out/otel-adx-20261008/normalize/`：`trace-red.png`（页面问题）、
`browser-readability-final.log`、`trace-green.png`（六列）、`trace-detail-green.png`
（完整链路）、`trace-readability-deploy.log`。原 `panels-live.log` 仅为数据帧验收，
不能替代本次页面验收。源码修改尚未提交或推送。

## Explore 接口查询验收

monitor revision **34** 已部署；core 保持 revision **95**，本轮未替换业务镜像。
ADX Schedule 新增 **Explore 接口查询**，进入原生 Tempo Explore 并带入环境、
最近 6 小时、Spans 表和接口字段。ADX 与 AKernel 的 Schedule JSON 一致。

实际 Chrome 页面验收通过：环境变量正确展开，Spans 表显示实际接口名、组件、
HTTP 状态和 `cmd_run` / `cmd_list`，不出现 nested JSON；滚动虚拟表后点击创建
Span ID，能够打开完整的创建链路。真实 20 次只读资源查询未注入 traceparent，
采样收到 1 条根 Trace，根服务为 adx-apiserver，根名称为
GET /global-scheduler/resources。未创建新 Sandbox 或重复 SDK 生命周期测试。

证据：执行工作区 `out/otel-adx-20261008/normalize/` 中
`explore-browser-final.log`、`explore-green.png`、`explore-detail-green.png`、
`explore-roots-green.log`、`explore-roots.json` 和 `explore-basepath-deploy.log`。
首次入口检查确认缺少入口；子路径跳转已修正。中间脚本的 pane 固定名称假设和
虚拟表不可见行断言已修正，不能将这些脚本失败解释为接口/数据丢失。

原生 Explore 仍保留根 Trace 列，没有看板列隐藏配置；已有测试中父 Span 未上报的
提示保留。预设是 Explore 查询入口，不改变全局默认表格式。手工查询方法记录在
`deploy/observability.md`。后台心跳/调度轮次埋点裁剪仍属于未部署的源码变更。
