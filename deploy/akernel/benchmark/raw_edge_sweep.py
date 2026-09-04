#!/usr/bin/env python3
"""Run multiple raw Edge benchmark processes over selected sandbox groups."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time


def parse_limits(value: str) -> dict[str, int]:
    if not value:
        return {}
    result = {}
    for item in value.split(","):
        group, count = item.split("=", 1)
        result[group] = int(count)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", required=True)
    parser.add_argument("--group-limits", default="")
    parser.add_argument("--connections-per-sandbox", type=int, default=4)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--operations-per-connection", type=int, default=3000)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--action", default="ping")
    args = parser.parse_args()

    selected_groups = set(args.groups.split(","))
    limits = parse_limits(args.group_limits)
    by_group: dict[str, list[str]] = {}
    with open("/state/placements", encoding="utf-8") as source:
        for line in source:
            group, sandbox_id = line.split(maxsplit=1)
            if group in selected_groups:
                by_group.setdefault(group, []).append(sandbox_id.strip())

    sandboxes = []
    for group in sorted(selected_groups):
        values = sorted(by_group.get(group, []))
        sandboxes.extend(values[: limits.get(group, len(values))])
    if not sandboxes:
        raise SystemExit("no matching sandboxes")

    connections = [
        sandbox_id
        for sandbox_id in sandboxes
        for _ in range(args.connections_per_sandbox)
    ]
    shards = [[] for _ in range(min(args.shards, len(connections)))]
    for index, sandbox_id in enumerate(connections):
        shards[index % len(shards)].append(sandbox_id)

    processes = []
    started = time.perf_counter()
    for shard in shards:
        command = [
            sys.executable,
            "/bench/edge_raw_throughput.py",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--action",
            args.action,
            "--operations-per-sandbox",
            str(args.operations_per_connection),
            *shard,
        ]
        processes.append(
            subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
        )

    results = []
    for process in processes:
        stdout, _ = process.communicate()
        if process.returncode != 0:
            raise SystemExit(f"benchmark shard failed with {process.returncode}")
        results.append(json.loads(stdout))
    wall_seconds = time.perf_counter() - started

    operations = sum(item["operations"] for item in results)
    weighted_mean = sum(
        item["latency_ms"]["mean"] * item["operations"] for item in results
    ) / operations
    print(
        json.dumps(
            {
                "action": args.action,
                "groups": sorted(selected_groups),
                "sandboxes": len(sandboxes),
                "connections": len(connections),
                "connections_per_sandbox": args.connections_per_sandbox,
                "shards": len(shards),
                "operations": operations,
                "wall_seconds": wall_seconds,
                "throughput_ops_s": operations / wall_seconds,
                "shard_throughput_ops_s": [
                    item["throughput_ops_s"] for item in results
                ],
                "latency_ms": {
                    "weighted_mean": weighted_mean,
                    "max_shard_p50": max(item["latency_ms"]["p50"] for item in results),
                    "max_shard_p95": max(item["latency_ms"]["p95"] for item in results),
                    "max_shard_p99": max(item["latency_ms"]["p99"] for item in results),
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
