# 按角色 STOPSIGNAL(SIGTERM) 镜像变体配方

背景:003248Z 一次性退出取证证明宽限期内无主动撤回;根因定位为
镜像基础层 `STOPSIGNAL SIGRTMIN+3`(f34b68d2,全部 canon 镜像继承)。
frontend/master 角色 PID1=yr(Go,仅 Notify SIGINT/SIGTERM)收不到有效
停止信号;node 角色 PID1=systemd 正常处理 RTMIN+3。根授权最小修复:
**按角色镜像变体**,不改共享镜像、不改 CLI、不用 preStop/K8s 新特性。

## 文件

- `stopsignal-term.Dockerfile`:由 BM1 上 `docker image inspect` 解析的
  **完整 digest 动态生成**(570dcf05…),非手写;
  `FROM akernel-bm1/all-in-one@sha256:6c8054ed…` + `STOPSIGNAL SIGTERM`,
  仅产生一个 0B 元数据层,不替换任何文件/库。
- `build-stopsignal-term-image.sh`:构建+自验证脚本(chart-of-record
  digest 交叉核对、目标不存在前置、RootFS.diff_ids 全等、history 恰
  增一个空层、config 键差集白名单、tar SHA、串行 kind load、全程
  完整日志与真实 rc)。

## 部署变更(仅 values 两行,待根审后执行)

`charts/core/values.yaml` 经 overlay:
`frontend.image.tag: canon-df2e-yr-signalfix-term`、
`master.image.tag: canon-df2e-yr-signalfix-term`;
node 继续全局 `core.image.tag: canon-df2e-yr-signalfix`(SIGRTMIN+3)。

## 验收范围

1. 构建:VERIFY_OK(仅 StopSignal 差异;文件系统不变);
2. 渲染:frontend Deployment / master StatefulSet image 指向新 tag,
   node DaemonSet image 不变;
3. 部署后:三 node Pod 未滚动、各代理 PID/starttick 不变;frontend/
   master 新 Pod PID1 完整 SHA 仍为 ecff370c…(变体未动二进制);
4. 物理退出复验(复用 003248 分步规程,采集缺陷修复:logs 去
   --timeout 改外部 subprocess 超时、SECRET_RE 更名 bug、stderr 保留、
   动态名称、expected_identity 按新根审计文件生成):宽限期内出现
   撤回写入(DELETE 后空能力 PUT 合法),成功判据仍由根读原件。
