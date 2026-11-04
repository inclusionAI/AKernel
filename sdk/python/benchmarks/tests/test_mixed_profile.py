"""The full mixed profile must account for every planned transaction class."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from benchmarks import mixed_profile
from benchmarks.harness.load import ArrivalResult


class MixedProfileTest(unittest.TestCase):
    def test_complete_load_is_marked_met(self):
        def fake_load(*, operation, **_kwargs):
            operation()
            return ArrivalResult(1, 1, 0, 0)

        with patch.object(mixed_profile, "run_open_loop", side_effect=fake_load):
            result = mixed_profile.run_profile(
                profile="interactive", duration=1, target_rps=7,
                operations={name: lambda: None for name in mixed_profile.CLASS_LIMITS},
            )
        self.assertTrue(result["target_load_met"])
        self.assertEqual(result["status"], "passed")

    def test_checkpoint_budget_is_configurable_and_lost_arrivals_are_explicit(self):
        budgets = []

        def fake_load(*, max_inflight, **_kwargs):
            budgets.append(max_inflight)
            return ArrivalResult(2, 1, 1, 0)

        with patch.object(mixed_profile, "run_open_loop", side_effect=fake_load):
            result = mixed_profile.run_profile(
                profile="interactive", duration=1, target_rps=7,
                operations={name: lambda: None for name in mixed_profile.CLASS_LIMITS},
                checkpoint_concurrency=4,
            )
        self.assertIn(4, budgets)
        self.assertEqual(result["classes"]["checkpoint"]["max_inflight"], 4)
        self.assertFalse(result["target_load_met"])

    def test_invalid_checkpoint_concurrency_is_rejected_before_running_load(self):
        with self.assertRaisesRegex(ValueError, "checkpoint_concurrency"):
            mixed_profile.run_profile(
                profile="interactive", duration=1, target_rps=7,
                operations={name: lambda: None for name in mixed_profile.CLASS_LIMITS},
                checkpoint_concurrency=0,
            )

    def test_concurrent_checkpoints_use_independent_sandboxes_and_return_leases(self):
        fixtures = mixed_profile._MixedFixtures(
            run_id="pool", runtime="runsc", image="fixture", port=18081,
            socket_path="/run/akernel/execd.sock", profile="interactive",
            checkpoint_concurrency=2,
        )
        barrier = threading.Barrier(2)
        seen = []
        lock = threading.Lock()

        class FakeSandbox:
            def __init__(self, name):
                self.id = name
                self.content = ""
                self.files = SimpleNamespace(
                    write=self.write, read=lambda _path: self.content
                )
                self.commands = SimpleNamespace(run=self.command)

            def write(self, _path, content):
                self.content = content

            def command(self, *_args, **_kwargs):
                with lock:
                    seen.append(self.id)
                barrier.wait(timeout=2)
                self.saved = self.content
                return SimpleNamespace(exit_code=0, stdout="")

            def reload(self):
                self.content = self.saved
                return True

        for name in ("first", "second"):
            fixtures._checkpoint_pool.put(FakeSandbox(name))
        with patch.object(mixed_profile, "verify_checkpoint_response"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(fixtures.checkpoint) for _ in range(2)]
                for future in futures:
                    future.result(timeout=3)
        self.assertEqual(set(seen), {"first", "second"})
        self.assertEqual(fixtures._checkpoint_pool.qsize(), 2)

    def test_failed_checkpoint_returns_its_sandbox_lease(self):
        fixtures = mixed_profile._MixedFixtures(
            run_id="pool", runtime="runsc", image="fixture", port=18081,
            socket_path="/run/akernel/execd.sock", profile="interactive",
            checkpoint_concurrency=1,
        )
        def fail_write(*_args):
            raise ValueError("write failed")

        sandbox = SimpleNamespace(id="first", files=SimpleNamespace(write=fail_write))
        fixtures._checkpoint_pool.put(sandbox)
        with self.assertRaisesRegex(ValueError, "write failed"):
            fixtures.checkpoint()
        self.assertIs(fixtures._checkpoint_pool.get_nowait(), sandbox)

    def test_all_profiles_include_seven_classes_and_total_one_hundred_percent(self):
        expected = {
            "command",
            "http",
            "file",
            "lifecycle",
            "pty",
            "tunnel",
            "checkpoint",
        }
        for name, weights in mixed_profile.PROFILES.items():
            with self.subTest(name=name):
                self.assertEqual(set(weights), expected)
                self.assertEqual(sum(weights.values()), 100)
                self.assertTrue(all(weight > 0 for weight in weights.values()))

    def test_each_class_runs_and_reports_its_own_result(self):
        counts = dict.fromkeys(mixed_profile.PROFILES["interactive"], 0)

        def operation(name):
            def execute():
                counts[name] += 1
                if name == "http":
                    raise ValueError("HTTP content mismatch")

            return execute

        operations = {name: operation(name) for name in counts}

        def fake_load(*, operation, **_kwargs):
            operation()
            return ArrivalResult(1, 1, 0, 0)

        with patch.object(mixed_profile, "run_open_loop", side_effect=fake_load):
            result = mixed_profile.run_profile(
                profile="interactive",
                duration=1,
                target_rps=7,
                operations=operations,
            )

        self.assertEqual(counts, dict.fromkeys(counts, 1))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["classes"]["http"]["failed"], 1)
        self.assertEqual(result["classes"]["http"]["succeeded"], 0)
        self.assertEqual(result["classes"]["command"]["succeeded"], 1)
        self.assertEqual(result["classes"]["checkpoint"]["succeeded"], 1)

    def test_missing_operation_and_invalid_limits_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing operations"):
            mixed_profile.run_profile(
                profile="interactive",
                duration=1,
                target_rps=7,
                operations={"command": lambda: None},
            )
        with self.assertRaisesRegex(ValueError, "duration"):
            mixed_profile.run_profile(
                profile="interactive",
                duration=0,
                target_rps=7,
                operations={
                    name: lambda: None for name in mixed_profile.PROFILES["interactive"]
                },
            )

    def test_fixture_exposes_callable_for_every_class(self):
        fixtures = mixed_profile._MixedFixtures(
            run_id="case",
            runtime="runsc",
            image="fixture",
            port=18081,
            socket_path="/run/akernel/execd.sock",
            profile="interactive",
        )
        operations = fixtures.operations()
        self.assertEqual(set(operations), set(mixed_profile.CLASS_LIMITS))
        self.assertTrue(all(callable(operation) for operation in operations.values()))

    def test_io_heavy_profile_increases_file_and_checkpoint_payloads(self):
        self.assertGreater(
            mixed_profile.FILE_BYTES["io-heavy"],
            mixed_profile.FILE_BYTES["interactive"],
        )
        self.assertGreater(
            mixed_profile.CHECKPOINT_BYTES["io-heavy"],
            mixed_profile.CHECKPOINT_BYTES["interactive"],
        )


if __name__ == "__main__":
    unittest.main()
