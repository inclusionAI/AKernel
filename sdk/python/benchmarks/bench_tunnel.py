"""Measure verified guest-to-SDK-host HTTP through a reverse tunnel."""

import argparse
import http.server
import json
import re
import shlex
import socketserver
import threading
import time
from pathlib import Path
from uuid import uuid4

from akernel_sdk import HttpReverseTunnel, Sandbox
from benchmarks.harness.errors import safe_error
from benchmarks.harness.stats import LatencyHistogram


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self) -> None:
        body = self.payload if self.path == "/health" else b"not found\n"
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def probe_guest(sandbox: Sandbox, marker: str) -> float:
    """Require one guest HTTP request to return the matching host marker."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", marker):
        raise ValueError("invalid marker")
    shell = (
        "exec 3<>/dev/tcp/127.0.0.1/18766; "
        "printf 'GET /health HTTP/1.1\\r\\nHost: localhost\\r\\n"
        "Connection: close\\r\\n\\r\\n' >&3; "
        "while IFS= read -r line <&3; do "
        f'case "$line" in *{marker}*) printf {marker}; exit 0;; esac; '
        "done; exit 1"
    )
    started = time.perf_counter()
    result = sandbox.commands.run("bash -c " + shlex.quote(shell), timeout=20)
    if result.exit_code != 0 or result.stdout != marker:
        raise AssertionError("reverse tunnel response mismatch")
    return time.perf_counter() - started


def run_benchmark(*, runtime: str, iterations: int, run_id: str) -> dict:
    if iterations < 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", run_id):
        raise ValueError("invalid iterations or run_id")
    marker = f"TUNNEL_{run_id}"
    handler = type("RunHandler", (_Handler,), {"payload": f"{marker}\n".encode()})
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    tunnel = HttpReverseTunnel(
        target=f"http://127.0.0.1:{server.server_address[1]}",
        reverse_port=18765,
        listen_port=18766,
    )
    latency = LatencyHistogram()
    failures: list[str] = []
    completed = 0
    try:
        with Sandbox(
            runtime=runtime, cpu=1000, memory=2048, reverse_tunnel=tunnel
        ) as sandbox:
            deadline = time.monotonic() + 30
            while True:
                try:
                    probe_guest(sandbox, marker)
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.25)
            for _ in range(iterations):
                try:
                    latency.observe(probe_guest(sandbox, marker))
                except Exception as error:
                    failures.append(safe_error(error))
                    break
                completed += 1
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "passed" if not failures else "failed",
        "runtime": runtime,
        "iterations_requested": iterations,
        "completed": completed,
        "failures": failures,
        "latency_ms": latency.summary(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AKernel SDK reverse-tunnel benchmark")
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--run-id", default=uuid4().hex)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run_benchmark(
            runtime=args.runtime, iterations=args.iterations, run_id=args.run_id
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
    print(f"tunnel benchmark status={result['status']} result={args.output}")
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
