"""Resident workload must detect data corruption and always release instances."""

import unittest
from unittest.mock import patch

from benchmarks import mixed_pressure


class _Result:
    exit_code = 0
    stdout = "resident-ok"


class _Sandbox:
    instances = []
    corrupt_reads = False

    def __init__(self, **_kwargs):
        self.commands = self
        self.files = self
        self.killed = False
        self.content = ""
        self.instances.append(self)

    def run(self, *_args, **_kwargs):
        return _Result()

    def write(self, _path, content):
        self.content = content

    def read(self, _path):
        return "wrong payload" if self.corrupt_reads else self.content

    def kill(self):
        self.killed = True


class MixedPressureTest(unittest.TestCase):
    def setUp(self):
        _Sandbox.instances = []
        _Sandbox.corrupt_reads = False

    def test_cycle_checks_command_and_file_integrity(self):
        sandbox = _Sandbox()
        mixed_pressure.run_resident_cycle(sandbox, "run-1", 3)
        _Sandbox.corrupt_reads = True
        with self.assertRaisesRegex(AssertionError, "data mismatch"):
            mixed_pressure.run_resident_cycle(sandbox, "run-1", 4)

    def test_run_cleans_resident_when_file_read_is_corrupt(self):
        _Sandbox.corrupt_reads = True
        with (
            patch.object(mixed_pressure, "Sandbox", _Sandbox),
            patch.object(mixed_pressure, "run_single_request") as churn,
        ):
            result = mixed_pressure.run_mixed(
                run_id="test",
                duration=0.03,
                residents=1,
                resident_interval=0.01,
                churn_rps=50,
                max_inflight=1,
                sandbox_kwargs={},
            )

        self.assertEqual(churn.call_count, result["churn"]["arrival"]["submitted"])
        self.assertEqual(result["status"], "failed")
        self.assertGreater(result["resident"]["failed"], 0)
        self.assertTrue(all(item.killed for item in _Sandbox.instances))


if __name__ == "__main__":
    unittest.main()
