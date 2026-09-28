# 仅主控 function_master 更新镜像变体(master-m1)

目的:落地"具名可复用快照不随源实例退出而删除"修复
(fs 仓 19bedc9c;store 单测 36/36 过;function_master Release
产物 SHA `b82986cfaf1f14cea7ddfbf45cd9a785d23c0cd464353a8c7d3deea40e7f3c98`)。

## 关键事实(构建前已核)

- 实际执行的 master 二进制是
  `/opt/akernel-scheduler/function-master/function_master`
  (根 verdict `/proc/138/exe`;基座该文件为普通文件非链接,0755,
  36812152B,SHA `a0fd58b3d9d38c5583a23910ef4d3837307ab61f36fb3a5aee8cbf75667277a4`
  =当前运行进程)。
- `/home/yuanrong/functionsystem/bin/function_master`(178B)是包装器:
  `export LD_LIBRARY_PATH=/opt/akernel-scheduler/function-master/lib…;
  exec /opt/akernel-scheduler/function-master/function_master "$@"`
  ——隔离加载检查必须用该 lib 目录。
- 基座 = 当前 SIGTERM 角色镜像完整 digest
  `d307f76990fdff27770904b9aef6d2cdf8358dfd26828b2ebbb03aca321758b0`
  (75 层)。

## 配方

- `master-m1-image.Dockerfile`(机器生成,SHA `f16634ef…`):
  FROM 上述 digest + 单条 `COPY --chmod=755 function_master → /opt 实路径`
  ——恰好一个新文件层,不触碰 wrapper/lib/其他任何字节。
- 构建上下文只含 Dockerfile 与 Release 产物 `function_master`
  (来源 `/data/work/19bedc9c-release-…/build-release/bin/`,SHA 校验后进入上下文)。

## 验收(本目录构建脚本执行)

1. 新镜像层序 = 基座 75 层前缀 + 恰 1 新层,且该层只含上述一个路径;
2. runtime config 与基座**完全一致**(含 StopSignal=SIGTERM),
   差异仅允许 rootfs/history 等结构字段;
3. 镜像内该文件 SHA 必须 = `b82986cf…e7f3c98`;
4. 隔离加载:`--network none` 容器内以
   `LD_LIBRARY_PATH=/opt/akernel-scheduler/function-master/lib` 运行
   `function_master --help`(及实际动态加载),留真实 rc 与容器终态;
   该检查不代替部署后真实运行。

## 边界

- 本配方**只**构建与检查;kind 导入/helm 升级/滚动均未授权;
- 部署时 values 仅 `core.master.image.tag` 一行(frontend/nodes 原值不滚动)。
