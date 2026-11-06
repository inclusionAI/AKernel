"""Public resource units used by the standalone memory-budget regression."""

import os
import unittest
from unittest.mock import patch

from akernel_sdk import NodeInfo
from tests.e2e.standalone import test_sdk_contracts as fixture


class MemoryBudgetFixtureTest(unittest.TestCase):
    def test_page_aligned_parent_caps_follow_whole_mib_allocation_and_release(self):
        public_capacity = 6144.0
        cases = [(extra, None) for extra in (0, 4096, 1024 * 1024 - 4096)]
        cases.extend([(4096, 500), (4096, 1500)])
        for extra_bytes, cpu_budget in cases:
            with self.subTest(extra_bytes=extra_bytes, cpu_budget=cpu_budget):
                maximum = 6 * 1024**3 + extra_bytes
                request_cpu = min(1000, cpu_budget) if cpu_budget is not None else 1000

                def node(available, cpu=None, budget=cpu_budget):
                    return NodeInfo(
                        id="single-node",
                        status=0,
                        capacity={
                            "Memory": public_capacity,
                            **({"CPU": budget} if budget is not None else {}),
                        },
                        allocatable={
                            "Memory": available,
                            **({"CPU": cpu} if cpu is not None else {}),
                        },
                        labels={},
                    )

                case = fixture.MemoryBudgetIntegrationTest(
                    "test_capacity_and_allocation_follow_the_parent_memory_budget"
                )
                with (
                    patch.object(fixture, "_MEMORY_BUDGET", str(maximum)),
                    patch.dict(
                        os.environ,
                        {
                            "AKERNEL_TEST_EXPECT_CPU_MILLIS":
                                str(cpu_budget) if cpu_budget else ""
                        },
                    ),
                    patch.object(
                        fixture,
                        "resources",
                        side_effect=[
                            [node(public_capacity, cpu_budget)],
                            [node(
                                public_capacity - 1024,
                                cpu_budget - request_cpu if cpu_budget else None,
                            )],
                            [node(public_capacity, cpu_budget)],
                        ],
                    ) as resources,
                    patch.object(fixture, "Sandbox") as sandbox,
                    patch.object(
                        fixture.time,
                        "sleep",
                        side_effect=AssertionError(
                            "public memory should match immediately"
                        ),
                    ),
                ):
                    command = sandbox.return_value.commands.run.return_value
                    command.stdout = "memory-budget"
                    case.test_capacity_and_allocation_follow_the_parent_memory_budget()

                self.assertEqual(resources.call_count, 3)
                sandbox.assert_called_once_with(
                    cpu=request_cpu,
                    memory=1024,
                    runtime=fixture._RUNTIME,
                    image=fixture._IMAGE,
                )
                sandbox.return_value.kill.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
