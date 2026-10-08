#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Create node-pinned sandboxes and measure raw Direct Command throughput."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import resource
import ssl
import statistics
import threading
import time
import urllib.parse
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any


def build_create_body(
    *,
    name: str,
    node_id: str,
    runtime: str,
    cpu: int,
    memory: int,
    idle_timeout: int,
) -> dict[str, Any]:
    """Return the public Sandbox v1 create contract used by this benchmark."""

    return {
        "namespace": "default",
        "name": name,
        "failover": False,
        "idleTimeoutSeconds": idle_timeout,
        "createTimeoutSeconds": 90,
        "scheduleTimeoutSeconds": 30,
        "initCallTimeoutSeconds": 30,
        "rootfs": {"runtime": runtime},
        "cpu": cpu,
        "memory": memory,
        "cpu_limit": 0,
        "mem_limit": 0,
        "storage_limit_mb": 0,
        "scheduleAffinities": [
            {
                "kind": 0,
                "affinity": 2,
                "labelOps": [
                    {
                        "type": 0,
                        "labelKey": "NODE_ID",
                        "labelValues": [node_id],
                    }
                ],
            }
        ],
        "dataPlane": {
            "tunnelSecurityMode": "tls",
            "portForwardSecurityMode": "tls",
        },
        "lifecycle": "detached",
    }


def parse_final_event(payload: bytes) -> dict[str, Any]:
    """Extract the authoritative final event from a Sandbox create stream."""

    event = ""
    data_lines: list[str] = []
    for raw_line in payload.decode("utf-8").splitlines() + [""]:
        line = raw_line.rstrip("\r")
        if not line:
            if event == "final" and data_lines:
                value = json.loads("\n".join(data_lines))
                if not isinstance(value, dict):
                    raise ValueError("Sandbox create final event is not an object")
                return value
            event = ""
            data_lines = []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    raise ValueError("Sandbox create stream ended without a final event")


def _parsed_origin(origin: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("origin must be an http(s) origin")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("origin must not include a path, query, or fragment")
    return parsed


def _connection(
    parsed: urllib.parse.SplitResult, timeout: float
) -> http.client.HTTPConnection:
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=timeout,
            context=ssl._create_unverified_context(),  # noqa: SLF001
        )
    return http.client.HTTPConnection(
        parsed.hostname, parsed.port or 80, timeout=timeout
    )


def _headers(token: str, *, request_id: str, direct: bool) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Auth": token.strip(),
        "X-Request-Id": request_id,
    }
    if direct:
        headers["Authorization"] = "Bearer " + token.strip()
        headers["X-ADX-Request-ID"] = request_id
    return headers


def create_sandbox(
    origin: str,
    token: str,
    body: dict[str, Any],
    *,
    request_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    parsed = _parsed_origin(origin)
    payload = json.dumps(body, separators=(",", ":")).encode()
    connection = _connection(parsed, timeout_seconds)
    try:
        headers = _headers(token, request_id=request_id, direct=False)
        headers["Accept"] = "text/event-stream"
        headers["Content-Length"] = str(len(payload))
        connection.request(
            "POST", "/api/sandbox/v1/sandboxes", body=payload, headers=headers
        )
        response = connection.getresponse()
        content = response.read()
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
    finally:
        connection.close()


def delete_sandbox(
    origin: str, token: str, sandbox_id: str, *, timeout_seconds: float = 60
) -> None:
    parsed = _parsed_origin(origin)
    request_id = "delete-" + uuid.uuid4().hex
    connection = _connection(parsed, timeout_seconds)
    try:
        path = "/api/sandbox/v1/sandboxes/" + urllib.parse.quote(sandbox_id, safe="")
        connection.request(
            "DELETE",
            path,
            headers=_headers(token, request_id=request_id, direct=False),
        )
        response = connection.getresponse()
        content = response.read()
        if response.status not in (200, 202, 204, 404):
            raise RuntimeError(
                f"delete {sandbox_id} returned HTTP {response.status}: "
                f"{content[:512].decode('utf-8', errors='replace')}"
            )
    finally:
        connection.close()


def list_instances(origin: str, token: str) -> list[dict[str, Any]]:
    parsed = _parsed_origin(origin)
    connection = _connection(parsed, 30)
    try:
        request_id = "list-" + uuid.uuid4().hex
        headers = _headers(token, request_id=request_id, direct=True)
        connection.request("GET", "/api/instances", headers=headers)
        response = connection.getresponse()
        content = response.read()
        if response.status != 200:
            raise RuntimeError(f"instance list returned HTTP {response.status}")
        value = json.loads(content)
        if not isinstance(value, list):
            raise RuntimeError("instance list is not an array")
        return [item for item in value if isinstance(item, dict)]
    finally:
        connection.close()


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime + child.ru_utime + child.ru_stime


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile) - 1))
    return ordered[index] * 1000


