"""Bounded fixed-arrival load generation for SDK benchmarks."""

import math
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import BoundedSemaphore


@dataclass(frozen=True)
class ArrivalResult:
    scheduled: int
    submitted: int
    rejected_inflight: int
    missed_deadline: int


def run_open_loop(
    *,
    duration: float,
    target_rps: float,
    max_inflight: int,
    operation: Callable[[], None],
    abort: Callable[[], bool] | None = None,
) -> ArrivalResult:
    """Schedule fixed arrivals, bound work, and drain all submitted operations.

    A slot missed by the load generator is counted separately from a slot
    rejected because the allowed number of SDK calls is already in flight.
    Neither kind of lost arrival is silently included in successful throughput.
    """
    if duration <= 0 or target_rps <= 0 or max_inflight < 1:
        raise ValueError("duration, target_rps and max_inflight must be positive")

    interval = 1.0 / target_rps
    start = time.perf_counter()
    end = start + duration
    next_due = start
    inflight = BoundedSemaphore(max_inflight)
    pending: set[Future[None]] = set()
    scheduled = submitted = rejected = missed = 0

    def execute() -> None:
        try:
            operation()
        finally:
            inflight.release()

    with ThreadPoolExecutor(max_workers=max_inflight) as pool:
        while next_due < end:
            if abort is not None and abort():
                break
            now = time.perf_counter()
            if now < next_due:
                time.sleep(next_due - now)
                now = time.perf_counter()
            if abort is not None and abort():
                break

            # If the generator itself fell behind, do not emit a burst to
            # artificially catch up. Record those missing arrival slots.
            overdue = max(0, math.floor((now - next_due) / interval))
            remaining = math.ceil((end - next_due) / interval)
            skipped = min(overdue, remaining - 1)
            missed += skipped
            scheduled += skipped
            next_due += skipped * interval

            scheduled += 1
            if inflight.acquire(blocking=False):
                for future in tuple(pending):
                    if future.done():
                        future.result()
                        pending.remove(future)
                pending.add(pool.submit(execute))
                submitted += 1
            else:
                rejected += 1
            next_due += interval

        # The executor drains its workers here. Future.result also makes
        # unexpected worker exceptions observable to the caller.
        for future in pending:
            future.result()

    return ArrivalResult(scheduled, submitted, rejected, missed)
