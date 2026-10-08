"""Load generator correctness and boundedness."""

import threading
import unittest

from benchmarks.harness.load import run_open_loop


class OpenLoopTest(unittest.TestCase):
    def test_records_inflight_rejection_and_drains(self):
        release = threading.Event()
        entered = threading.Event()
        completed = 0

        def operation():
            nonlocal completed
            entered.set()
            release.wait(timeout=1)
            completed += 1

        timer = threading.Timer(0.04, release.set)
        timer.start()
        try:
            result = run_open_loop(
                duration=0.025, target_rps=1000, max_inflight=1, operation=operation
            )
        finally:
            release.set()
            timer.join()

        self.assertTrue(entered.is_set())
        self.assertEqual(completed, result.submitted)
        self.assertEqual(completed, 1)
        self.assertGreater(result.rejected_inflight, 0)
        self.assertEqual(
            result.scheduled,
            result.submitted + result.rejected_inflight + result.missed_deadline,
        )

    def test_propagates_worker_failure(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("worker died")

        with self.assertRaisesRegex(RuntimeError, "worker died"):
            run_open_loop(
                duration=0.025, target_rps=100, max_inflight=1, operation=operation
            )
        self.assertGreaterEqual(calls, 1)

    def test_rejects_invalid_limits(self):
        for duration, rate, limit in ((0, 1, 1), (1, 0, 1), (1, 1, 0)):
            with self.subTest(duration=duration, rate=rate, limit=limit):
                with self.assertRaises(ValueError):
                    run_open_loop(
                        duration=duration,
                        target_rps=rate,
                        max_inflight=limit,
                        operation=lambda: None,
                    )

    def test_abort_stops_new_arrivals_after_workload_failure(self):
        stop = threading.Event()
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            stop.set()

        result = run_open_loop(
            duration=0.5,
            target_rps=100,
            max_inflight=1,
            operation=operation,
            abort=stop.is_set,
        )
        self.assertEqual(calls, 1)
        self.assertLess(result.scheduled, 50)


if __name__ == "__main__":
    unittest.main()
