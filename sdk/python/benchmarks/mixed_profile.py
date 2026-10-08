"""Seven-class public-SDK mixed load with separate arrival and result accounting."""

import argparse
import itertools
import os
import re
import socketserver
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from akernel_sdk import HttpReverseTunnel, Sandbox
from benchmarks.bench_checkpoint import checkpoint_command, verify_checkpoint_response
from benchmarks.bench_http import run_http_case
from benchmarks.bench_pty import run_pty_case
from benchmarks.bench_tunnel import _Handler, probe_guest
from benchmarks.harness.errors import safe_error
from benchmarks.harness.load import run_open_loop
from benchmarks.harness.stats import LatencyHistogram
from benchmarks.sandbox_pressure import _write_result

PROFILES = {
    "interactive": {
        "command": 30,
        "http": 20,
        "file": 15,
        "lifecycle": 15,
        "pty": 10,
        "tunnel": 5,
        "checkpoint": 5,
    },
    "io-heavy": {
        "command": 15,
        "http": 15,
        "file": 30,
        "lifecycle": 10,
        "pty": 10,
        "tunnel": 5,
        "checkpoint": 15,
    },
    "churn-heavy": {
        "command": 20,
        "http": 15,
        "file": 10,
        "lifecycle": 35,
        "pty": 5,
        "tunnel": 5,
        "checkpoint": 10,
    },
}

# Each logical class has its own in-flight budget. Checkpoint is serialized
# because it rewinds its dedicated Sandbox while other classes continue.
CLASS_LIMITS = {
    "command": 4,
    "http": 4,
    "file": 2,
    "lifecycle": 2,
    "pty": 2,
    "tunnel": 1,
    "checkpoint": 1,
}

FILE_BYTES = {"interactive": 4096, "io-heavy": 1048576, "churn-heavy": 1024}
CHECKPOINT_BYTES = {"interactive": 0, "io-heavy": 1048576, "churn-heavy": 0}


def run_profile(
    *,
    profile: str,
    duration: float,
    target_rps: float,
    operations: dict,
) -> dict:
    """Run all seven logical classes concurrently and retain per-class results."""
    if profile not in PROFILES:
        raise ValueError(f"unknown mixed profile: {profile}")
    if duration <= 0 or target_rps <= 0:
        raise ValueError("duration and target_rps must be positive")
    missing = set(PROFILES[profile]) - set(operations)
    if missing:
        raise ValueError(f"missing operations: {', '.join(sorted(missing))}")

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()

    def run_class(name: str, weight: int) -> tuple[str, dict]:
        histogram = LatencyHistogram()
        succeeded = failed = 0
        errors: list[str] = []

        def operation() -> None:
            nonlocal succeeded, failed
            began = time.perf_counter()
            try:
                operations[name]()
            except Exception as error:
                failed += 1
                if len(errors) < 10:
                    errors.append(safe_error(error))
            else:
                succeeded += 1
                histogram.observe(time.perf_counter() - began)

        arrivals = run_open_loop(
            duration=duration,
            target_rps=target_rps * weight / 100,
            max_inflight=CLASS_LIMITS[name],
            operation=operation,
        )
        return name, {
            "weight_percent": weight,
            "target_rps": target_rps * weight / 100,
            "max_inflight": CLASS_LIMITS[name],
            "arrival": vars(arrivals),
            "succeeded": succeeded,
            "failed": failed,
            "error_samples": errors,
            "latency_ms": histogram.summary(),
        }

    with ThreadPoolExecutor(max_workers=len(PROFILES[profile])) as pool:
        futures = [
            pool.submit(run_class, name, weight)
            for name, weight in PROFILES[profile].items()
        ]
        classes = dict(future.result() for future in futures)
    return {
        "schema_version": 1,
        "started_at": started_at,
        "duration_seconds": time.perf_counter() - start,
        "status": (
            "passed"
            if all(
                result["arrival"]["submitted"] > 0 and result["failed"] == 0
                for result in classes.values()
            )
            else "failed"
        ),
        "profile": profile,
        "target_rps": target_rps,
        "classes": classes,
    }


