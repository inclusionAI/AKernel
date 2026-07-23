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
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, wait

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from akernel_sdk import HttpReverseTunnel, Sandbox

# Empty => don't configure an image; the cluster default image is used.
DEFAULT_IMAGE = os.environ.get("AKERNEL_PRESSURE_IMAGE", "")
DEFAULT_RUNTIME = os.environ.get("AKERNEL_PRESSURE_RUNTIME", "runsc")

# Keep the default workload independent of optional image tools and external
# network availability. Callers can use --cmd for application-level pressure.
DEFAULT_CMD = "/bin/true"

# Token substituted with ``sandbox.reverse_tunnel.url`` at runtime.
TUNNEL_URL_PLACEHOLDER = "{TUNNEL_URL}"

TUNNEL_CMD_TEMPLATE = (
    'python3 -c "import urllib.request,sys;'
    "r=urllib.request.urlopen('" + TUNNEL_URL_PLACEHOLDER + "/health',timeout=10);"
    "body=r.read().decode();"
    "print(r.status,body);"
    "sys.exit(0 if r.status==200 and 'OK' in body else 1)\""
)


def _safe_kill(sb):
    try:
        sb.kill()
    except Exception:
        pass


def _build_sandbox_kwargs(
    image,
    runtime,
    cpu,
    memory,
    cpu_limit,
    mem_limit,
    idle_timeout,
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
    if image:
        kwargs["image"] = image
    if upstream:
        kwargs["reverse_tunnel"] = HttpReverseTunnel(
            target=upstream,
            reverse_port=reverse_port,
            listen_port=listen_port,
        )
    return kwargs


def run_single_request(stats, term_pool, sb_kwargs, cmd, cmd_timeout, tunnel_mode):
    t0 = time.time()
    sb = None
    try:
        sb = Sandbox(**sb_kwargs)
        t_created = time.time()

        if tunnel_mode:
            tunnel_url = sb.reverse_tunnel.url
            real_cmd = cmd.replace(TUNNEL_URL_PLACEHOLDER, tunnel_url)
        else:
            real_cmd = cmd

        result = sb.commands.run(real_cmd, timeout=cmd_timeout)
        if result.exit_code != 0:
            raise RuntimeError(
                f"exit_code={result.exit_code} "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )

        elapsed = time.time() - t0
        with stats["lock"]:
            stats["success"] += 1
            stats["latencies"].append(elapsed)
            stats["create_latencies"].append(t_created - t0)

        term_pool.submit(_safe_kill, sb)
        sb = None
    except Exception as e:
        with stats["lock"]:
            stats["failed"] += 1
            stats["errors"].append(repr(e)[:300])
        if sb is not None:
            term_pool.submit(_safe_kill, sb)
    finally:
        with stats["lock"]:
            stats["total"] += 1


def thread_loop(stats, term_pool, end_time, sb_kwargs, cmd, cmd_timeout, tunnel_mode):
    while time.time() < end_time:
        run_single_request(stats, term_pool, sb_kwargs, cmd, cmd_timeout, tunnel_mode)


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
    upstream,
    reverse_port,
    listen_port,
    cmd,
    cmd_timeout,
    tunnel_mode,
):
    stats = {
        "total": 0,
        "success": 0,
        "failed": 0,
        "latencies": [],
        "create_latencies": [],
        "errors": [],
        "lock": threading.Lock(),
    }
    sb_kwargs = _build_sandbox_kwargs(
        image,
        runtime,
        cpu,
        memory,
        cpu_limit,
        mem_limit,
        idle_timeout,
        upstream,
        reverse_port,
        listen_port,
    )
    term_pool = ThreadPoolExecutor(max_workers=max(4, threads_per_proc))
    end_time = time.time() + duration

    with ThreadPoolExecutor(max_workers=threads_per_proc) as worker_pool:
        futs = [
            worker_pool.submit(
                thread_loop,
                stats,
                term_pool,
                end_time,
                sb_kwargs,
                cmd,
                cmd_timeout,
                tunnel_mode,
            )
            for _ in range(threads_per_proc)
        ]
        wait(futs)

    term_pool.shutdown(wait=True)
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


def main(args):
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
        f"   duration       : {args.duration}s\n"
        f"   runtime        : {args.runtime}\n"
        f"   image (rootfs) : {args.image or '<cluster default>'}\n"
        f"   cpu req/limit  : {args.cpu}m / {args.cpu_limit}m\n"
        f"   mem req/limit  : {args.memory}MiB / {args.mem_limit}MiB\n"
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
        pool = ProcessPoolExecutor(max_workers=args.processes)
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
                args.upstream,
                args.reverse_port,
                args.listen_port,
                args.cmd,
                args.cmd_timeout,
                bool(args.upstream),
            )
            for _ in range(args.processes)
        ]

        t_start = time.time()
        done = wait(futs)
        t_end = time.time()

        merged = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "latencies": [],
            "create_latencies": [],
            "errors": [],
        }
        for fut in done.done:
            s = fut.result()
            merged["total"] += s["total"]
            merged["success"] += s["success"]
            merged["failed"] += s["failed"]
            merged["latencies"].extend(s["latencies"])
            merged["create_latencies"].extend(s["create_latencies"])
            merged["errors"].extend(s["errors"])

        total_time = t_end - t_start
        rps = merged["total"] / total_time if total_time > 0 else 0

        print("\n" + "=" * 60)
        print("📊 SANDBOX PRESSURE TEST RESULT")
        print("=" * 60)
        print(f"⏱️  Duration       : {total_time:.2f}s")
        print(f"🔁 Total Requests : {merged['total']}")
        print(f"✅ Success        : {merged['success']}")
        print(f"❌ Failed         : {merged['failed']}")
        print(f"⚡ RPS            : {rps:.2f}")

        def _percentiles(label, samples_s):
            if not samples_s:
                return
            ms = sorted(x * 1000 for x in samples_s)
            n = len(ms)
            p50 = ms[n // 2]
            p90 = ms[min(int(0.9 * n), n - 1)]
            p99 = ms[min(int(0.99 * n), n - 1)]
            print(
                f"📉 {label:<20}: P50={p50:.1f}ms P90={p90:.1f}ms "
                f"P99={p99:.1f}ms (n={n})"
            )

        _percentiles("create+run latency", merged["latencies"])
        _percentiles("create-only latency", merged["create_latencies"])

        if merged["errors"]:
            print("\n— sample failures (up to 5) —")
            for err in merged["errors"][:5]:
                print(f"  • {err}")

        pool.shutdown(wait=True)
        if merged["failed"] and merged["success"] == 0:
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
        "--image",
        default=DEFAULT_IMAGE,
        help="custom rootfs image (empty = use cluster default image)",
    )
    parser.add_argument(
        "--runtime",
        choices=("runsc", "kata"),
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
