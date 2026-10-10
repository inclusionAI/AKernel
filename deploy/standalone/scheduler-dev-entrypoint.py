#!/usr/bin/env python3
"""启动有独立持久目录和固定容量的单节点开发环境。"""

import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import time

import yaml

STATE = Path("/home/akernel")
CGROUP = Path("/sys/fs/cgroup")


def prepare_cgroup():
    # 此文件系统必须来自启动器声明的私有 cgroup namespace。
    if Path("/proc/self/ns/cgroup").readlink() != Path("/proc/1/ns/cgroup").readlink():
        raise RuntimeError("开发服务必须与容器 PID 1 共用私有 cgroup namespace")
    available = set((CGROUP / "cgroup.controllers").read_text().split())
    if not {"cpu", "memory", "pids"}.issubset(available):
        raise RuntimeError("开发容器缺少 cpu、memory 或 pids 控制器")
    services = CGROUP / "services"
    services.mkdir(exist_ok=True)
    for pid in (CGROUP / "cgroup.procs").read_text().split():
        (services / "cgroup.procs").write_text(pid)
    (CGROUP / "cgroup.subtree_control").write_text("+cpu +memory +pids")
    (services / "memory.max").write_text(str(4 * 1024**3))


def prepare_config(templates=Path("/etc/akernel/dev-templates"),
                   output=Path("/etc/akernel/adx-standalone.yaml"),
                   adxctl="/opt/adx/current/bin/adxctl"):
    config = STATE / "sandboxd/config"
    config.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "oss.json", "registry.json", "oss_auths.json"):
        target = config / name
        if not target.exists():
            shutil.copyfile(templates / name, target)
    auths = config / "registry_auths.json"
    if not auths.exists():
        auths.write_text('{"auths": {}}\n')
    auths.chmod(0o600)
    text = (templates / "sandboxd_config.toml").read_text()
    for old, new in (
        ('10.88.0.1/16', '10.93.0.1/16'),
        ('cgroup_cache_size=800', 'cgroup_cache_size=8'),
        ('netns_cache_size=1000', 'netns_cache_size=8'),
        ('interface_cache_size=1000', 'interface_cache_size=8'),
        ('max_instance_num=1000', 'max_instance_num=16'),
        ('cgroup_root_name="/akernel"', 'cgroup_root_name="/akernel-agent-dx"'),
        ('filestore_dir_size="50G"', 'filestore_dir_size="20G"'),
        ('overlay_tmpfs_size="10G"', 'overlay_tmpfs_size="1G"'),
        ('cgroup_memory_limit="64GiB"', 'cgroup_memory_limit="1GiB"'),
        ('# AKERNEL_CHUNK_DB_SIZE', 'chunk_db_size="4GiB"'),
        ('kata="/usr/local/bin/containerd-shim-kata-v2"', 'kata=""'),
        ('firecracker="/usr/local/bin/firecracker"', 'firecracker=""'),
    ):
        if old not in text:
            raise RuntimeError("开发模板与预期配置不一致")
        text = text.replace(old, new)
    (STATE / "sandboxd/config.toml").write_text(text)
    images = STATE / "images"
    images.mkdir(exist_ok=True)
    shutil.copyfile(config / "config.json", images / "config.json")
    adx = STATE / "adx"
    (adx / "secrets").mkdir(parents=True, exist_ok=True, mode=0o700)
    key = adx / "secrets/admin-key"
    if not key.exists():
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(48) + "\n")
    key.chmod(0o600)
    # 预留 4 GiB 给控制服务、运行时及缓存；容量只由这一节点通告一次。
    capacity = {
        "capacity": {"cpu_millis": 3000, "memory_bytes": 12 * 1024**3,
                     "disk_bytes": 16 * 1024**3},
        "devices": [], "valid_until_unix_seconds": int(time.time()) + 86400,
    }
    (adx / "capacity.json").write_text(json.dumps(capacity))
    deployment = {
        "schema_version": 1, "profile": "standalone", "internal_security": "network",
        "namespace": "akernel-agent-dx-dev",
        "logging": {"max_file_bytes": 16777216, "max_files": 3,
                    "max_total_bytes": 268435456},
        "service_overrides": {
            "adxlet": {
                "config": {
                    "execd_env": {"ADX_EXECD_CONTROL_SOCKET_PATH": "/run/akernel"},
                },
                "env": {"ADX_DATA_PLANE_ALLOWED_TARGET_CIDRS": "10.93.0.0/16"},
            },
            "ingress": {"env": {
                "ADX_DATA_PLANE_INGRESS_PLAIN_BIND": "0.0.0.0:8080",
                "ADX_DATA_PLANE_INGRESS_ALLOWED_CLIENT_CIDRS": "0.0.0.0/0",
                "ADX_DATA_PLANE_INGRESS_PORT_HOST_DOMAIN": "localhost",
            }},
        },
    }
    output.write_text(json.dumps(deployment))
    # 配置覆盖采用深合并。先展开，再整体替换资源来源，避免把 auto 的
    # disk_path、valid_for_seconds 留进拒绝额外字段的 file 配置。
    expanded = yaml.safe_load(subprocess.check_output(
        [adxctl, "--config", str(output), "config", "dump"], text=True))
    for service in expanded["services"]:
        if service["role"] == "adxlet":
            service["config"]["resource_source"] = {
                "kind": "file", "path": str(adx / "capacity.json"),
            }
            service["config"]["checkpoint_dir"] = str(
                STATE / "sandboxd/root/checkpoints/adx")
    output.write_text(json.dumps(expanded))
    subprocess.run([adxctl, "--config", str(output), "validate"], check=True)


def main():
    prepare_cgroup()
    prepare_config()
    subprocess.run(["/usr/local/bin/sandboxd-network-prepare"], check=True)
    (STATE / "logs/sandboxd").mkdir(parents=True, exist_ok=True)
    daemon = subprocess.Popen([
        "/usr/local/bin/sandboxd", "-config", str(STATE / "sandboxd/config.toml"),
        "-socket", "/run/sandboxd/sandboxd.sock", "-log-level", "info",
        "-log-file", str(STATE / "logs/sandboxd/sandboxd.log"),
    ], env=dict(os.environ, GOMEMLIMIT="1GiB", GOGC="100"))
    children = [daemon]
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        deadline = time.monotonic() + 90
        while not Path("/run/sandboxd/sandboxd.sock").exists():
            if stopped or daemon.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("sandboxd 未在期限内提供服务")
            time.sleep(0.25)
        children.append(subprocess.Popen(["/usr/local/bin/adx-service", "run"]))
        while not stopped:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("开发环境服务意外退出")
            time.sleep(0.5)
    finally:
        # 先让调度服务结束，再终止 daemon；不删除物理状态或清空账本。
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=100)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("服务未正常结束，保留环境用于检查")


if __name__ == "__main__":
    main()
