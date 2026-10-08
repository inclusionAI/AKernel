# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

"""Closed-loop raw HTTP throughput benchmark for the deployed API Server."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import resource
import ssl
import statistics
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit


@dataclass(frozen=True)
class Case:
    """One HTTP path and its response contract."""

    name: str
    url: str
    auth: str
    expect: str

    @classmethod
    def parse(cls, value: str) -> Case:
        parts = value.split("|", 3)
        if len(parts) != 4:
            raise ValueError("case must be NAME|URL|AUTH|EXPECT")
        case = cls(*parts)
        if not case.name or case.auth not in {"none", "bearer"}:
            raise ValueError("case name is required and auth must be none or bearer")
        if case.expect not in {"text", "json-object", "json-array"}:
            raise ValueError("unsupported response expectation")
        parsed = urlsplit(case.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("case URL must be absolute HTTP(S)")
        return case


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _latency_summary(values: list[float]) -> dict[str, float]:
    milliseconds = [value * 1000 for value in values]
    if not milliseconds:
        return {key: 0.0 for key in ("mean", "p50", "p95", "p99", "max")}
    return {
        "mean": round(statistics.fmean(milliseconds), 3),
        "p50": round(_percentile(milliseconds, 0.50), 3),
        "p95": round(_percentile(milliseconds, 0.95), 3),
        "p99": round(_percentile(milliseconds, 0.99), 3),
        "max": round(max(milliseconds), 3),
    }


def _connection(parsed: SplitResult, timeout: float) -> http.client.HTTPConnection:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(
            parsed.hostname, port, timeout=timeout, context=context
        )
    return http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)


def _validate(body: bytes, expectation: str) -> None:
    if expectation == "text":
        if not body:
            raise ValueError("empty response")
        return
    value = json.loads(body)
    expected_type = dict if expectation == "json-object" else list
    if not isinstance(value, expected_type):
        raise ValueError(f"expected {expectation}")


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def run_phase(
    case: Case,
    *,
    token: str,
    concurrency: int,
    warmup_seconds: float,
    duration_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one closed-loop phase with one persistent connection per worker."""
    token = token.strip()
    if concurrency < 1 or warmup_seconds < 0 or duration_seconds <= 0:
        raise ValueError("invalid phase limits")
    if case.auth == "bearer" and not token:
        raise ValueError("bearer case requires a token")

    parsed = urlsplit(case.url)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    headers = {"Accept": "application/json", "Connection": "keep-alive"}
    if case.auth == "bearer":
        headers["Authorization"] = "Bearer " + token

    barrier = threading.Barrier(concurrency + 1)
    lock = threading.Lock()
    latencies: list[float] = []
    status_codes: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    bytes_received = 0

    def worker() -> None:
        nonlocal bytes_received
        local_latencies: list[float] = []
        local_status: Counter[str] = Counter()
        local_errors: Counter[str] = Counter()
        local_bytes = 0
        connection: http.client.HTTPConnection | None = None
        barrier.wait()
        warmup_deadline = time.monotonic() + warmup_seconds
        finish = warmup_deadline + duration_seconds
        while time.monotonic() < finish:
            measured = time.monotonic() >= warmup_deadline
            started = time.perf_counter()
            try:
                if connection is None:
                    connection = _connection(parsed, timeout_seconds)
                connection.request("GET", target, headers=headers)
                response = connection.getresponse()
                body = response.read()
                elapsed = time.perf_counter() - started
                if measured:
                    local_status[str(response.status)] += 1
                    local_bytes += len(body)
                if not 200 <= response.status < 300:
                    raise ValueError(f"HTTP {response.status}")
                _validate(body, case.expect)
                if measured:
                    local_latencies.append(elapsed)
                if response.will_close:
                    connection.close()
                    connection = None
            except Exception as error:  # benchmark must count and continue
                if measured:
                    local_errors[type(error).__name__] += 1
                if connection is not None:
                    connection.close()
                    connection = None
        if connection is not None:
            connection.close()
        with lock:
            latencies.extend(local_latencies)
            status_codes.update(local_status)
            errors.update(local_errors)
            bytes_received += local_bytes

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    cpu_before = _cpu_seconds()
    barrier.wait()
    wall_started = time.monotonic()
    for thread in threads:
        thread.join()
    wall_elapsed = time.monotonic() - wall_started
    cpu_elapsed = _cpu_seconds() - cpu_before
    succeeded = len(latencies)
    failed = sum(errors.values())
    measured_elapsed = min(duration_seconds, max(0.001, wall_elapsed - warmup_seconds))
    return {
        "case": case.name,
        "concurrency": concurrency,
        "warmup_seconds": warmup_seconds,
        "duration_seconds": duration_seconds,
        "wall_seconds": round(wall_elapsed, 3),
        "succeeded": succeeded,
        "failed": failed,
        "requests_per_second": round(succeeded / measured_elapsed, 2),
        "error_rate": round(failed / max(1, succeeded + failed), 6),
        "status_codes": dict(sorted(status_codes.items())),
        "errors": dict(sorted(errors.items())),
        "bytes_received": bytes_received,
        "latency_ms": _latency_summary(latencies),
        "generator_cpu_seconds": round(cpu_elapsed, 3),
        "generator_cpu_cores": round(cpu_elapsed / max(wall_elapsed, 0.001), 3),
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", required=True, type=Case.parse)
    parser.add_argument("--concurrency", default="1,4,16,32,64,128")
    parser.add_argument("--warmup-seconds", type=float, default=2)
    parser.add_argument("--duration-seconds", type=float, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--token-env", default="AKERNEL_TOKEN")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    concurrency = [int(value) for value in args.concurrency.split(",")]
    token = os.environ.get(args.token_env, "")
    result: dict[str, Any] = {
        "schema_version": 1,
        "started_at_unix_seconds": int(time.time()),
        "mode": "closed_loop_raw_http_1_1_keepalive",
        "config": {
            "cases": [
                asdict(case) | {"url": urlsplit(case.url).path} for case in args.case
            ],
            "concurrency": concurrency,
            "warmup_seconds": args.warmup_seconds,
            "duration_seconds": args.duration_seconds,
            "timeout_seconds": args.timeout_seconds,
        },
        "phases": [],
    }
    for case in args.case:
        for count in concurrency:
            phase = run_phase(
                case,
                token=token,
                concurrency=count,
                warmup_seconds=args.warmup_seconds,
                duration_seconds=args.duration_seconds,
                timeout_seconds=args.timeout_seconds,
            )
            result["phases"].append(phase)
            print(
                f"case={case.name} concurrency={count} "
                f"rps={phase['requests_per_second']} "
                f"p99_ms={phase['latency_ms']['p99']} "
                f"failed={phase['failed']}",
                flush=True,
            )
    result["status"] = (
        "passed"
        if all(phase["failed"] == 0 for phase in result["phases"])
        else "failed"
    )
    _write_json(args.output, result)
    print(
        "RESULT_JSON=" + json.dumps(result, separators=(",", ":")),
        flush=True,
    )
    print(f"status={result['status']} output={args.output}", flush=True)
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
