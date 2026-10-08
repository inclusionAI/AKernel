# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pressure benchmark for akernel_sdk.Sandbox.

Spawns sandboxes in parallel via multi-process x multi-thread fan-out, runs a
simple workload inside each, and reports RPS / latency stats.

Modes:
  - default   : workload runs /bin/true to verify command execution
  - --tunnel  : main proc starts a local http.server on 127.0.0.1:<tunnel-port>;
                each sandbox creates an HttpReverseTunnel, then fetches /health
                through the reverse tunnel to verify connectivity end-to-end.

Image:
  By default no image is configured, so the cluster default image is used.
  Override with --image or AKERNEL_PRESSURE_IMAGE.

Usage:
  export AKERNEL_SERVER_ADDRESS=...
  export AKERNEL_TOKEN=...

  # plain (cluster default image)
  python sandbox_pressure.py --processes 2 --threads 4 --duration 30

  # tunnel mode (auto local server)
  python sandbox_pressure.py --tunnel --processes 2 --threads 4 --duration 60

  # custom image / workload
  python sandbox_pressure.py \
      --image python:3.12-slim \
      --cmd 'python3 -c "import urllib.request;print(urllib.request.urlopen(\"https://example.com\",timeout=10).status)"'
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from akernel_sdk import HttpReverseTunnel, Sandbox
from benchmarks.harness.errors import safe_error
from benchmarks.harness.load import run_open_loop
from benchmarks.harness.stats import LatencyHistogram

# Empty => don't configure an image; the cluster default image is used.
DEFAULT_IMAGE = os.environ.get("AKERNEL_PRESSURE_IMAGE", "")
DEFAULT_RUNTIME = os.environ.get("AKERNEL_PRESSURE_RUNTIME", "runsc")

# Keep the default workload independent of optional image tools and external
# network availability. Callers can use --cmd for application-level pressure.
DEFAULT_CMD = "/bin/true"

# Token substituted with ``sandbox.reverse_tunnel.url`` at runtime.
TUNNEL_URL_PLACEHOLDER = "{TUNNEL_URL}"

TUNNEL_CMD_TEMPLATE = (
    "sh -c 'test \"$(wget -qO- " + TUNNEL_URL_PLACEHOLDER + "/health)\" = OK'"
)


def _new_stats():
    return {
        "total": 0,
        "success": 0,
        "failed": 0,
        "errors_by_phase": {"create": 0, "command": 0, "cleanup": 0},
        "latencies": LatencyHistogram(),
        "create_latencies": LatencyHistogram(),
        "command_latencies": LatencyHistogram(),
        "cleanup_latencies": LatencyHistogram(),
        "failure_latencies": LatencyHistogram(),
        "errors": [],
        "arrival": {
            "scheduled": 0,
            "submitted": 0,
            "rejected_inflight": 0,
            "missed_deadline": 0,
        },
        "lock": threading.Lock(),
    }


def _build_sandbox_kwargs(
    image,
    runtime,
    cpu,
    memory,
    cpu_limit,
    mem_limit,
    idle_timeout,
    xpu,
    storage_mb,
    upstream,
    reverse_port,
    listen_port,
):
    kwargs = {
        "runtime": runtime,
        "cpu": cpu,
        "memory": memory,
        "cpu_limit": cpu_limit,
        "mem_limit": mem_limit,
        "idle_timeout": idle_timeout,
    }
    if xpu:
        kwargs["xpu"] = xpu
    if storage_mb is not None:
        kwargs["storage_mb"] = storage_mb
    if image:
        kwargs["image"] = image
    if upstream:
        kwargs["reverse_tunnel"] = HttpReverseTunnel(
            target=upstream,
            reverse_port=reverse_port,
            listen_port=listen_port,
        )
    return kwargs