def _latency_summary(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values, default=0.0) * 1000, 3),
        "mean": round(statistics.fmean(values) * 1000, 3) if values else 0.0,
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "p99": round(_percentile(values, 0.99), 3),
        "max": round(max(values, default=0.0) * 1000, 3),
    }


def _validate_command_response(status: int, content: bytes) -> None:
    if status != 200:
        raise ValueError(f"HTTP {status}")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("command response is not an object")
    if value.get("error"):
        raise ValueError("command returned an operation error")
    if int(value.get("exit_code", -1)) != 0:
        raise ValueError("command returned a nonzero exit code")
    if value.get("stdout", "") != "" or value.get("stderr", "") != "":
        raise ValueError("no-op command produced output")


def _validate_health_response(status: int, content: bytes) -> None:
    if status != 200:
        raise ValueError(f"HTTP {status}")
    value = json.loads(content)
    if not isinstance(value, dict) or value.get("status") != "ok":
        raise ValueError("RRT health response is not ok")


def run_phase(
    *,
    origin: str,
    token: str,
    sandbox_ids: list[str],
    concurrency: int,
    warmup_seconds: float,
    duration_seconds: float,
    timeout_seconds: float,
    run_id: str,
    operation: str = "command",
) -> dict[str, Any]:
    """Run one closed-loop Direct data-plane phase over persistent HTTP/1.1."""

    if not sandbox_ids:
        raise ValueError("at least one Sandbox is required")
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    if operation not in ("command", "health"):
        raise ValueError("operation must be command or health")
    parsed = _parsed_origin(origin)
    barrier = threading.Barrier(concurrency + 1)
    lock = threading.Lock()
    latencies: list[float] = []
    statuses: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    per_sandbox: Counter[str] = Counter()

    def worker(worker_index: int) -> None:
        connection: http.client.HTTPConnection | None = None
        local_latencies: list[float] = []
        local_statuses: Counter[str] = Counter()
        local_errors: Counter[str] = Counter()
        local_per_sandbox: Counter[str] = Counter()
        counter = 0
        barrier.wait()
        warmup_deadline = time.monotonic() + warmup_seconds
        finish = warmup_deadline + duration_seconds
        while time.monotonic() < finish:
            measured = time.monotonic() >= warmup_deadline
            sandbox_id = sandbox_ids[(worker_index + counter) % len(sandbox_ids)]
            request_id = f"perf-{run_id}-{worker_index}-{counter}"
            base_path = "/direct/" + urllib.parse.quote(sandbox_id, safe="")
            payload: bytes | None = None
            method = "GET"
            path = base_path + "/healthz"
            if operation == "command":
                request = {
                    "action": "process.exec",
                    "args": {"cmd": "true", "envs": None, "cwd": None, "timeout": 5},
                    "requestId": request_id,
                }
                payload = json.dumps(request, separators=(",", ":")).encode()
                method = "POST"
                path = base_path + "/invoke"
            started = time.perf_counter()
            try:
                if connection is None:
                    connection = _connection(parsed, timeout_seconds)
                headers = _headers(token, request_id=request_id, direct=True)
                if payload is not None:
                    headers["Content-Length"] = str(len(payload))
                connection.request(method, path, body=payload, headers=headers)
                response = connection.getresponse()
                content = response.read()
                elapsed = time.perf_counter() - started
                if measured:
                    local_statuses[str(response.status)] += 1
                if operation == "command":
                    _validate_command_response(response.status, content)
                else:
                    _validate_health_response(response.status, content)
                if measured:
                    local_latencies.append(elapsed)
                    local_per_sandbox[sandbox_id] += 1
                if response.will_close:
                    connection.close()
                    connection = None
            except Exception as error:  # benchmark must count and clean up
                if measured:
                    local_errors[type(error).__name__] += 1
                if connection is not None:
                    connection.close()
                    connection = None
            counter += 1
        if connection is not None:
            connection.close()
        with lock:
            latencies.extend(local_latencies)
            statuses.update(local_statuses)
            errors.update(local_errors)
            per_sandbox.update(local_per_sandbox)

    threads = [
        threading.Thread(target=worker, args=(index,), daemon=True)
        for index in range(concurrency)
    ]
    for thread in threads:
        thread.start()
    cpu_before = _cpu_seconds()
    barrier.wait()
    wall_started = time.monotonic()
    for thread in threads:
        thread.join()
    wall_seconds = time.monotonic() - wall_started
    cpu_seconds = _cpu_seconds() - cpu_before
    succeeded = len(latencies)
    failed = sum(errors.values())
    measured_seconds = min(duration_seconds, max(0.001, wall_seconds - warmup_seconds))
    return {
        "operation": operation,
        "sandbox_count": len(sandbox_ids),
        "concurrency": concurrency,
        "warmup_seconds": warmup_seconds,
        "duration_seconds": duration_seconds,
        "wall_seconds": round(wall_seconds, 3),
        "succeeded": succeeded,
        "failed": failed,
        "requests_per_second": round(succeeded / measured_seconds, 2),
        "error_rate": round(failed / max(1, succeeded + failed), 6),
        "status_codes": dict(sorted(statuses.items())),
        "errors": dict(sorted(errors.items())),
        "latency_ms": _latency_summary(latencies),
        "per_sandbox_succeeded": dict(sorted(per_sandbox.items())),
        "generator_cpu_seconds": round(cpu_seconds, 3),
        "generator_cpu_cores": round(cpu_seconds / max(wall_seconds, 0.001), 3),
    }


