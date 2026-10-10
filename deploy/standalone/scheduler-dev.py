#!/usr/bin/env python3
"""为已构建镜像生成并执行隔离开发容器的启动计划。"""

import argparse
import json
from pathlib import Path
import socket
import subprocess

NAME = "akernel-agent-dx-dev-node"
NETWORK = "akernel-agent-dx-dev"
OWNER = "akernel.scheduler.environment=agent-dx-dev-20261010"
STATE = Path("/data/akernel-agent-dx-dev/state")


def commands(image):
    return [
        ["docker", "network", "create", "--label", OWNER, NETWORK],
        ["docker", "run", "--detach", "--name", NAME, "--label", OWNER,
         "--network", NETWORK, "--privileged", "--cgroupns", "private",
         "--cpus", "4", "--cpuset-cpus", "0-7", "--memory", "16g",
         "--memory-swap", "16g", "--pids-limit", "8192",
         "--publish", "127.0.0.1:19443:8443", "--publish", "127.0.0.1:19080:8080",
         "--mount", f"type=bind,source={STATE},target=/home/akernel",
         "--log-opt", "max-size=16m", "--log-opt", "max-file=3", image],
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    # 镜像必须使用完整本地内容身份，不通过可变标签启动。
    if not args.image.startswith("sha256:") or len(args.image) != 71:
        parser.error("--image 必须是完整 sha256 镜像身份")
    try:
        int(args.image[7:], 16)
    except ValueError:
        parser.error("镜像 SHA256 非法")
    plan = commands(args.image)
    print(json.dumps({"state": str(STATE), "commands": plan}, indent=2), flush=True)
    if not args.apply:
        return
    for kind, name in (("container", NAME), ("network", NETWORK)):
        result = subprocess.run(["docker", kind, "inspect", name], capture_output=True)
        if result.returncode == 0:
            raise RuntimeError(f"{kind} {name} 已存在，请先核对其物理归属")
    subprocess.run(["docker", "image", "inspect", args.image], check=True,
                   stdout=subprocess.DEVNULL)
    for port in (19443, 19080):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    STATE.mkdir(parents=True, exist_ok=True)
    for command in plan:
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
