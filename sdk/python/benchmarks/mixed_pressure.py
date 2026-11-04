"""Bounded resident IO plus lifecycle churn through the public AKernel SDK."""

import argparse
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from akernel_sdk import Sandbox
from benchmarks.harness.errors import safe_error
from benchmarks.harness.load import run_open_loop
from benchmarks.harness.stats import LatencyHistogram
from benchmarks.sandbox_pressure import _new_stats, _write_result, run_single_request


def run_resident_cycle(sandbox: Sandbox, run_id: str, sequence: int) -> None:
    """Exercise command and file IO on a still-running resident sandbox."""
    result = sandbox.commands.run("printf resident-ok", timeout=20)
    if result.exit_code != 0 or result.stdout != "resident-ok":
        raise AssertionError("resident command result mismatch")
    path = f"/tmp/akernel-mixed-{run_id}.txt"
    payload = f"{run_id}:{sequence}"
    sandbox.files.write(path, payload)
    if sandbox.files.read(path) != payload:
        raise AssertionError("resident file data mismatch")


def run_mixed(
    *,
    run_id: str,
    duration: float,
    residents: int,
    resident_interval: float,
    churn_rps: float,
    max_inflight: int,
    sandbox_kwargs: dict,
) -> dict:
    """Run separate resident and churn pools, then drain and delete everything."""
    if (
        duration <= 0
        or residents < 1
        or resident_interval < 0
        or churn_rps <= 0
        or max_inflight < 1
    ):
        raise ValueError("mixed load limits must be positive")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise ValueError("run_id must contain only letters, digits, _ or -")

    active: list[Sandbox] = []
    resident_errors: list[str] = []
    resident_latencies = LatencyHistogram()
    resident_lock = threading.Lock()
    resident_succeeded = 0
    resident_failed = 0
    stop = threading.Event()
    churn_stats = _new_stats()
    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()

    def resident_loop(sandbox: Sandbox) -> None:
        nonlocal resident_succeeded, resident_failed
        sequence = 0
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                run_resident_cycle(sandbox, run_id, sequence)
            except Exception as error:
                with resident_lock:
                    resident_failed += 1
                    if len(resident_errors) < 10:
                        resident_errors.append(safe_error(error))
                stop.set()
                return
            with resident_lock:
                resident_succeeded += 1
                resident_latencies.observe(time.perf_counter() - t0)
            sequence += 1
            stop.wait(resident_interval)

    cleanup_errors: list[str] = []
    arrivals = None
    try:
        for _ in range(residents):
            active.append(Sandbox(**sandbox_kwargs))
        with ThreadPoolExecutor(max_workers=residents) as pool:
            futures = [pool.submit(resident_loop, sandbox) for sandbox in active]
            try:
                arrivals = run_open_loop(
                    duration=duration,
                    target_rps=churn_rps,
                    max_inflight=max_inflight,
                    operation=lambda: run_single_request(
                        churn_stats,
                        None,
                        sandbox_kwargs,
                        "/bin/true",
                        20,
                        False,
                    ),
                    abort=stop.is_set,
                )
            finally:
                stop.set()
                for future in futures:
                    future.result()
    finally:
        stop.set()
        for sandbox in active:
            try:
                sandbox.kill()
            except Exception as error:
                cleanup_errors.append(safe_error(error))

    elapsed = time.perf_counter() - start
    assert arrivals is not None
    return {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at,
        "status": (
            "passed"
            if not resident_failed and not churn_stats["failed"] and not cleanup_errors
            else "failed"
        ),
        "duration_seconds": elapsed,
        "config": {
            "resident_count": residents,
            "resident_interval_seconds": resident_interval,
            "churn_target_rps": churn_rps,
            "churn_max_inflight": max_inflight,
            "runtime": sandbox_kwargs.get("runtime"),
        },
        "resident": {
            "succeeded": resident_succeeded,
            "failed": resident_failed,
            "latency_ms": resident_latencies.summary(),
            "error_samples": resident_errors,
            "cleanup_errors": cleanup_errors,
        },
        "churn": {
            "arrival": vars(arrivals),
            "attempted": churn_stats["total"],
            "succeeded": churn_stats["success"],
            "failed": churn_stats["failed"],
            "errors_by_phase": churn_stats["errors_by_phase"],
            "error_samples": churn_stats["errors"],
            "latency_ms": churn_stats["latencies"].summary(),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AKernel SDK mixed load")
    parser.add_argument("--run-id", default=uuid4().hex)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--residents", type=int, default=2)
    parser.add_argument("--resident-interval", type=float, default=0.1)
    parser.add_argument("--churn-rps", type=float, default=2.0)
    parser.add_argument("--max-inflight", type=int, default=4)
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument("--cpu", type=int, default=1000)
    parser.add_argument("--memory", type=int, default=2048)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        result = run_mixed(
            run_id=args.run_id,
            duration=args.duration,
            residents=args.residents,
            resident_interval=args.resident_interval,
            churn_rps=args.churn_rps,
            max_inflight=args.max_inflight,
            sandbox_kwargs={
                "runtime": args.runtime,
                "cpu": args.cpu,
                "memory": args.memory,
            },
        )
    except Exception as error:
        result = {
            "schema_version": 1,
            "run_id": args.run_id,
            "status": "driver_error",
            "error": safe_error(error),
        }
    _write_result(args.output, result)
    print(f"mixed load status={result['status']} result={args.output}")
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
