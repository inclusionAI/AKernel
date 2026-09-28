# 恢复ceiling准入候选角色镜像(72da2ffd 四程序)

修复落地面: 具名恢复 ceiling 级保守准入(functionsystem 嵌套仓
提交链 …→72da2ffd; 组件测试 34/34 独立通过; 正式Release 四程序
独立通过, 证据 72da2ffd-four-program-release-independent-verdict.json,
构建目录 /data/work/72da2ffd-release-20260928T072812Z)。

## 关键事实(构建前已核)

- **运行路径**(复用既有基线 node-combined-checkpoint-running-
  processes-independent.json / master-m1-running-process-libraries-
  independent.json / role-term-exit-replacement-process-independent.json,
  本轮仅补 PID/startticks/exe/SHA 与库差异):
  - master 角色: `/opt/akernel-scheduler/function-master/function_master`
    与 `/opt/akernel-scheduler/function-proxy/function_proxy`(合并进程);
  - node/frontend 角色: `/opt/akernel-scheduler/function-proxy/function_proxy`;
  - 四程序在两镜像均有 `/opt/akernel-scheduler/<role>/` 实路径、
    per-role `lib/` 目录与 `/home/yuanrong/functionsystem/bin/` 下
    sh wrapper + `.wheel` ELF 副本(faasfrontend 侧, 现为剥离变体)。
    wrapper 只 export LD_LIBRARY_PATH 后 exec /opt 实路径——语义不变。
- **动态库差异结论**: 两基座现有 lib 目录即可完整解析四个新
  程序(node v2 与 term-m1 镜像环境 ldd rc=0、not-found=0;
  逐依赖 SHA 与 lib 目录清单存 Release 目录 libdiff-audit/)——
  **无需替换任何库**, 仅替换四个程序本体。
- **基座 digest**:
  - node v3 ← canon-df2e-yr-signalfix-node-splitfsr-v2
    (sha256:1bb51e0e…, SIGRTMIN+3 保持);
  - term m2 ← canon-df2e-yr-signalfix-term-m1
    (sha256:28e6e5fb…, SIGTERM 保持, master/frontend)。
- sandboxd/runsc/RRT/yr/插件/有效配置逐字节沿用; runtime config
  须与基座整体相等(规范 SHA 核对)。

## 替换内容(每镜像恰 8 个新常规文件, 全 0755)

| 程序 | /opt 实路径 | wheel 副本 |
|---|---|---|
| function_proxy | …/function-proxy/function_proxy | bin/function_proxy.wheel |
| function_master | …/function-master/function_master | bin/function_master.wheel |
| function_agent | …/function-agent/function_agent | bin/function_agent.wheel |
| runtime_manager | …/runtime-manager/runtime_manager | bin/runtime_manager.wheel |

产物 SHA 以 Release attempt 的 artifacts.json 为唯一权威。

## 验收(构建脚本内建)

1. 层序 = 基座层前缀 + 恰 1 新层(rootfs 单 COPY);新层恰 8 个常规
   文件, 0755, SHA 与 Release 一致;无非文件条目;
2. runtime config 与基座整体相等(含 STOPSIGNAL);
3. manifest digest 与 inspect Id 交叉核对;镜像 tar 保存+SHA;
4. 有限加载冒烟(每程序 --help, --network none --read-only, 容器
   保留): 仅证 ELF+动态库加载到达 main 参数解析, 不称启动成功。

## 边界

- 本配方只构建与检查;kind 导入/helm 升级/滚动/部署均未授权;
- 平台仓原 gitlink WIP 不动。
