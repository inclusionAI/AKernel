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

"""Cross-node public SDK placement and independent routing contracts."""

import os
import unittest

from akernel_sdk import BackendOperationError, Sandbox, resources

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class NodePlacementIntegrationTest(unittest.TestCase):
    def _two_nodes(self):
        eligible = [
            node.id
            for node in resources()
            if node.allocatable.get("CPU", 0) >= 1000
            and node.allocatable.get("Memory", 0) >= 2048
        ]
        if len(eligible) < 2:
            self.skipTest(f"requires two eligible nodes; found {eligible}")
        return eligible[:2]

    def test_two_pinned_nodes_serve_independent_sandboxes(self):
        first_node, second_node = self._two_nodes()
        with Sandbox(
            runtime=_RUNTIME, node_id=first_node, cpu=1000, memory=2048
        ) as first:
            with Sandbox(
                runtime=_RUNTIME, node_id=second_node, cpu=1000, memory=2048
            ) as second:
                self.assertNotEqual(first.id, second.id)
                first.files.write("/tmp/e2e-node-marker", "first")
                second.files.write("/tmp/e2e-node-marker", "second")
                self.assertEqual(first.files.read("/tmp/e2e-node-marker"), "first")
                self.assertEqual(second.files.read("/tmp/e2e-node-marker"), "second")
                self.assertEqual(first.commands.run("printf first").stdout, "first")
                self.assertEqual(second.commands.run("printf second").stdout, "second")

    def test_unknown_node_is_rejected_without_creating_a_sandbox(self):
        with self.assertRaises(BackendOperationError):
            Sandbox(
                runtime=_RUNTIME,
                node_id="e2e-node-does-not-exist",
                cpu=1000,
                memory=2048,
                schedule_timeout=2,
            )


if __name__ == "__main__":
    unittest.main()
