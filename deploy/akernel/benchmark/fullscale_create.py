#!/usr/bin/env python3
"""Create node-pinned 1-core sandboxes for a full-cluster regression."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import yr_sandbox
import yr_sandbox.sandbox_api as sandbox_api


sandbox_api._NODE_ID_LABEL = "HOST_IP"


def targets() -> list[tuple[str, str, int]]:
    result = []
    for raw in os.environ["AKERNEL_BENCH_TARGETS"].split(","):
        group, host_ip, count = raw.split(":", 2)
        result.append((group, host_ip, int(count)))
    return result


def create_one(group: str, host_ip: str, index: int) -> tuple[str, str]:
    sandbox = yr_sandbox.Sandbox(
        runtime="runsc",
        cpu=1000,
        memory=96,
        cpu_limit=1000,
        mem_limit=512,
        idle_timeout=3600,
        schedule_timeout=60,
        create_timeout=180,
        name=f"regression-{group}-{index:02d}",
        node_id=host_ip,
        detached=True,
    )
    return group, sandbox.id


def main() -> None:
    jobs = []
    created: list[tuple[str, str]] = []
    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            for group, host_ip, count in targets():
                for index in range(count):
                    jobs.append(executor.submit(create_one, group, host_ip, index))
            for future in as_completed(jobs):
                item = future.result()
                created.append(item)
                print(f"created group={item[0]} id={item[1]}", flush=True)
    except BaseException:
        for future in jobs:
            future.cancel()
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda item: yr_sandbox.Sandbox.delete(item[1]), created))
        raise

    created.sort()
    with open("/state/placements", "w", encoding="utf-8") as output:
        for group, sandbox_id in created:
            output.write(f"{group} {sandbox_id}\n")
    print(f"created_total={len(created)}", flush=True)


if __name__ == "__main__":
    main()
