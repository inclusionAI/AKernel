"""Verified public port-forward request baseline on one resident sandbox."""

import argparse
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

from akernel_sdk import Sandbox
from benchmarks.harness.errors import safe_error
from benchmarks.harness.stats import LatencyHistogram


def run_http_case(url: str, expected: bytes) -> dict:
    """Complete one request and verify status and exact instance payload."""
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read()
        if response.status != 200:
            raise AssertionError(f"HTTP status {response.status}")
    if body != expected:
        raise AssertionError("HTTP body mismatch")
    return {"elapsed_seconds": time.perf_counter() - started, "bytes": len(body)}


def run_benchmark(
    *,
    image: str,
    runtime: str,
    port: int,
    iterations: int,
    run_id: str,
) -> dict:
    if not image or iterations < 1 or not 1 <= port <= 65535:
        raise ValueError("image, iterations and valid port are required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise ValueError("invalid run_id")
    marker = f"AKERNEL_HTTP_{run_id}\n".encode()
    latency = LatencyHistogram()
    errors = []
    completed = 0
    with Sandbox(
        image=image,
        runtime=runtime,
        cpu=1000,
        memory=2048,
        port_forwardings=[port],
    ) as sandbox:
        sandbox.files.write("/tmp/index.html", marker)
        server = sandbox.commands.run(
            f"python3 -m http.server {port} --bind 0.0.0.0 --directory /tmp",
            background=True,
        )
        try:
            url = sandbox.get_port_url(port)
            deadline = time.monotonic() + 30
            while True:
                try:
                    run_http_case(url, marker)
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.25)

            for _ in range(iterations):
                try:
                    measurement = run_http_case(url, marker)
                except Exception as error:
                    errors.append(safe_error(error))
                    break
                latency.observe(measurement["elapsed_seconds"])
                completed += 1
        finally:
            server.kill()

    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "passed" if not errors else "failed",
        "config": {
            "image": image,
            "runtime": runtime,
            "port": port,
            "iterations_requested": iterations,
        },
        "completed": completed,
        "failures": errors,
        "latency_ms": latency.summary(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AKernel SDK port-forward benchmark")
    parser.add_argument("--image", default=os.getenv("AKERNEL_TEST_HTTP_IMAGE", ""))
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--run-id", default=uuid4().hex)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run_benchmark(
            image=args.image,
            runtime=args.runtime,
            port=args.port,
            iterations=args.iterations,
            run_id=args.run_id,
        )
    except Exception as error:
        result = {
            "schema_version": 1,
            "run_id": args.run_id,
            "status": "driver_error",
            "error": safe_error(error),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(f"port benchmark status={result['status']} result={args.output}")
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
