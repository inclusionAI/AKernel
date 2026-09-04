#!/usr/bin/env python3

# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Benchmark Edge /direct using raw persistent TLS connections.

This diagnostic intentionally bypasses HTTPX and the high-level SDK while
retaining TLS, JWT authentication, Edge routing, Node Proxy, and RRT.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import BinaryIO

_ACTIONS = {
    "file.exists": (
        {"action": "file.exists", "args": {"path": "/"}},
        ("exists", True),
    ),
    "ping": ({"action": "ping", "args": {}}, ("status", "ok")),
}


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


def _read_response(reader: BinaryIO, expected_status: int) -> bytes:
    status = reader.readline()
    expected_prefix = f"HTTP/1.1 {expected_status} ".encode()
    if not status.startswith(expected_prefix):
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


def benchmark_instance(
    instance_id: str,
    connection_index: int,
    operations: int,
    host: str,
    port: int,
    token: str,
    context: ssl.SSLContext,
    barrier: threading.Barrier,
    missing_route: bool,
    edge_only: bool,
    body: bytes,
    expected_result: tuple[str, object],
    edge_generates_request_id: bool,
) -> list[float]:
    """Run one keep-alive TLS connection for a sandbox instance."""

    target_id = f"missing-{instance_id}" if missing_route else instance_id
    path = (
        "/__akernel_benchmark_not_found"
        if edge_only
        else f"/direct/{target_id}/invoke"
    )
    expected_status = 404 if edge_only else (503 if missing_route else 200)
    request_id_header = (
        b""
        if edge_generates_request_id
        else f"X-Request-ID: raw-{os.getpid()}-{connection_index}\r\n".encode()
    )
    request = (
        f"POST {path} HTTP/1.1\r\n".encode()
        + f"Host: {host}:{port}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"X-Auth: {token}\r\n".encode()
        + request_id_header
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: keep-alive\r\n\r\n"
        + body
    )
    latencies: list[float] = []
    with socket.create_connection((host, port), timeout=10) as tcp:
        tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with context.wrap_socket(tcp, server_hostname=host) as connection:
            with connection.makefile("rb") as reader:
                connection.sendall(request)
                warmup_body = _read_response(reader, expected_status)
                if not missing_route and not edge_only and json.loads(
                    warmup_body
                ).get(expected_result[0]) != expected_result[1]:
                    raise RuntimeError(
                        f"unexpected warm-up response: {warmup_body!r}"
                    )
                barrier.wait()
                for _ in range(operations):
                    started = time.perf_counter()
                    connection.sendall(request)
                    response_body = _read_response(reader, expected_status)
                    if not missing_route and not edge_only and json.loads(
                        response_body
                    ).get(expected_result[0]) != expected_result[1]:
                        raise RuntimeError(
                            f"unexpected response for {instance_id}: {response_body!r}"
                        )
                    latencies.append(time.perf_counter() - started)
    return latencies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--operations-per-sandbox", type=int, default=5_000)
    parser.add_argument("--missing-route", action="store_true")
    parser.add_argument("--edge-only", action="store_true")
    parser.add_argument("--action", choices=sorted(_ACTIONS), default="file.exists")
    parser.add_argument(
        "--edge-generates-request-id",
        action="store_true",
        help="Omit X-Request-ID to measure Edge-side UUID generation.",
    )
    parser.add_argument("instances", nargs="+")
    args = parser.parse_args()
    if args.operations_per_sandbox <= 0:
        parser.error("--operations-per-sandbox must be positive")
    token = os.environ.get("AKERNEL_TOKEN", "").strip()
    if not token:
        raise SystemExit("AKERNEL_TOKEN is required")
    action, expected_result = _ACTIONS[args.action]
    body = json.dumps(action, separators=(",", ":")).encode()

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    barrier = threading.Barrier(len(args.instances) + 1)
    with ThreadPoolExecutor(max_workers=len(args.instances)) as executor:
        futures = [
            executor.submit(
                benchmark_instance,
                instance_id,
                connection_index,
                args.operations_per_sandbox,
                args.host,
                args.port,
                token,
                context,
                barrier,
                args.missing_route,
                args.edge_only,
                body,
                expected_result,
                args.edge_generates_request_id,
            )
            for connection_index, instance_id in enumerate(args.instances)
        ]
        barrier.wait()
        started = time.perf_counter()
        samples = [future.result() for future in futures]
    wall_seconds = time.perf_counter() - started
    latencies_ms = [sample * 1000 for values in samples for sample in values]
    operations = len(latencies_ms)
    print(
        json.dumps(
            {
                "instances": len(args.instances),
                "action": args.action,
                "request_id_source": (
                    "edge" if args.edge_generates_request_id else "raw-client"
                ),
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