def run_single_request(stats, _term_pool, sb_kwargs, cmd, cmd_timeout, tunnel_mode):
    t0 = time.perf_counter()
    sb = None
    phase = "create"
    try:
        sb = Sandbox(**sb_kwargs)
        t_created = time.perf_counter()

        if tunnel_mode:
            tunnel_url = sb.reverse_tunnel.url
            real_cmd = cmd.replace(TUNNEL_URL_PLACEHOLDER, tunnel_url)
        else:
            real_cmd = cmd

        phase = "command"
        result = sb.commands.run(real_cmd, timeout=cmd_timeout)
        t_command_done = time.perf_counter()
        if result.exit_code != 0:
            raise RuntimeError(
                f"exit_code={result.exit_code} "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )

        # A complete transaction includes deletion. An asynchronous delete
        # cannot be counted as success before its result is known.
        phase = "cleanup"
        sb.kill()
        sb = None
        elapsed = time.perf_counter() - t0
        with stats["lock"]:
            stats["success"] += 1
            stats["latencies"].observe(elapsed)
            stats["create_latencies"].observe(t_created - t0)
            stats["command_latencies"].observe(t_command_done - t_created)
            stats["cleanup_latencies"].observe(elapsed - (t_command_done - t0))
    except Exception as e:
        cleanup_error = None
        if sb is not None:
            try:
                sb.kill()
            except Exception as error:
                cleanup_error = error
        with stats["lock"]:
            stats["failed"] += 1
            stats["errors_by_phase"][phase] += 1
            stats["failure_latencies"].observe(time.perf_counter() - t0)
            if len(stats["errors"]) < 10:
                message = safe_error(e)
                if cleanup_error is not None:
                    message += f"; cleanup also failed: {safe_error(cleanup_error)}"
                stats["errors"].append(message[:300])
    finally:
        with stats["lock"]:
            stats["total"] += 1


def thread_loop(stats, term_pool, end_time, sb_kwargs, cmd, cmd_timeout, tunnel_mode):
    while time.perf_counter() < end_time:
        run_single_request(
            stats,
            term_pool,
            sb_kwargs,
            cmd,
            cmd_timeout,
            tunnel_mode,
        )


def worker_process(
    threads_per_proc,
    duration,
    image,
    runtime,
    cpu,
    memory,
    cpu_limit,
    mem_limit,
    idle_timeout,
    xpu,
    storage_mb,
    upstream,
    reverse_port,
    listen_port,
    cmd,
    cmd_timeout,
    tunnel_mode,
    target_rps=0.0,
):
    stats = _new_stats()
    sb_kwargs = _build_sandbox_kwargs(
        image,
        runtime,
        cpu,
        memory,
        cpu_limit,
        mem_limit,
        idle_timeout,
        xpu,
        storage_mb,
        upstream,
        reverse_port,
        listen_port,
    )
    end_time = time.perf_counter() + duration

    if target_rps:
        arrivals = run_open_loop(
            duration=duration,
            target_rps=target_rps,
            max_inflight=threads_per_proc,
            operation=lambda: run_single_request(
                stats, None, sb_kwargs, cmd, cmd_timeout, tunnel_mode
            ),
        )
        stats["arrival"] = vars(arrivals)
    else:
        with ThreadPoolExecutor(max_workers=threads_per_proc) as worker_pool:
            futs = [
                worker_pool.submit(
                    thread_loop,
                    stats,
                    None,
                    end_time,
                    sb_kwargs,
                    cmd,
                    cmd_timeout,
                    tunnel_mode,
                )
                for _ in range(threads_per_proc)
            ]
            for future in futs:
                future.result()
        stats["arrival"]["scheduled"] = stats["total"]
        stats["arrival"]["submitted"] = stats["total"]

    stats.pop("lock", None)
    return stats


def start_local_http_server(port: int):
    """Spawn a python http.server with /health -> OK, /index.html -> banner."""
    temp_dir = tempfile.mkdtemp(prefix="sandbox_pressure_")

    with open(os.path.join(temp_dir, "health"), "w") as f:
        f.write("OK")
    with open(os.path.join(temp_dir, "index.html"), "w") as f:
        f.write(f"<h1>sandbox pressure test</h1><p>{time.ctime()}</p>")

    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "0.0.0.0"],
        cwd=temp_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(40):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            print(f"[OK] local http server bound on 0.0.0.0:{port}")
            return proc, temp_dir
        except OSError:
            time.sleep(0.2)

    proc.kill()
    raise RuntimeError(f"failed to start local http server on port {port}")


def _write_result(output: Path | None, report: dict) -> None:
    if output is None:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


