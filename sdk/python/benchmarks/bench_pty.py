"""Verified repeated PTY sessions on one resident sandbox."""

import argparse
import json
import re
import threading
import time
from pathlib import Path
from uuid import uuid4

from akernel_sdk import Sandbox
from benchmarks.harness.errors import safe_error
from benchmarks.harness.stats import LatencyHistogram


def run_pty_case(sandbox: Sandbox, marker: str) -> dict:
    """Open a session, capture exact output, and verify normal termination."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", marker):
        raise ValueError("marker must contain only letters, digits, _ or -")
    output = bytearray()
    received = threading.Event()
    expected = marker.encode()
    received_at: float | None = None

    def on_data(data: bytes) -> None:
        nonlocal received_at
        output.extend(data)
        if expected in output and not received.is_set():
            received_at = time.perf_counter()
            received.set()

    started = time.perf_counter()
    with sandbox.pty.create(
        command=["/bin/sh", "-c", f"printf '%s' '{marker}'"],
        on_data=on_data,
        timeout=20,
    ) as session:
        ready = time.perf_counter()
        exit_code = session.wait(timeout=20)
        if exit_code != 0:
            raise AssertionError(f"PTY exit code {exit_code}")
        if not received.wait(timeout=2):
            raise AssertionError("PTY output mismatch")
    completed = time.perf_counter()
    if received_at is None:
        raise AssertionError("PTY output timestamp missing")
    return {
        "connect_seconds": ready - started,
        "output_seconds": received_at - started,
        "full_seconds": completed - started,
        "output_bytes": len(output),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AKernel SDK PTY benchmark")
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument("--cpu", type=int, default=1000)
    parser.add_argument("--memory", type=int, default=2048)
    parser.add_argument("--run-id", default=uuid4().hex)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.iterations < 1:
        raise ValueError("iterations must be positive")
    latency = LatencyHistogram()
    connection = LatencyHistogram()
    output = LatencyHistogram()
    failures = []
    completed = 0
    try:
        with Sandbox(runtime=args.runtime, cpu=args.cpu, memory=args.memory) as sb:
            for sequence in range(args.iterations):
                try:
                    measurement = run_pty_case(sb, f"pty_{args.run_id}_{sequence}")
                except Exception as error:
                    failures.append(safe_error(error))
                    break
                completed += 1
                latency.observe(measurement["full_seconds"])
                connection.observe(measurement["connect_seconds"])
                output.observe(measurement["output_seconds"])
        result = {
            "schema_version": 1,
            "run_id": args.run_id,
            "status": "passed" if not failures else "failed",
            "iterations_requested": args.iterations,
            "completed": completed,
            "failures": failures,
            "latency_ms": latency.summary(),
            "connect_latency_ms": connection.summary(),
            "output_latency_ms": output.summary(),
        }
    except Exception as error:
        result = {
            "schema_version": 1,
            "run_id": args.run_id,
            "status": "driver_error",
            "completed": completed,
            "error": safe_error(error),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(f"PTY benchmark status={result['status']} result={args.output}")
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