def _run_phase_worker(arguments: dict[str, Any]) -> dict[str, Any]:
    return run_phase(**arguments)


def run_phase_multi(
    *,
    origin: str,
    token: str,
    sandbox_ids: list[str],
    concurrency: int,
    warmup_seconds: float,
    duration_seconds: float,
    timeout_seconds: float,
    run_id: str,
    operation: str = "command",
    generator_processes: int = 1,
) -> dict[str, Any]:
    """Run one phase with independent Python generators and aggregate throughput."""

    if generator_processes < 1:
        raise ValueError("generator_processes must be positive")
    process_count = min(generator_processes, concurrency)
    base, remainder = divmod(concurrency, process_count)
    arguments = [
        {
            "origin": origin,
            "token": token,
            "sandbox_ids": sandbox_ids,
            "concurrency": base + (1 if index < remainder else 0),
            "warmup_seconds": warmup_seconds,
            "duration_seconds": duration_seconds,
            "timeout_seconds": timeout_seconds,
            "run_id": f"{run_id}-p{index}",
            "operation": operation,
        }
        for index in range(process_count)
    ]
    if process_count == 1:
        results = [_run_phase_worker(arguments[0])]
    else:
        with ProcessPoolExecutor(max_workers=process_count) as pool:
            results = list(pool.map(_run_phase_worker, arguments))

    statuses: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    per_sandbox: Counter[str] = Counter()
    for result in results:
        statuses.update(result["status_codes"])
        errors.update(result["errors"])
        per_sandbox.update(result["per_sandbox_succeeded"])
    succeeded = sum(int(result["succeeded"]) for result in results)
    failed = sum(int(result["failed"]) for result in results)
    measured_seconds = max(
        0.001,
        max(float(result["wall_seconds"]) for result in results) - warmup_seconds,
    )
    latency_keys = ("min", "mean", "p50", "p95", "p99", "max")
    latency = {
        key: round(max(float(result["latency_ms"][key]) for result in results), 3)
        for key in latency_keys
    }
    process_results = [
        {
            "concurrency": result["concurrency"],
            "succeeded": result["succeeded"],
            "failed": result["failed"],
            "requests_per_second": result["requests_per_second"],
            "latency_ms": result["latency_ms"],
            "generator_cpu_cores": result["generator_cpu_cores"],
        }
        for result in results
    ]
    return {
        "operation": operation,
        "sandbox_count": len(sandbox_ids),
        "concurrency": concurrency,
        "generator_processes": process_count,
        "warmup_seconds": warmup_seconds,
        "duration_seconds": duration_seconds,
        "wall_seconds": round(
            max(float(result["wall_seconds"]) for result in results), 3
        ),
        "succeeded": succeeded,
        "failed": failed,
        "requests_per_second": round(succeeded / measured_seconds, 2),
        "error_rate": round(failed / max(1, succeeded + failed), 6),
        "status_codes": dict(sorted(statuses.items())),
        "errors": dict(sorted(errors.items())),
        "latency_ms": latency,
        "latency_aggregation": "maximum of per-process quantiles",
        "per_sandbox_succeeded": dict(sorted(per_sandbox.items())),
        "generator_cpu_seconds": round(
            sum(float(result["generator_cpu_seconds"]) for result in results), 3
        ),
        "generator_cpu_cores": round(
            sum(float(result["generator_cpu_cores"]) for result in results), 3
        ),
        "process_results": process_results,
    }


