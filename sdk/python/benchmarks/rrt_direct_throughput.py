#!/usr/bin/env python3

# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Benchmark local sandbox RRT endpoints without Edge or Node Proxy.

Run this inside an AKernel node container. Active sandbox bridge addresses are
discovered from sandboxd OCI metadata and each endpoint receives one persistent
HTTP/1.1 connection, matching the Edge backend pool's steady-state shape.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

_CONFIG_GLOB = "/home/akernel/sandboxd/root/containers/sbox-*/config.json"
_INTERFACE_ANNOTATION = "sandbox.akernel.dev/resource-interface"
_CGROUP_ANNOTATION = "sandbox.akernel.dev/resource-cgroup"
_CPU_CGROUP_ROOT = Path("/sys/fs/cgroup/cpu,cpuacct")
_BODY = json.dumps(
    {"action": "file.exists", "args": {"path": "/"}},
    separators=(",", ":"),
).encode()


def _percentile(values: list[float], percentile: float) -> float:
    index = max(0, math.ceil(percentile * len(values)) - 1)
    return sorted(values)[index]


def _read_exact(reader: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = reader.read(remaining)
        if not chunk:
            raise EOFError(f"response ended with {remaining} bytes remaining")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_response(reader: BinaryIO) -> bytes:
    status = reader.readline()
    if not status.startswith(b"HTTP/1.1 200 "):
        raise RuntimeError(f"unexpected response status: {status!r}")
    content_length: int | None = None
    while True:
        line = reader.readline()
        if line in (b"\r\n", b""):
            break
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            content_length = int(value.strip())
    if content_length is None:
        raise RuntimeError("response is missing Content-Length")
    return _read_exact(reader, content_length)


def discover_active_endpoints() -> list[str]:
    """Return bridge IPs for sandboxd containers with a populated CPU cgroup."""

    endpoints: list[str] = []
    for config_path in sorted(Path("/").glob(_CONFIG_GLOB.lstrip("/"))):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        annotations = config.get("annotations") or {}
        cgroup = str(annotations.get(_CGROUP_ANNOTATION) or "")
        cgroup_procs = _CPU_CGROUP_ROOT / cgroup.lstrip("/") / "cgroup.procs"
        try:
            active = bool(cgroup_procs.read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            continue
        if not active:
            continue
        interface = json.loads(annotations[_INTERFACE_ANNOTATION])
        endpoints.append(str(interface["ip"]))
    return endpoints


def benchmark_endpoint(ip: str, operations: int, port: int) -> list[float]:
    """Run sequential keep-alive requests against one RRT endpoint."""

    request = (
        b"POST /invoke HTTP/1.1\r\n"
        + f"Host: {ip}:{port}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(_BODY)}\r\n".encode()
        + b"Connection: keep-alive\r\n\r\n"
        + _BODY
    )
    latencies: list[float] = []
    with socket.create_connection((ip, port), timeout=10) as connection:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with connection.makefile("rb") as reader:
            for _ in range(operations):
                started = time.perf_counter()
                connection.sendall(request)
                response = json.loads(_read_response(reader))
                if response.get("exists") is not True:
                    raise RuntimeError(f"unexpected response from {ip}: {response!r}")
                latencies.append(time.perf_counter() - started)
    return latencies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations-per-sandbox", type=int, default=5_000)
    parser.add_argument("--port", type=int, default=50_090)
    args = parser.parse_args()
    if args.operations_per_sandbox <= 0:
        parser.error("--operations-per-sandbox must be positive")

    endpoints = discover_active_endpoints()
    if not endpoints:
        raise SystemExit("no active local sandbox endpoints found")

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
        samples = list(
            executor.map(
                lambda ip: benchmark_endpoint(
                    ip, args.operations_per_sandbox, args.port
                ),
                endpoints,
            )
        )
    wall_seconds = time.perf_counter() - started
    latencies_ms = [sample * 1000 for values in samples for sample in values]
    operations = len(latencies_ms)
    print(
        json.dumps(
            {
                "endpoints": len(endpoints),
                "operations": operations,
                "wall_seconds": wall_seconds,
                "throughput_ops_s": operations / wall_seconds,
                "latency_ms": {
                    "mean": sum(latencies_ms) / operations,
                    "p50": _percentile(latencies_ms, 0.50),
                    "p95": _percentile(latencies_ms, 0.95),
                    "p99": _percentile(latencies_ms, 0.99),
                    "max": max(latencies_ms),
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