def main(args):
    if (
        args.processes < 1
        or args.threads < 1
        or args.duration < 1
        or args.target_rps < 0
    ):
        raise ValueError("processes, threads and duration must be positive")
    run_id = args.run_id or uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    if args.tunnel and not args.upstream:
        args.upstream = f"127.0.0.1:{args.tunnel_port}"
        args.cmd = TUNNEL_CMD_TEMPLATE
        local_proc, temp_dir = start_local_http_server(args.tunnel_port)
    else:
        local_proc, temp_dir = None, None

    print(
        f"\n🚀 Sandbox pressure test\n"
        f"   processes      : {args.processes}\n"
        f"   threads/proc   : {args.threads}\n"
        f"   total parallel : {args.processes * args.threads}\n"
        f"   target RPS     : {args.target_rps or '<closed loop>'}\n"
        f"   duration       : {args.duration}s\n"
        f"   runtime        : {args.runtime}\n"
        f"   image (rootfs) : {args.image or '<cluster default>'}\n"
        f"   cpu req/limit  : {args.cpu}m / {args.cpu_limit}m\n"
        f"   mem req/limit  : {args.memory}MiB / {args.mem_limit}MiB\n"
        f"   xpu request    : {args.xpu or '<none>'}\n"
        f"   storage quota  : {args.storage_mb or '<default>'} MiB\n"
        f"   tunnel mode    : {bool(args.upstream)}"
        + (
            f" (upstream={args.upstream}, reverse_port={args.reverse_port}, "
            f"listen_port={args.listen_port})"
            if args.upstream
            else ""
        )
        + f"\n   workload       : {args.cmd!r}\n"
    )

    try:
        t_start = time.perf_counter()
        try:
            with ProcessPoolExecutor(max_workers=args.processes) as pool:
                futs = [
                    pool.submit(
                        worker_process,
                        args.threads,
                        args.duration,
                        args.image,
                        args.runtime,
                        args.cpu,
                        args.memory,
                        args.cpu_limit,
                        args.mem_limit,
                        args.idle_timeout,
                        args.xpu,
                        args.storage_mb,
                        args.upstream,
                        args.reverse_port,
                        args.listen_port,
                        args.cmd,
                        args.cmd_timeout,
                        bool(args.upstream),
                        args.target_rps / args.processes if args.target_rps else 0.0,
                    )
                    for _ in range(args.processes)
                ]
                results = [future.result() for future in futs]
        except Exception as error:
            _write_result(
                args.output,
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "started_at": started_at,
                    "status": "driver_error",
                    "error": safe_error(error),
                },
            )
            raise
        t_end = time.perf_counter()

        merged = _new_stats()
        for s in results:
            merged["total"] += s["total"]
            merged["success"] += s["success"]
            merged["failed"] += s["failed"]
            for phase, count in s["errors_by_phase"].items():
                merged["errors_by_phase"][phase] += count
            for name, count in s["arrival"].items():
                merged["arrival"][name] += count
            for metric in (
                "latencies",
                "create_latencies",
                "command_latencies",
                "cleanup_latencies",
                "failure_latencies",
            ):
                merged[metric].merge(s[metric])
            merged["errors"].extend(s["errors"][: max(0, 10 - len(merged["errors"]))])

        total_time = t_end - t_start
        attempted_rps = merged["total"] / total_time if total_time > 0 else 0
        successful_rps = merged["success"] / total_time if total_time > 0 else 0

        report = {
            "schema_version": 1,
            "run_id": run_id,
            "started_at": started_at,
            "status": "passed" if merged["failed"] == 0 else "failed",
            "mode": "open_loop" if args.target_rps else "closed_loop",
            "config": {
                "processes": args.processes,
                "threads_per_process": args.threads,
                "duration_requested_seconds": args.duration,
                "runtime": args.runtime,
                "image": args.image or None,
                "cpu_request_millicores": args.cpu,
                "memory_request_mib": args.memory,
                "tunnel": bool(args.upstream),
                "target_rps": args.target_rps or None,
            },
            "duration_seconds": total_time,
            "attempted": merged["total"],
            "succeeded": merged["success"],
            "failed": merged["failed"],
            "attempted_rps": attempted_rps,
            "successful_rps": successful_rps,
            "arrival": merged["arrival"],
            "errors_by_phase": merged["errors_by_phase"],
            "error_samples": merged["errors"],
            "latency_ms": {
                metric: merged[metric].summary()
                for metric in (
                    "latencies",
                    "create_latencies",
                    "command_latencies",
                    "cleanup_latencies",
                    "failure_latencies",
                )
            },
        }
        _write_result(args.output, report)

        print("\n" + "=" * 60)
        print("📊 SANDBOX PRESSURE TEST RESULT")
        print("=" * 60)
        print(f"⏱️  Duration       : {total_time:.2f}s")
        print(f"🔁 Total Requests : {merged['total']}")
        print(f"✅ Success        : {merged['success']}")
        print(f"❌ Failed         : {merged['failed']}")
        print(f"⚡ Attempted RPS  : {attempted_rps:.2f}")
        print(f"⚡ Successful RPS : {successful_rps:.2f}")
        print(f"📥 Arrival slots  : {merged['arrival']}")

        def _percentiles(label, histogram):
            if not histogram.count:
                return
            summary = histogram.summary()
            print(
                f"📉 {label:<20}: P50≤{summary['p50_upper_ms']:.1f}ms "
                f"P90≤{summary['p90_upper_ms']:.1f}ms "
                f"P99≤{summary['p99_upper_ms']:.1f}ms "
                f"(n={summary['count']}; bucket upper bounds)"
            )

        _percentiles("full lifecycle", merged["latencies"])
        _percentiles("create-only latency", merged["create_latencies"])
        _percentiles("command latency", merged["command_latencies"])
        _percentiles("delete latency", merged["cleanup_latencies"])
        _percentiles("failed lifecycle", merged["failure_latencies"])
        print(f"   Errors by phase: {merged['errors_by_phase']}")

        if merged["errors"]:
            print("\n— sample failures (up to 5) —")
            for err in merged["errors"][:5]:
                print(f"  • {err}")

        if merged["failed"]:
            raise SystemExit(1)
    finally:
        if local_proc is not None:
            local_proc.terminate()
            try:
                local_proc.wait(timeout=5)
            except Exception:
                local_proc.kill()
        if temp_dir is not None:
            import shutil

            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AKernel Sandbox pressure test (multi-process + multi-thread)"
    )
    parser.add_argument("--processes", type=int, default=2)
    parser.add_argument("--threads", type=int, default=4, help="threads per process")
    parser.add_argument("--duration", type=int, default=60, help="seconds")
    parser.add_argument(
        "--target-rps",
        type=float,
        default=0.0,
        help="fixed total arrival rate; 0 uses the existing closed-loop mode",
    )
    parser.add_argument(
        "--run-id", default="", help="identifier for result correlation"
    )
    parser.add_argument(
        "--output", type=Path, help="write a structured JSON result to this path"
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="custom rootfs image (empty = use cluster default image)",
    )
    parser.add_argument(
        "--runtime",
        default=DEFAULT_RUNTIME,
        help="sandbox runtime (default: AKERNEL_PRESSURE_RUNTIME or runsc)",
    )
    parser.add_argument(
        "--cpu", type=int, default=100, help="cpu request, milli-cores (100=0.1c)"
    )
    parser.add_argument(
        "--cpu-limit",
        type=int,
        default=500,
        help="cpu cgroup limit, milli-cores (0=disabled, 500=0.5c)",
    )
    parser.add_argument("--memory", type=int, default=100, help="memory request, MiB")
    parser.add_argument(
        "--mem-limit",
        type=int,
        default=4096,
        help="memory cgroup limit, MiB (0=disabled, 4096=4G)",
    )
    parser.add_argument("--idle-timeout", type=int, default=120, help="seconds")
    parser.add_argument(
        "--xpu",
        default="",
        help="optional type:model:count accelerator request",
    )
    parser.add_argument(
        "--storage-mb",
        type=int,
        default=None,
        help="optional writable root filesystem quota in MiB",
    )
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="enable tunnel mode: auto-start local http.server, all sandboxes "
        "fetch through reverse tunnel to verify connectivity",
    )
    parser.add_argument(
        "--tunnel-port", type=int, default=18080, help="local http.server port"
    )
    parser.add_argument(
        "--upstream",
        default="",
        help="raw upstream addr (overrides --tunnel auto-detected one). "
        "Empty = no tunnel.",
    )
    parser.add_argument("--reverse-port", type=int, default=8765)
    parser.add_argument("--listen-port", type=int, default=8766)
    parser.add_argument(
        "--cmd",
        default=DEFAULT_CMD,
        help=(
            f"workload command (use {TUNNEL_URL_PLACEHOLDER} for "
            "tunnel-url substitution)"
        ),
    )
    parser.add_argument("--cmd-timeout", type=int, default=20)
    args = parser.parse_args()

    main(args)