def _csv_ints(value: str) -> list[int]:
    items = sorted(set(int(item) for item in value.split(",") if item.strip()))
    if not items or items[0] < 1:
        raise argparse.ArgumentTypeError("expected positive comma-separated integers")
    return items


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _instance_id(item: dict[str, Any]) -> str:
    return str(
        item.get("id") or item.get("instance_id") or item.get("instanceId") or ""
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--token-env", default="BENCH_TOKEN")
    parser.add_argument("--target-node", required=True)
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument(
        "--sandbox-counts", type=_csv_ints, default=_csv_ints("1,2,4,8,16,24")
    )
    parser.add_argument(
        "--concurrency-factors", type=_csv_ints, default=_csv_ints("1,2,4,8")
    )
    parser.add_argument("--max-concurrency", type=int, default=128)
    parser.add_argument("--warmup-seconds", type=float, default=2)
    parser.add_argument("--duration-seconds", type=float, default=8)
    parser.add_argument("--cooldown-seconds", type=float, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--cpu", type=int, default=1000)
    parser.add_argument("--memory", type=int, default=2048)
    parser.add_argument("--idle-timeout", type=int, default=900)
    parser.add_argument("--operation", choices=("command", "health"), default="command")
    parser.add_argument("--generator-processes", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("/results/summary.json"))
    args = parser.parse_args()

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        raise SystemExit(f"{args.token_env} is not set")
    run_id = uuid.uuid4().hex[:12]
    created: list[str] = []
    create_records: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    cleanup_errors: list[str] = []
    before = list_instances(args.origin, token)
    before_ids = {_instance_id(item) for item in before if _instance_id(item)}
    fatal_error: str | None = None

    try:
        for sandbox_count in args.sandbox_counts:
            while len(created) < sandbox_count:
                index = len(created) + 1
                name = f"direct-perf-{run_id}-{index}"
                result = create_sandbox(
                    args.origin,
                    token,
                    build_create_body(
                        name=name,
                        node_id=args.target_node,
                        runtime=args.runtime,
                        cpu=args.cpu,
                        memory=args.memory,
                        idle_timeout=args.idle_timeout,
                    ),
                    request_id=f"create-{run_id}-{index}",
                    timeout_seconds=120,
                )
                sandbox_id = str(result.get("sandboxId") or result.get("instanceId"))
                reported_node = str(result.get("nodeId") or result.get("node_id") or "")
                if reported_node and reported_node != args.target_node:
                    raise RuntimeError(
                        f"Sandbox {sandbox_id} was assigned to {reported_node}, "
                        f"expected {args.target_node}"
                    )
                created.append(sandbox_id)
                create_records.append(
                    {
                        "sandbox_id": sandbox_id,
                        "requested_node": args.target_node,
                        "reported_node": reported_node or None,
                    }
                )
                print(
                    f"created {len(created)}/{max(args.sandbox_counts)} "
                    f"sandbox_id={sandbox_id} "
                    f"node={reported_node or 'not-in-response'}",
                    flush=True,
                )

            active = created[:sandbox_count]
            concurrencies = sorted(
                {
                    min(args.max_concurrency, sandbox_count * factor)
                    for factor in args.concurrency_factors
                }
            )
            for concurrency in concurrencies:
                phase = run_phase_multi(
                    origin=args.origin,
                    token=token,
                    sandbox_ids=active,
                    concurrency=concurrency,
                    warmup_seconds=args.warmup_seconds,
                    duration_seconds=args.duration_seconds,
                    timeout_seconds=args.timeout_seconds,
                    run_id=run_id,
                    operation=args.operation,
                    generator_processes=args.generator_processes,
                )
                phases.append(phase)
                print(
                    "phase "
                    f"sandboxes={sandbox_count} concurrency={concurrency} "
                    f"qps={phase['requests_per_second']} "
                    f"p99_ms={phase['latency_ms']['p99']} "
                    f"failed={phase['failed']} "
                    f"client_cores={phase['generator_cpu_cores']}",
                    flush=True,
                )
                if args.cooldown_seconds > 0:
                    time.sleep(args.cooldown_seconds)
    except Exception as error:  # preserve cleanup and evidence
        fatal_error = f"{type(error).__name__}: {error}"
    finally:
        for sandbox_id in reversed(created):
            try:
                delete_sandbox(args.origin, token, sandbox_id)
            except Exception as error:  # keep deleting the rest
                cleanup_errors.append(f"{sandbox_id}: {type(error).__name__}: {error}")

    after: list[dict[str, Any]] = []
    residue = set(created)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            after = list_instances(args.origin, token)
            after_ids = {_instance_id(item) for item in after if _instance_id(item)}
            residue = set(created).intersection(after_ids)
            if not residue:
                break
        except Exception as error:
            cleanup_errors.append(
                f"list after cleanup: {type(error).__name__}: {error}"
            )
        time.sleep(1)
    after_ids = {_instance_id(item) for item in after if _instance_id(item)}
    peaks: dict[str, dict[str, Any]] = {}
    for sandbox_count in args.sandbox_counts:
        candidates = [
            phase for phase in phases if phase["sandbox_count"] == sandbox_count
        ]
        if candidates:
            best = max(candidates, key=lambda phase: phase["requests_per_second"])
            peaks[str(sandbox_count)] = {
                "requests_per_second": best["requests_per_second"],
                "concurrency": best["concurrency"],
                "p99_ms": best["latency_ms"]["p99"],
                "generator_cpu_cores": best["generator_cpu_cores"],
            }
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "status": (
            "passed"
            if not fatal_error
            and not cleanup_errors
            and not residue
            and phases
            and all(phase["failed"] == 0 for phase in phases)
            else "failed"
        ),
        "scope": (
            "raw Direct Command process.exec; Sandbox lifecycle excluded from QPS"
            if args.operation == "command"
            else "raw Direct RRT healthz; Sandbox lifecycle excluded from QPS"
        ),
        "operation": args.operation,
        "generator_processes": args.generator_processes,
        "origin": args.origin,
        "target_node": args.target_node,
        "runtime": args.runtime,
        "sandbox_resource": {"cpu_millicores": args.cpu, "memory_mib": args.memory},
        "sandbox_counts": args.sandbox_counts,
        "concurrency_factors": args.concurrency_factors,
        "max_concurrency": args.max_concurrency,
        "cooldown_seconds": args.cooldown_seconds,
        "created": create_records,
        "phases": phases,
        "peaks": peaks,
        "fatal_error": fatal_error,
        "cleanup_errors": cleanup_errors,
        "residue": sorted(residue),
        "cluster_instances_before": len(before_ids),
        "cluster_instances_after": len(after_ids),
        "unrelated_new_instances": sorted(after_ids - before_ids - set(created)),
    }
    _write_json(args.output, result)
    print(
        "ADX_DIRECT_COMMAND_RESULT=" + json.dumps(result, separators=(",", ":")),
        flush=True,
    )
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
