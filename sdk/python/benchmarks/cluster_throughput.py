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

"""Measure AKernel sandbox create, direct-invoke, and command throughput.

The modes deliberately isolate different paths:

* ``create`` measures only ``Sandbox(...)`` readiness. Sandboxes stay alive
  until the measured burst completes, and deletion is outside the timer.
* ``invoke`` uses a pre-created pool and calls ``files.exists("/")``.
* ``exec`` uses a pre-created pool and calls ``commands.run("/bin/true")``.

The program emits one JSON document. Tokens are read only by the SDK from the
environment and are never included in the result.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import socket
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from akernel_sdk.sandbox import Sandbox


class SandboxLike(Protocol):
    """Minimal sandbox contract required by this benchmark."""

    @property
    def id(self) -> str: ...

    @property
    def files(self) -> Any: ...

    @property
    def commands(self) -> Any: ...

    def kill(self) -> None: ...


@dataclass(frozen=True)
class SandboxConfig:
    """Resource settings shared by every measured sandbox."""

    runtime: str = "runsc"
    cpu: int = 100
    memory: int = 128
    cpu_limit: int = 500
    mem_limit: int = 512
    idle_timeout: int = 300
    schedule_timeout: int = 60

    def kwargs(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Sample:
    """One operation result."""

    latency_seconds: float
    value: Any = None
    error: str | None = None


@dataclass
class BenchmarkResult:
    """Serializable result for one benchmark mode."""

    mode: str
    operations: int
    concurrency: int
    success: int
    failed: int
    wall_seconds: float
    throughput_ops_s: float
    latency_ms: dict[str, float]
    errors: list[str] = field(default_factory=list)
    cleanup_failed: int = 0
    cleanup_seconds: float = 0.0
    sandbox_count: int = 0


T = TypeVar("T")
_KEPT_SANDBOXES: list[SandboxLike] = []


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """Return a nearest-rank percentile from a sorted, non-empty sample."""

    index = max(0, math.ceil(percentile * len(sorted_values)) - 1)
    return sorted_values[index]


def latency_summary(samples_seconds: list[float]) -> dict[str, float]:
    """Summarize successful operation latency in milliseconds."""

    if not samples_seconds:
        return {}
    values = sorted(value * 1000 for value in samples_seconds)
    return {
        "min": values[0],
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": values[-1],
        "mean": sum(values) / len(values),
    }


def _error_text(error: BaseException) -> str:
    """Return a bounded, single-line error suitable for benchmark output."""

    return " ".join(f"{type(error).__name__}: {error}".split())[:500]


def run_parallel(
    operation: Callable[[int], T], operations: int, concurrency: int
) -> tuple[list[Sample], float]:
    """Run a fixed number of operations with bounded closed-loop concurrency."""

    if operations < 1:
        raise ValueError("operations must be at least 1")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    samples: list[Sample] = []

    def measured(index: int) -> Sample:
        started = time.perf_counter()
        try:
            value = operation(index)
            return Sample(time.perf_counter() - started, value=value)
        except Exception as error:  # Each failed request is benchmark data.
            return Sample(
                time.perf_counter() - started,
                error=_error_text(error),
            )

    wall_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(measured, index) for index in range(operations)]
        for future in as_completed(futures):
            samples.append(future.result())
    return samples, time.perf_counter() - wall_started


def _result(
    mode: str,
    operations: int,
    concurrency: int,
    samples: list[Sample],
    wall_seconds: float,
    *,
    cleanup_failed: int = 0,
    cleanup_seconds: float = 0.0,
    sandbox_count: int = 0,
) -> BenchmarkResult:
    successful = [sample for sample in samples if sample.error is None]
    errors = [sample.error for sample in samples if sample.error is not None]
    return BenchmarkResult(
        mode=mode,
        operations=operations,
        concurrency=concurrency,
        success=len(successful),
        failed=len(errors),
        wall_seconds=wall_seconds,
        throughput_ops_s=len(successful) / wall_seconds if wall_seconds else 0.0,
        latency_ms=latency_summary(
            [sample.latency_seconds for sample in successful]
        ),
        errors=errors[:5],
        cleanup_failed=cleanup_failed,
        cleanup_seconds=cleanup_seconds,
        sandbox_count=sandbox_count,
    )


def cleanup_sandboxes(
    sandboxes: list[SandboxLike], concurrency: int
) -> int:
    """Best-effort parallel sandbox cleanup; return the failure count."""

    if not sandboxes:
        return 0

    def kill(sandbox: SandboxLike) -> None:
        sandbox.kill()

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = [executor.submit(kill, sandbox) for sandbox in sandboxes]
        return sum(1 for future in futures if future.exception() is not None)


def benchmark_create(
    factory: Callable[[], SandboxLike],
    operations: int,
    concurrency: int,
    start_at_epoch: float = 0,
    pre_cleanup_delay: float = 0,
) -> BenchmarkResult:
    """Measure a create burst while holding successful sandboxes alive."""

    wait_for_synchronized_start(start_at_epoch)
    samples, wall_seconds = run_parallel(
        lambda _index: factory(), operations, concurrency
    )
    sandboxes = [
        sample.value for sample in samples if sample.error is None
    ]
    if pre_cleanup_delay > 0:
        time.sleep(pre_cleanup_delay)
    cleanup_started = time.perf_counter()
    cleanup_failed = cleanup_sandboxes(sandboxes, concurrency)
    cleanup_seconds = time.perf_counter() - cleanup_started
    return _result(
        "create",
        operations,
        concurrency,
        samples,
        wall_seconds,
        cleanup_failed=cleanup_failed,
        cleanup_seconds=cleanup_seconds,
        sandbox_count=len(sandboxes),
    )


def wait_for_synchronized_start(start_at_epoch: float) -> None:
    """Wait for a shared epoch, rejecting workers that missed it badly."""

    if start_at_epoch <= 0:
        return
    start_delay = start_at_epoch - time.time()
    if start_delay < -1:
        raise RuntimeError(
            f"missed synchronized start by {-start_delay:.3f} seconds"
        )
    if start_delay > 0:
        time.sleep(start_delay)


def create_pool(
    factory: Callable[[], SandboxLike], sandbox_count: int, concurrency: int
) -> list[SandboxLike]:
    """Create the unmeasured sandbox pool used by invoke and exec modes."""

    samples, _ = run_parallel(lambda _index: factory(), sandbox_count, concurrency)
    sandboxes = [
        sample.value for sample in samples if sample.error is None
    ]
    errors = [sample.error for sample in samples if sample.error is not None]
    if errors:
        cleanup_sandboxes(sandboxes, concurrency)
        raise RuntimeError(f"failed to create sandbox pool: {errors[0]}")
    return sandboxes


def benchmark_pool(
    mode: str,
    factory: Callable[[], SandboxLike],
    operations: int,
    concurrency: int,
    sandbox_count: int,
    command_timeout: int,
    pool_create_concurrency: int = 0,
    start_at_epoch: float = 0,
    keep_sandboxes: bool = False,
) -> BenchmarkResult:
    """Measure direct invoke or command execution against a pre-created pool."""

    if mode not in {"invoke", "exec"}:
        raise ValueError(f"unsupported pool mode: {mode}")
    if sandbox_count < concurrency:
        raise ValueError("sandbox-count must be greater than or equal to concurrency")
    if pool_create_concurrency < 0:
        raise ValueError("pool-create-concurrency cannot be negative")

    create_concurrency = pool_create_concurrency or concurrency
    sandboxes = create_pool(factory, sandbox_count, create_concurrency)
    locks = [threading.Lock() for _ in sandboxes]

    def invoke(index: int) -> None:
        pool_index = index % len(sandboxes)
        sandbox = sandboxes[pool_index]
        with locks[pool_index]:
            if mode == "invoke":
                if not sandbox.files.exists("/"):
                    raise RuntimeError("sandbox root does not exist")
                return
            result = sandbox.commands.run("/bin/true", timeout=command_timeout)
            if result.exit_code != 0:
                raise RuntimeError(f"/bin/true exited with {result.exit_code}")

    try:
        # Exclude first-use connection setup from the measured interval.
        for index, sandbox in enumerate(sandboxes):
            if mode == "invoke":
                if not sandbox.files.exists("/"):
                    raise RuntimeError(f"sandbox {index} root does not exist")
            else:
                result = sandbox.commands.run("/bin/true", timeout=command_timeout)
                if result.exit_code != 0:
                    raise RuntimeError(
                        f"sandbox {index} warm-up exited with {result.exit_code}"
                    )
        wait_for_synchronized_start(start_at_epoch)
        samples, wall_seconds = run_parallel(invoke, operations, concurrency)
    finally:
        if keep_sandboxes:
            # Retain strong references until main prints the result and exits
            # without running SDK destructors. Cleanup is coordinated outside
            # this process so concurrent deletion cannot hide measurements.
            _KEPT_SANDBOXES.extend(sandboxes)
            cleanup_failed = 0
            cleanup_seconds = 0.0
        else:
            cleanup_started = time.perf_counter()
            cleanup_failed = cleanup_sandboxes(sandboxes, concurrency)
            cleanup_seconds = time.perf_counter() - cleanup_started

    return _result(
        mode,
        operations,
        concurrency,
        samples,
        wall_seconds,
        cleanup_failed=cleanup_failed,
        cleanup_seconds=cleanup_seconds,
        sandbox_count=sandbox_count,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("create", "invoke", "exec", "all"),
        default="all",
    )
    parser.add_argument("--operations", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--sandbox-count", type=int, default=4)
    parser.add_argument(
        "--pool-create-concurrency",
        type=int,
        default=0,
        help="pool setup concurrency; 0 uses --concurrency",
    )
    parser.add_argument(
        "--start-at-epoch",
        type=float,
        default=0,
        help="wait after pool warm-up until this Unix epoch timestamp",
    )
    parser.add_argument(
        "--keep-sandboxes",
        action="store_true",
        help="leave the pool running for externally coordinated cleanup",
    )
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument("--cpu", type=int, default=100)
    parser.add_argument("--memory", type=int, default=128)
    parser.add_argument("--cpu-limit", type=int, default=500)
    parser.add_argument("--mem-limit", type=int, default=512)
    parser.add_argument("--idle-timeout", type=int, default=300)
    parser.add_argument("--schedule-timeout", type=int, default=60)
    parser.add_argument(
        "--node-id",
        action="append",
        default=[],
        help="pin creates to this node; repeat to distribute round-robin",
    )
    parser.add_argument(
        "--node-label",
        default="NODE_ID",
        help="scheduler label used for --node-id (external collectors use HOST_IP)",
    )
    parser.add_argument("--command-timeout", type=int, default=20)
    parser.add_argument(
        "--pre-cleanup-delay",
        type=float,
        default=0,
        help="wait after measured create completion before deleting sandboxes",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not os.environ.get("AKERNEL_SERVER_ADDRESS"):
        raise SystemExit("AKERNEL_SERVER_ADDRESS is required")
    if not os.environ.get("AKERNEL_TOKEN"):
        raise SystemExit("AKERNEL_TOKEN is required")

    config = SandboxConfig(
        runtime=args.runtime,
        cpu=args.cpu,
        memory=args.memory,
        cpu_limit=args.cpu_limit,
        mem_limit=args.mem_limit,
        idle_timeout=args.idle_timeout,
        schedule_timeout=args.schedule_timeout,
    )
    node_ids = tuple(dict.fromkeys(node_id.strip() for node_id in args.node_id))
    if any(not node_id for node_id in node_ids):
        raise SystemExit("--node-id must be non-empty")
    node_label = args.node_label.strip()
    if not node_label:
        raise SystemExit("--node-label must be non-empty")
    if node_ids and node_label != "NODE_ID":
        if os.environ.get("AKERNEL_BACKEND") != "openyuanrong-sandbox":
            raise SystemExit(
                "custom --node-label requires AKERNEL_BACKEND=openyuanrong-sandbox"
            )
        import yr_sandbox.sandbox_api as sandbox_api

        sandbox_api._NODE_ID_LABEL = node_label
    factory_lock = threading.Lock()
    next_node = 0

    def factory() -> SandboxLike:
        nonlocal next_node
        node_id = None
        if node_ids:
            with factory_lock:
                node_id = node_ids[next_node % len(node_ids)]
                next_node += 1
        return Sandbox(**config.kwargs(), node_id=node_id)

    modes = ("create", "invoke", "exec") if args.mode == "all" else (args.mode,)
    results: list[BenchmarkResult] = []

    for mode in modes:
        if mode == "create":
            result = benchmark_create(
                factory,
                args.operations,
                args.concurrency,
                args.start_at_epoch,
                args.pre_cleanup_delay,
            )
        else:
            result = benchmark_pool(
                mode,
                factory,
                args.operations,
                args.concurrency,
                args.sandbox_count,
                args.command_timeout,
                args.pool_create_concurrency,
                args.start_at_epoch,
                args.keep_sandboxes,
            )
        results.append(result)

    payload = {
        "schema_version": 1,
        "run_id": f"{int(time.time())}-{socket.gethostname()}",
        "client": {
            "hostname": socket.gethostname(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "sandbox": asdict(config),
        "runner": {
            "pool_create_concurrency": args.pool_create_concurrency,
            "start_at_epoch": args.start_at_epoch,
            "keep_sandboxes": args.keep_sandboxes,
            "node_ids": list(node_ids),
            "node_label": node_label,
            "pre_cleanup_delay": args.pre_cleanup_delay,
        },
        "results": [asdict(result) for result in results],
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")

    failed = any(
        result.failed or result.cleanup_failed for result in results
    )
    exit_code = 1 if failed else 0
    if args.keep_sandboxes:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
