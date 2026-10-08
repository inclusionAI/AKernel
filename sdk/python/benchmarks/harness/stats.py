"""Bounded latency summaries for long-running end-to-end load tests."""

from bisect import bisect_left
from dataclasses import dataclass, field
from math import ceil

_BOUNDS_MS = (
    1,
    2,
    5,
    10,
    20,
    30,
    40,
    50,
    75,
    100,
    125,
    150,
    175,
    200,
    225,
    250,
    300,
    400,
    500,
    750,
    1000,
    2000,
    5000,
    10000,
    12000,
    15000,
    20000,
    30000,
    60000,
    120000,
    300000,
    600000,
)


@dataclass
class LatencyHistogram:
    """Keep all observations in fixed buckets, with exact min/max/mean."""

    counts: list[int] = field(default_factory=lambda: [0] * (len(_BOUNDS_MS) + 1))
    count: int = 0
    total_seconds: float = 0.0
    min_seconds: float | None = None
    max_seconds: float | None = None

    def observe(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("latency cannot be negative")
        self.counts[bisect_left(_BOUNDS_MS, seconds * 1000)] += 1
        self.count += 1
        self.total_seconds += seconds
        self.min_seconds = (
            seconds if self.min_seconds is None else min(self.min_seconds, seconds)
        )
        self.max_seconds = (
            seconds if self.max_seconds is None else max(self.max_seconds, seconds)
        )

    def merge(self, other: "LatencyHistogram") -> None:
        if len(self.counts) != len(other.counts):
            raise ValueError("cannot merge different bucket layouts")
        self.counts = [
            left + right for left, right in zip(self.counts, other.counts, strict=True)
        ]
        self.count += other.count
        self.total_seconds += other.total_seconds
        if other.min_seconds is not None:
            self.min_seconds = (
                other.min_seconds
                if self.min_seconds is None
                else min(self.min_seconds, other.min_seconds)
            )
        if other.max_seconds is not None:
            self.max_seconds = (
                other.max_seconds
                if self.max_seconds is None
                else max(self.max_seconds, other.max_seconds)
            )

    def percentile_upper_ms(self, quantile: float) -> float | None:
        if not 0 < quantile <= 1:
            raise ValueError("quantile must be in (0, 1]")
        if not self.count:
            return None
        target = ceil(quantile * self.count)
        cumulative = 0
        for index, bucket_count in enumerate(self.counts):
            cumulative += bucket_count
            if cumulative >= target:
                if index == len(_BOUNDS_MS):
                    return self.max_seconds * 1000
                return float(_BOUNDS_MS[index])
        raise AssertionError("histogram count does not match its buckets")

    def summary(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "min_ms": None if self.min_seconds is None else self.min_seconds * 1000,
            "mean_ms": (
                None if not self.count else self.total_seconds * 1000 / self.count
            ),
            "max_ms": None if self.max_seconds is None else self.max_seconds * 1000,
            "p50_upper_ms": self.percentile_upper_ms(0.5),
            "p90_upper_ms": self.percentile_upper_ms(0.9),
            "p99_upper_ms": self.percentile_upper_ms(0.99),
        }
