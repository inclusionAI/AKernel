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

import sys
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks import cluster_throughput as benchmark


@dataclass
class FakeResult:
    exit_code: int = 0


class FakeFiles:
    def __init__(self):
        self.calls = 0

    def exists(self, path):
        self.calls += 1
        return path == "/"


class FakeCommands:
    def __init__(self):
        self.calls = 0

    def run(self, command, timeout=60):
        self.calls += 1
        return FakeResult(0 if command == "/bin/true" else 1)


class FakeSandbox:
    next_id = 0

    def __init__(self):
        FakeSandbox.next_id += 1
        self.id = f"sandbox-{FakeSandbox.next_id}"
        self.files = FakeFiles()
        self.commands = FakeCommands()
        self.killed = False

    def kill(self):
        self.killed = True


class ClusterThroughputBenchmarkTest(unittest.TestCase):
    def test_latency_summary_uses_nearest_rank_percentiles(self):
        summary = benchmark.latency_summary([0.001, 0.002, 0.003, 0.004])

        self.assertEqual(summary["min"], 1.0)
        self.assertEqual(summary["p50"], 2.0)
        self.assertEqual(summary["p90"], 4.0)
        self.assertEqual(summary["p99"], 4.0)
        self.assertEqual(summary["max"], 4.0)

    def test_create_holds_sandboxes_until_measured_burst_finishes(self):
        created = []

        def factory():
            sandbox = FakeSandbox()
            created.append(sandbox)
            time.sleep(0.001)
            return sandbox

        result = benchmark.benchmark_create(factory, operations=4, concurrency=2)

        self.assertEqual(result.success, 4)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.sandbox_count, 4)
        self.assertGreaterEqual(result.cleanup_seconds, 0)
        self.assertTrue(all(sandbox.killed for sandbox in created))

    def test_create_waits_for_synchronized_start(self):
        with (
            patch.object(benchmark.time, "time", return_value=97.5),
            patch.object(benchmark.time, "sleep") as sleep,
        ):
            result = benchmark.benchmark_create(
                FakeSandbox,
                operations=2,
                concurrency=1,
                start_at_epoch=100,
            )

        sleep.assert_called_once_with(2.5)
        self.assertEqual(result.success, 2)

    def test_create_can_wait_before_cleanup(self):
        with patch.object(benchmark.time, "sleep") as sleep:
            result = benchmark.benchmark_create(
                FakeSandbox,
                operations=2,
                concurrency=1,
                pre_cleanup_delay=2,
            )

        sleep.assert_called_once_with(2)
        self.assertEqual(result.success, 2)

    def test_cli_accepts_repeated_node_ids(self):
        args = benchmark.parse_args(
            [
                "--mode=create",
                "--node-id=192.0.2.10",
                "--node-id=192.0.2.11",
                "--node-label=HOST_IP",
            ]
        )

        self.assertEqual(args.node_id, ["192.0.2.10", "192.0.2.11"])
        self.assertEqual(args.node_label, "HOST_IP")

    def test_invoke_uses_precreated_pool_and_excludes_warmup(self):
        created = []

        def factory():
            sandbox = FakeSandbox()
            created.append(sandbox)
            return sandbox

        result = benchmark.benchmark_pool(
            "invoke",
            factory,
            operations=6,
            concurrency=2,
            sandbox_count=2,
            command_timeout=5,
        )

        self.assertEqual(result.success, 6)
        self.assertEqual(result.failed, 0)
        self.assertEqual(sum(s.files.calls for s in created), 8)
        self.assertTrue(all(sandbox.killed for sandbox in created))

    def test_exec_uses_bin_true(self):
        created = []

        def factory():
            sandbox = FakeSandbox()
            created.append(sandbox)
            return sandbox

        result = benchmark.benchmark_pool(
            "exec",
            factory,
            operations=4,
            concurrency=2,
            sandbox_count=2,
            command_timeout=5,
        )

        self.assertEqual(result.success, 4)
        self.assertEqual(sum(s.commands.calls for s in created), 6)

    def test_pool_rejects_less_sandboxes_than_concurrency(self):
        with self.assertRaisesRegex(ValueError, "sandbox-count"):
            benchmark.benchmark_pool(
                "invoke",
                FakeSandbox,
                operations=2,
                concurrency=2,
                sandbox_count=1,
                command_timeout=5,
            )

    def test_pool_creation_concurrency_is_independent_from_invoke(self):
        created = []

        def factory():
            sandbox = FakeSandbox()
            created.append(sandbox)
            return sandbox

        with patch.object(
            benchmark, "create_pool", wraps=benchmark.create_pool
        ) as create_pool:
            result = benchmark.benchmark_pool(
                "invoke",
                factory,
                operations=8,
                concurrency=4,
                sandbox_count=4,
                command_timeout=5,
                pool_create_concurrency=2,
            )

        create_pool.assert_called_once_with(factory, 4, 2)
        self.assertEqual(result.success, 8)
        self.assertTrue(all(sandbox.killed for sandbox in created))

    def test_pool_waits_for_synchronized_start_after_warmup(self):
        with (
            patch.object(benchmark.time, "time", return_value=97.5),
            patch.object(benchmark.time, "sleep") as sleep,
        ):
            result = benchmark.benchmark_pool(
                "invoke",
                FakeSandbox,
                operations=4,
                concurrency=2,
                sandbox_count=2,
                command_timeout=5,
                start_at_epoch=100,
            )

        sleep.assert_called_once_with(2.5)
        self.assertEqual(result.success, 4)

    def test_pool_can_leave_sandboxes_for_external_cleanup(self):
        created = []

        def factory():
            sandbox = FakeSandbox()
            created.append(sandbox)
            return sandbox

        result = benchmark.benchmark_pool(
            "invoke",
            factory,
            operations=4,
            concurrency=2,
            sandbox_count=2,
            command_timeout=5,
            keep_sandboxes=True,
        )

        self.assertEqual(result.success, 4)
        self.assertTrue(all(not sandbox.killed for sandbox in created))
        benchmark._KEPT_SANDBOXES.clear()

if __name__ == "__main__":
    unittest.main()
