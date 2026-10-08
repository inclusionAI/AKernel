"""Measure an RRT Unix-socket checkpoint and same-ID SDK reload."""

import argparse
import json
import os
import re
import shlex
import time
from pathlib import Path
from uuid import uuid4

from akernel_sdk import Sandbox
from benchmarks.harness.errors import safe_error
from benchmarks.harness.stats import LatencyHistogram


def checkpoint_command(socket_path: str) -> str:
    """Use the guest's Python runtime to call the RRT Unix HTTP endpoint."""
    script = (
        "import http.client, socket, sys\n"
        "class UnixConnection(http.client.HTTPConnection):\n"
        " def connect(self):\n"
        "  self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "  self.sock.settimeout(300)\n"
        f"  self.sock.connect({socket_path!r})\n"
        "connection = UnixConnection('localhost', timeout=300)\n"
        "connection.request('POST', '/checkpoint')\n"
        "response = connection.getresponse()\n"
        "body = response.read()\n"
        "connection.close()\n"
        "if response.status != 200:\n"
        " sys.stderr.write(body.decode(errors='replace'))\n"
        " sys.exit(1)\n"
        "sys.stdout.write(body.decode())\n"
    )
    return "python3 -c " + shlex.quote(script)


def verify_checkpoint_response(output: str) -> None:
    """Reject incomplete checkpoint responses before attempting reload."""
    response = json.loads(output)
    if response.get("status") != "completed":
        raise AssertionError("checkpoint did not complete")


def run_benchmark(
    *,
    image: str,
    runtime: str,
    socket_path: str,
    iterations: int,
    run_id: str,
) -> dict:
    if not image or iterations < 1:
        raise ValueError("Python OCI image and positive iterations are required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", run_id):
        raise ValueError("invalid run_id")
    if not socket_path.startswith("/"):
        raise ValueError("checkpoint socket must be an absolute path")
    checkpoint_latency = LatencyHistogram()
    reload_latency = LatencyHistogram()
    failures: list[str] = []
    completed = 0
    with Sandbox(
        image=image,
        runtime=runtime,
        cpu=1000,
        memory=2048,
        storage_mb=256,
        failover=True,
    ) as sandbox:
        sandbox_id = sandbox.id
        path = f"/tmp/akernel-checkpoint-{run_id}"
        for sequence in range(iterations):
            marker = f"checkpoint_{run_id}_{sequence}"
            try:
                sandbox.files.write(path, marker)
                started = time.perf_counter()
                response = sandbox.commands.run(
                    checkpoint_command(socket_path), timeout=300
                )
                if response.exit_code != 0:
                    raise AssertionError("checkpoint command failed")
                verify_checkpoint_response(response.stdout)
                checkpoint_latency.observe(time.perf_counter() - started)

                sandbox.files.write(path, "after-checkpoint")
                started = time.perf_counter()
                if not sandbox.reload():
                    raise AssertionError("reload returned false")
                if sandbox.id != sandbox_id or sandbox.files.read(path) != marker:
                    raise AssertionError("checkpoint restore state mismatch")
                reload_latency.observe(time.perf_counter() - started)
            except Exception as error:
                failures.append(safe_error(error))
                break
            completed += 1
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "passed" if not failures else "failed",
        "runtime": runtime,
        "image": image,
        "socket_path": socket_path,
        "iterations_requested": iterations,
        "completed": completed,
        "failures": failures,
        "checkpoint_latency_ms": checkpoint_latency.summary(),
        "reload_latency_ms": reload_latency.summary(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AKernel SDK checkpoint benchmark")
    parser.add_argument(
        "--image", default=os.getenv("AKERNEL_TEST_CHECKPOINT_IMAGE", "")
    )
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument(
        "--socket-path",
        default=os.getenv("AKERNEL_TEST_CHECKPOINT_SOCKET", "/run/akernel/execd.sock"),
    )
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--run-id", default=uuid4().hex)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run_benchmark(
            image=args.image,
            runtime=args.runtime,
            socket_path=args.socket_path,
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
    print(f"checkpoint benchmark status={result['status']} result={args.output}")
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
