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

"""An uncheckpointed Sandbox must stay failed after its worker returns."""

import os
import subprocess
import time
import unittest
from pathlib import Path

from akernel_sdk import BackendOperationError, Sandbox, resources

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and os.environ.get("AKERNEL_TEST_DESTRUCTIVE_FAULTS") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


def _hook(variable: str) -> Path:
    value = os.environ.get(variable, "")
    path = Path(value)
    if not value or not path.is_absolute() or not path.is_file():
        raise ValueError(f"{variable} must name an executable absolute file")
    if not os.access(path, os.X_OK):
        raise ValueError(f"{variable} must be executable")
    return path


def _run_hook(path: Path, node_id: str) -> None:
    try:
        completed = subprocess.run(
            [str(path), node_id], capture_output=True, check=False, timeout=90
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(f"fault hook exceeded 90 seconds: {path.name}") from error
    if completed.returncode != 0:
        raise AssertionError(
            f"fault hook {path.name} failed with exit code {completed.returncode}"
        )


@unittest.skipUnless(
    _ENABLED,
    "requires isolated multi-worker deployment and explicit fault opt-in",
)
class NodeLossIntegrationTest(unittest.TestCase):
    def test_uncheckpointed_node_loss_is_terminal_after_worker_recovers(self):
        lost_node = os.environ.get("AKERNEL_TEST_FAULT_NODE_ID", "")
        healthy_node = os.environ.get("AKERNEL_TEST_HEALTHY_NODE_ID", "")
        if not lost_node or not healthy_node:
            self.skipTest("set fault and healthy node IDs for an isolated cluster")
        if lost_node == healthy_node:
            self.fail("fault and healthy node IDs must differ")
        if not (
            os.environ.get("AKERNEL_TEST_STOP_WORKER")
            and os.environ.get("AKERNEL_TEST_RECOVER_WORKER")
        ):
            self.skipTest("set both worker stop and recovery hook paths")
        stop_hook = _hook("AKERNEL_TEST_STOP_WORKER")
        recover_hook = _hook("AKERNEL_TEST_RECOVER_WORKER")

        with (
            Sandbox(
                runtime=_RUNTIME, node_id=healthy_node, cpu=1000, memory=2048
            ) as healthy,
            Sandbox(
                runtime=_RUNTIME,
                node_id=lost_node,
                cpu=1000,
                memory=2048,
                failover=True,
            ) as lost,
        ):
            lost_id = lost.id
            lost.files.write("/tmp/akernel-lost-node-marker", "must-not-revive")
            self.assertEqual(healthy.commands.run("printf healthy").stdout, "healthy")
            _run_hook(stop_hook, lost_node)
            try:
                deadline = time.monotonic() + 180
                last_state = None
                while time.monotonic() < deadline:
                    self.assertEqual(
                        healthy.commands.run("printf healthy").stdout, "healthy"
                    )
                    try:
                        last_state = lost.get_info().state.lower()
                    except BackendOperationError:
                        last_state = None
                    if last_state == "failed":
                        break
                    time.sleep(2)
                self.assertEqual(last_state, "failed", "worker loss was not terminal")
            finally:
                _run_hook(recover_hook, lost_node)

            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    node = next((n for n in resources() if n.id == lost_node), None)
                except BackendOperationError:
                    node = None
                if node is not None and node.allocatable.get("CPU", 0) >= 1000:
                    break
                time.sleep(2)
            else:
                self.fail("worker did not rejoin with allocatable CPU")

            self.assertEqual(lost.id, lost_id)
            self.assertEqual(lost.get_info().state.lower(), "failed")
            self.assertEqual(healthy.commands.run("printf healthy").stdout, "healthy")


if __name__ == "__main__":
    unittest.main()