class _MixedFixtures:
    """Own four independent Sandboxes and the two network services."""

    def __init__(
        self,
        *,
        run_id: str,
        runtime: str,
        image: str,
        port: int,
        socket_path: str,
        profile: str,
    ) -> None:
        self.run_id = run_id
        self.runtime = runtime
        self.image = image
        self.port = port
        self.socket_path = socket_path
        self.profile = profile
        self._stack = ExitStack()
        self._sequences = {name: itertools.count() for name in CLASS_LIMITS}
        self._checkpoint_lock = threading.Lock()
        self._cleanup_errors: list[str] = []

    def __enter__(self):
        try:
            self.resident = self._stack.enter_context(
                Sandbox(runtime=self.runtime, cpu=1000, memory=2048)
            )
            self.http_sandbox = self._stack.enter_context(
                Sandbox(
                    image=self.image,
                    runtime=self.runtime,
                    cpu=1000,
                    memory=2048,
                    port_forwardings=[self.port],
                )
            )
            self._http_payload = f"MIXED_HTTP_{self.run_id}\n".encode()
            self.http_sandbox.files.write("/tmp/index.html", self._http_payload)
            server = self.http_sandbox.commands.run(
                f"python3 -m http.server {self.port} --bind 0.0.0.0 --directory /tmp",
                background=True,
            )
            self._stack.callback(server.kill)
            self._http_url = self.http_sandbox.get_port_url(self.port)
            self._wait_ready(lambda: run_http_case(self._http_url, self._http_payload))

            marker = f"MIXED_TUNNEL_{self.run_id}"
            handler = type(
                "MixedHandler", (_Handler,), {"payload": f"{marker}\n".encode()}
            )
            host = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
            host.daemon_threads = True
            thread = threading.Thread(target=host.serve_forever, daemon=True)
            thread.start()
            self._stack.callback(thread.join, 5)
            self._stack.callback(host.server_close)
            self._stack.callback(host.shutdown)
            tunnel = HttpReverseTunnel(
                target=f"http://127.0.0.1:{host.server_address[1]}",
                reverse_port=18765,
                listen_port=18766,
            )
            self.tunnel_sandbox = self._stack.enter_context(
                Sandbox(
                    runtime=self.runtime,
                    cpu=1000,
                    memory=2048,
                    reverse_tunnel=tunnel,
                )
            )
            self._tunnel_marker = marker
            self._wait_ready(lambda: probe_guest(self.tunnel_sandbox, marker))

            self.checkpoint_sandbox = self._stack.enter_context(
                Sandbox(
                    image=self.image,
                    runtime=self.runtime,
                    cpu=1000,
                    memory=2048,
                    storage_mb=256,
                    failover=True,
                )
            )
            self._checkpoint_id = self.checkpoint_sandbox.id
        except Exception:
            self._stack.close()
            raise
        return self

    def __exit__(self, *_exc):
        try:
            self._stack.close()
        except Exception as error:
            self._cleanup_errors.append(safe_error(error))

    @property
    def cleanup_errors(self) -> list[str]:
        return self._cleanup_errors

    @staticmethod
    def _wait_ready(probe) -> None:
        deadline = time.monotonic() + 30
        while True:
            try:
                probe()
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)

    def operations(self) -> dict:
        return {name: getattr(self, name) for name in CLASS_LIMITS}

    def command(self) -> None:
        marker = f"MIXED_COMMAND_{self.run_id}_{next(self._sequences['command'])}"
        if self.profile == "io-heavy":
            command = f"printf {marker}; head -c 65536 /dev/zero | tr '\\000' x"
            expected = marker + "x" * 65536
        else:
            command = f"printf {marker}"
            expected = marker
        result = self.resident.commands.run(command, timeout=20)
        if result.exit_code != 0 or result.stdout != expected:
            raise AssertionError("mixed command result mismatch")

    def http(self) -> None:
        run_http_case(self._http_url, self._http_payload)

    def file(self) -> None:
        sequence = next(self._sequences["file"])
        path = f"/tmp/mixed-{self.run_id}-{sequence}.bin"
        prefix = f"MIXED_FILE_{self.run_id}_{sequence}".encode()
        size = FILE_BYTES[self.profile]
        payload = (prefix * (size // len(prefix) + 1))[:size]
        try:
            self.resident.files.write(path, payload)
            if self.resident.files.read(path, format="bytes") != payload:
                raise AssertionError("mixed file content mismatch")
        finally:
            if self.resident.files.exists(path):
                self.resident.files.remove(path)

    def lifecycle(self) -> None:
        marker = f"MIXED_LIFECYCLE_{self.run_id}_{next(self._sequences['lifecycle'])}"
        with Sandbox(runtime=self.runtime, cpu=1000, memory=2048) as sandbox:
            result = sandbox.commands.run(f"printf {marker}", timeout=20)
            if result.exit_code != 0 or result.stdout != marker:
                raise AssertionError("mixed lifecycle command mismatch")

    def pty(self) -> None:
        marker = f"pty_{self.run_id}_{next(self._sequences['pty'])}"
        run_pty_case(self.resident, marker)

    def tunnel(self) -> None:
        probe_guest(self.tunnel_sandbox, self._tunnel_marker)

    def checkpoint(self) -> None:
        with self._checkpoint_lock:
            sequence = next(self._sequences["checkpoint"])
            path = f"/tmp/mixed-checkpoint-{self.run_id}"
            marker = f"MIXED_CP_{self.run_id}_{sequence}"
            content = marker + "K" * CHECKPOINT_BYTES[self.profile]
            self.checkpoint_sandbox.files.write(path, content)
            result = self.checkpoint_sandbox.commands.run(
                checkpoint_command(self.socket_path), timeout=300
            )
            if result.exit_code != 0:
                raise AssertionError("mixed checkpoint command failed")
            verify_checkpoint_response(result.stdout)
            self.checkpoint_sandbox.files.write(path, "after-checkpoint")
            if not self.checkpoint_sandbox.reload():
                raise AssertionError("mixed reload returned false")
            if (
                self.checkpoint_sandbox.id != self._checkpoint_id
                or self.checkpoint_sandbox.files.read(path) != content
            ):
                raise AssertionError("mixed checkpoint restore mismatch")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="interactive")
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--target-rps", type=float, default=2)
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument("--image", default=os.getenv("AKERNEL_TEST_HTTP_IMAGE", ""))
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument(
        "--socket-path",
        default=os.getenv("AKERNEL_TEST_CHECKPOINT_SOCKET", "/run/akernel/execd.sock"),
    )
    parser.add_argument("--run-id", default=uuid4().hex)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args.run_id):
            raise ValueError("run_id must contain 1-32 safe characters")
        if not args.image or not 1 <= args.port <= 65535:
            raise ValueError("HTTP OCI image and valid port are required")
        if not args.socket_path.startswith("/"):
            raise ValueError("checkpoint socket must be absolute")
        with _MixedFixtures(
            run_id=args.run_id,
            runtime=args.runtime,
            image=args.image,
            port=args.port,
            socket_path=args.socket_path,
            profile=args.profile,
        ) as fixtures:
            result = run_profile(
                profile=args.profile,
                duration=args.duration,
                target_rps=args.target_rps,
                operations=fixtures.operations(),
            )
        result["run_id"] = args.run_id
        result["runtime"] = args.runtime
        result["image"] = args.image
        result["cleanup_errors"] = fixtures.cleanup_errors
        if fixtures.cleanup_errors:
            result["status"] = "failed"
    except Exception as error:
        result = {
            "schema_version": 1,
            "run_id": args.run_id,
            "status": "driver_error",
            "error": safe_error(error),
        }
    _write_result(args.output, result)
    print(f"mixed profile status={result['status']} result={args.output}")
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
