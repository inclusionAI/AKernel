"""Regression tests for pressure results that would otherwise look successful."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from benchmarks import sandbox_pressure


class _CommandResult:
    exit_code = 0


class _Commands:
    def run(self, *_args, **_kwargs):
        return _CommandResult()


class _CleanupFailureSandbox:
    commands = _Commands()

    def __init__(self, **_kwargs):
        pass

    def kill(self):
        raise RuntimeError("delete failed")


class PressureAccountingTest(unittest.TestCase):
    def test_open_loop_worker_reports_arrival_accounting(self):
        def completed(stats, *_args):
            with stats["lock"]:
                stats["total"] += 1
                stats["success"] += 1

        with patch.object(
            sandbox_pressure, "run_single_request", side_effect=completed
        ):
            stats = sandbox_pressure.worker_process(
                1,
                0.03,
                "",
                "runsc",
                100,
                100,
                500,
                4096,
                120,
                "",
                None,
                "",
                8765,
                8766,
                "/bin/true",
                5,
                False,
                target_rps=200,
            )

        arrival = stats["arrival"]
        self.assertEqual(stats["success"], arrival["submitted"])
        self.assertEqual(
            arrival["scheduled"],
            arrival["submitted"]
            + arrival["rejected_inflight"]
            + arrival["missed_deadline"],
        )

    def test_delete_failure_cannot_be_counted_as_success(self):
        stats = sandbox_pressure._new_stats()
        with (
            patch.object(sandbox_pressure, "Sandbox", _CleanupFailureSandbox),
            ThreadPoolExecutor(max_workers=1) as cleanup_pool,
        ):
            cleanup = sandbox_pressure.run_single_request(
                stats, cleanup_pool, {}, "/bin/true", 5, False
            )
            if cleanup is not None:
                cleanup.result()

        self.assertEqual(stats["success"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["errors_by_phase"]["cleanup"], 1)
        self.assertTrue(any("delete failed" in error for error in stats["errors"]))

    def test_worker_thread_crash_is_not_silently_dropped(self):
        with patch.object(
            sandbox_pressure,
            "run_single_request",
            side_effect=RuntimeError("worker crashed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "worker crashed"):
                sandbox_pressure.worker_process(
                    1,
                    0.1,
                    "",
                    "runsc",
                    100,
                    100,
                    500,
                    4096,
                    120,
                    "",
                    None,
                    "",
                    8765,
                    8766,
                    "/bin/true",
                    5,
                    False,
                )


if __name__ == "__main__":
    unittest.main()
