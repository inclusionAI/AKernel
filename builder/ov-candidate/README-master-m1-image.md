# 仅主控 function_master 更新镜像变体(master-m1)

修复落地面:具名可复用快照不随源实例退出而删除
(源码仓 components/yuanrong-functionsystem,修复提交
ed989809…(根审修订 fcb24457…),独立测试目标提交
19bedc9c1841ebd6be4cadcba1e12d60f858ecc1,
tree e82378b2b998a342044cc0894058b0480308a22f;
store 单测 36/36 通过,二进制
function_master Release 产物 SHA
b82986cfaf1f14cea7ddfbf45cd9a785d23c0cd464353a8c7d3deea40e7f3c98,
构建目录 /data/work/19bedc9c-release-20260928T015453Z)。

## 关键事实(构建前已核)

- 实际执行的 master 二进制是
  /opt/akernel-scheduler/function-master/function_master
  (根 verdict role-term-running-process-libraries-independent.json
  /proc/138/exe;基座该路径为普通文件非符号链接,0755,36812152 字节,
  SHA a0fd58b3d9d38c5583a23910ef4d3837307ab61f36fb3a5aee8cbf75667277a4
  =当前运行中 master 进程)。
- /home/yuanrong/functionsystem/bin/function_master(178 字节)为包装器:
  设 LD_LIBRARY_PATH=/opt/akernel-scheduler/function-master/lib 后
  exec 上述实路径。
- 基座 = 当前 SIGTERM 角色镜像完整 digest
  d307f76990fdff27770904b9aef6d2cdf8358dfd26828b2ebbb03aca321758b0
  (75 层)。

## 配方(components/AKernel 提交)

- ad53f3d2d9811e9134333d680880897d8b9756db(本文件+Dockerfile;Dockerfile
  完整 SHA 4473109fd1f4723ecf52506bcd4f39e4a41f24308b62f73f7f5ecf77fa921c8f):
  FROM 上述 digest + 纯 COPY function_master 到实路径;
  0755 来自上下文文件(构建前 chmod+stat 核验;legacy 构建器无 --chmod)。
  首版 f16634ef…(COPY --chmod)因 legacy 构建器不支持而失败——该轮
  build.log 被同名重跑覆盖,原件缺失已登记,不补造。

## 已过验收(证据目录 /data/work/master-m1-build-b0791aa6-20260928T020845Z)

1. 层序 = 基座 75 层前缀 + 恰 1 新层;新层常规文件仅目标 binary 一个
   (父目录 tar 条目 opt/… 允许),mode 0755;
2. runtime config 子对象与基座**整体相等**(含 StopSignal=SIGTERM);
   顶层差异仅 created/history/rootfs 结构字段;
3. 镜像内该文件 SHA =
   b82986cfaf1f14cea7ddfbf45cd9a785d23c0cd464353a8c7d3deea40e7f3c98;
4. 隔离加载(--network none,LD_LIBRARY_PATH=实际部署 lib 目录):
   function_master --help 实际运行并输出完整 flag 帮助
   (gflags --help 惯例退出码 2,容器终态已记录);
   镜像环境 ldd rc=0、"not found" 0 处、依赖解析 54 项。
   上述不代替部署后真实运行验收。

## 边界

- 本配方只构建与检查;kind 导入/helm 升级/滚动均未授权;
- 部署时 values 仅 core.master.image.tag 一行(frontend/nodes 原值不滚动)。
