#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Measure Sandbox Create completion throughput and validate post-create routes."""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
import urllib.parse
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

try:
    from benchmarks.direct_command.raw_direct import (
        _connection,
        _headers,
        _latency_summary,
        _parsed_origin,
        build_create_body,
        delete_sandbox,
        list_instances,
        parse_final_event,
    )
except ModuleNotFoundError:  # ConfigMap runner mounts both files in /bench.
    from raw_direct import (  # type: ignore[no-redef]
        _connection,
        _headers,
        _latency_summary,
        _parsed_origin,
        build_create_body,
        delete_sandbox,
        list_instances,
        parse_final_event,
    )


def build_body(
    *,
    name: str,
    target_node: str,
    runtime: str,
    cpu: int,
    memory: int,
    cpu_limit: int,
    memory_limit: int,
    idle_timeout: int,
) -> dict[str, Any]:
    """Build a public create request with optional hard node affinity."""

    body = build_create_body(
        name=name,
        node_id=target_node or "automatic",
        runtime=runtime,
        cpu=cpu,
        memory=memory,
        idle_timeout=idle_timeout,
    )
    body["cpu_limit"] = cpu_limit
    body["mem_limit"] = memory_limit
    if not target_node:
        body.pop("scheduleAffinities", None)
    return body


class _CreateClient:
    """Keep one HTTP connection per load worker, matching SDK client reuse."""

    def __init__(self, origin: str, token: str, timeout_seconds: float) -> None:
        self.token = token
        self.connection = _connection(_parsed_origin(origin), timeout_seconds)
        self.connections_opened = 0
        self.server_closed_responses = 0
        self._connect()

    def _connect(self) -> None:
        self.connection.connect()
        self.connections_opened += 1
        if self.connection.sock is not None:
            self.connection.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def create(
        self,
        body: dict[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        if self.connection.sock is None:
            self._connect()
        payload = json.dumps(body, separators=(",", ":")).encode()
        headers = _headers(self.token, request_id=request_id, direct=False)
        headers["Accept"] = "text/event-stream"
        headers["Content-Length"] = str(len(payload))
        self.connection.request(
            "POST", "/api/sandbox/v1/sandboxes", body=payload, headers=headers
        )
        response = self.connection.getresponse()
        content = response.read()
        if response.will_close:
            self.server_closed_responses += 1
        if not 200 <= response.status < 300:
            raise RuntimeError(
                f"Sandbox create returned HTTP {response.status}: "
                f"{content[:512].decode('utf-8', errors='replace')}"
            )
        if "text/event-stream" in response.getheader("content-type", "").lower():
            result = parse_final_event(content)
        else:
            result = json.loads(content)
        if not isinstance(result, dict) or result.get("status") != "running":
            raise RuntimeError(f"Sandbox create did not reach running: {result!r}")
        if not (result.get("sandboxId") or result.get("instanceId")):
            raise RuntimeError("Sandbox create result has no Sandbox ID")
        return result

    def close(self) -> None:
        self.connection.close()


def _route_ready(
    origin: str,
    token: str,
    sandbox_id: str,
    *,
    timeout_seconds: float,
) -> None:
    parsed = _parsed_origin(origin)
    request_id = "ready-" + uuid.uuid4().hex
    connection = _connection(parsed, timeout_seconds)
    try:
        path = "/direct/" + urllib.parse.quote(sandbox_id, safe="") + "/healthz"
        connection.request(
            "GET",
            path,
            headers=_headers(token, request_id=request_id, direct=True),
        )
        response = connection.getresponse()
        content = response.read()
        if response.status != 200:
            raise RuntimeError(
                f"route health returned HTTP {response.status}: "
                f"{content[:256].decode('utf-8', errors='replace')}"
            )
        value = json.loads(content)
        if not isinstance(value, dict) or value.get("status") != "ok":
            raise RuntimeError("route health response is not ok")
    finally:
        connection.close()


def _retry_transient(operation: Any, *, attempts: int = 5) -> Any:
    """Retry benchmark control operations outside the measured create interval."""

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except (ConnectionError, OSError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
    assert last_error is not None
    raise last_error


def run_create_phase(
    *,
    origin: str,
    token: str,
    run_id: str,
    count: int,
    concurrency: int,
    target_node: str,
    runtime: str,
    cpu: int,
    memory: int,
    cpu_limit: int,
    memory_limit: int,
    idle_timeout: int,
    timeout_seconds: float,
    cleanup_concurrency: int,
) -> dict[str, Any]:
    """Create one fixed batch, then verify routes and clean it outside timing."""

    if count < 1 or concurrency < 1 or cleanup_concurrency < 1:
        raise ValueError("count and concurrency must be positive")
    created: list[dict[str, Any]] = []
    create_latencies: list[float] = []
    create_errors: list[str] = []
    lock = threading.Lock()
    local = threading.local()
    clients: list[_CreateClient] = []

    def client() -> _CreateClient:
        value = getattr(local, "create_client", None)
        if value is None:
            value = _CreateClient(origin, token, timeout_seconds)
            local.create_client = value
            with lock:
                clients.append(value)
        return value

    def create_one(index: int) -> None:
        create_client = client()
        started = time.perf_counter()
        try:
            result = create_client.create(
                build_body(
                    name=f"create-perf-{run_id}-{index}",
                    target_node=target_node,
                    runtime=runtime,
                    cpu=cpu,
                    memory=memory,
                    cpu_limit=cpu_limit,
                    memory_limit=memory_limit,
                    idle_timeout=idle_timeout,
                ),
                request_id=f"create-{run_id}-{index}",
            )
            record = {
                "sandbox_id": str(result.get("sandboxId") or result.get("instanceId")),
                "node_id": str(result.get("nodeId") or result.get("node_id") or ""),
            }
            latency = time.perf_counter() - started
            with lock:
                created.append(record)
                create_latencies.append(latency)
        except Exception as error:
            with lock:
                create_errors.append(f"{type(error).__name__}: {error}"[:500])

    measured_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(count, concurrency)) as pool:
        futures = [pool.submit(create_one, index) for index in range(1, count + 1)]
        for future in as_completed(futures):
            future.result()
    for value in clients:
        value.close()
    measured_seconds = max(0.001, time.perf_counter() - measured_started)

    route_errors: list[str] = []

    def check_one(record: dict[str, Any]) -> None:
        try:
            _route_ready(
                origin,
                token,
                record["sandbox_id"],
                timeout_seconds=timeout_seconds,
            )
        except Exception as error:
            with lock:
                route_errors.append(
                    f"{record['sandbox_id']}: {type(error).__name__}: {error}"[:500]
                )

    with ThreadPoolExecutor(
        max_workers=min(len(created) or 1, cleanup_concurrency)
    ) as pool:
        list(pool.map(check_one, created))

    cleanup_errors: list[str] = []

    def cleanup_one(record: dict[str, Any]) -> None:
        try:
            _retry_transient(
                lambda: delete_sandbox(
                    origin,
                    token,
                    record["sandbox_id"],
                    timeout_seconds=max(60, timeout_seconds),
                )
            )
        except Exception as error:
            with lock:
                cleanup_errors.append(
                    f"{record['sandbox_id']}: {type(error).__name__}: {error}"[:500]
                )

    with ThreadPoolExecutor(
        max_workers=min(len(created) or 1, cleanup_concurrency)
    ) as pool:
        list(pool.map(cleanup_one, created))

    node_counts = Counter(record["node_id"] or "unreported" for record in created)
    succeeded = len(created)
    return {
        "status": (
            "passed"
            if succeeded == count
            and not create_errors
            and not route_errors
            and not cleanup_errors
            else "failed"
        ),
        "attempted": count,
        "succeeded": succeeded,
        "failed": len(create_errors),
        "concurrency": concurrency,
        "measured_seconds": round(measured_seconds, 3),
        "creates_per_second": round(succeeded / measured_seconds, 3),
        "create_latency_ms": _latency_summary(create_latencies),
        "route_ready": succeeded - len(route_errors),
        "route_errors": route_errors[:20],
        "cleanup_succeeded": succeeded - len(cleanup_errors),
        "cleanup_errors": cleanup_errors[:20],
        "create_errors": create_errors[:20],
        "http_workers": len(clients),
        "http_connections_opened": sum(value.connections_opened for value in clients),
        "http_server_closed_responses": sum(
            value.server_closed_responses for value in clients
        ),
        "node_counts": dict(sorted(node_counts.items())),
        "created": created,
    }


def _csv_ints(value: str) -> list[int]:
    result = sorted(set(int(item) for item in value.split(",") if item.strip()))
    if not result or result[0] < 1:
        raise argparse.ArgumentTypeError("expected positive comma-separated integers")
    return result


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--token-env", default="BENCH_TOKEN")
    parser.add_argument("--target-node", default="")
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument(
        "--concurrency", type=_csv_ints, default=_csv_ints("1,2,4,8,16")
    )
    parser.add_argument("--cpu", type=int, default=100)
    parser.add_argument("--memory", type=int, default=128)
    parser.add_argument("--cpu-limit", type=int, default=0)
    parser.add_argument("--memory-limit", type=int, default=0)
    parser.add_argument("--idle-timeout", type=int, default=900)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--cleanup-concurrency", type=int, default=16)
    parser.add_argument("--cooldown-seconds", type=float, default=5)
    parser.add_argument("--output", type=Path, default=Path("/results/summary.json"))
    args = parser.parse_args()

    if args.cpu < 1 or args.memory < 1:
        parser.error("--cpu and --memory must be positive")
    if args.cpu_limit < 0 or args.memory_limit < 0:
        parser.error("--cpu-limit and --memory-limit cannot be negative")
    if args.cpu_limit and args.cpu_limit < args.cpu:
        parser.error("--cpu-limit cannot be lower than --cpu")
    if args.memory_limit and args.memory_limit < args.memory:
        parser.error("--memory-limit cannot be lower than --memory")

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        raise SystemExit(f"{args.token_env} is not set")
    run_id = uuid.uuid4().hex[:12]
    before = _retry_transient(lambda: list_instances(args.origin, token))
    phases: list[dict[str, Any]] = []
    fatal_error = ""
    try:
        for concurrency in args.concurrency:
            phase = run_create_phase(
                origin=args.origin,
                token=token,
                run_id=f"{run_id}-c{concurrency}",
                count=args.batch_size,
                concurrency=concurrency,
                target_node=args.target_node,
                runtime=args.runtime,
                cpu=args.cpu,
                memory=args.memory,
                cpu_limit=args.cpu_limit,
                memory_limit=args.memory_limit,
                idle_timeout=args.idle_timeout,
                timeout_seconds=args.timeout_seconds,
                cleanup_concurrency=args.cleanup_concurrency,
            )
            phases.append(phase)
            print(
                "phase "
                f"concurrency={concurrency} qps={phase['creates_per_second']} "
                f"p99_ms={phase['create_latency_ms']['p99']} "
                f"failed={phase['failed']} route_errors={len(phase['route_errors'])} "
                f"cleanup_errors={len(phase['cleanup_errors'])}",
                flush=True,
            )
            if phase["status"] != "passed":
                break
            if args.cooldown_seconds > 0:
                time.sleep(args.cooldown_seconds)
    except Exception as error:
        fatal_error = f"{type(error).__name__}: {error}"

    after = _retry_transient(lambda: list_instances(args.origin, token))
    created_ids = {
        record["sandbox_id"] for phase in phases for record in phase.get("created", [])
    }
    after_ids = {
        str(item.get("id") or item.get("instance_id") or item.get("instanceId") or "")
        for item in after
    }
    residue = sorted(created_ids.intersection(after_ids))
    status = (
        "passed"
        if phases
        and not fatal_error
        and not residue
        and all(phase["status"] == "passed" for phase in phases)
        else "failed"
    )
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "status": status,
        "scope": (
            "Sandbox Create request through running; "
            "route check and delete excluded"
        ),
        "origin": args.origin,
        "target_node": args.target_node or None,
        "placement_mode": "pinned" if args.target_node else "automatic",
        "runtime": args.runtime,
        "batch_size": args.batch_size,
        "concurrency": args.concurrency,
        "sandbox_resource": {
            "cpu_request_millicores": args.cpu,
            "memory_request_mib": args.memory,
            "cpu_limit_millicores": args.cpu_limit,
            "memory_limit_mib": args.memory_limit,
        },
        "cluster_instances_before": len(before),
        "cluster_instances_after": len(after),
        "phases": phases,
        "fatal_error": fatal_error or None,
        "residue": residue,
    }
    _write_json(args.output, result)
    print(
        "ADX_CREATE_THROUGHPUT_RESULT=" + json.dumps(result, separators=(",", ":")),
        flush=True,
    )
    raise SystemExit(0 if status == "passed" else 1)


if __name__ == "__main__":
    main()
