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

"""Preserve runtime ownership across a controlled worker service restart."""

import os
import subprocess
import time
import unittest
import uuid
from pathlib import Path

from akernel_sdk import BackendOperationError, Sandbox

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and os.environ.get("AKERNEL_TEST_DESTRUCTIVE_FAULTS") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
    and bool(os.environ.get("AKERNEL_TEST_FAULT_NODE_ID"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")
_NODE_ID = os.environ.get("AKERNEL_TEST_FAULT_NODE_ID", "")


@unittest.skipUnless(
    _ENABLED,
    "requires isolated deployment and AKERNEL_TEST_DESTRUCTIVE_FAULTS=1",
)
class ProcessRestartIntegrationTest(unittest.TestCase):
    def _restart(self, hook_variable: str) -> None:
        hook = os.environ.get(hook_variable)
        if not hook:
            self.skipTest(f"set {hook_variable} to an executable restart hook")
        path = Path(hook)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            self.fail(f"{hook_variable} must name an executable absolute file")
        try:
            completed = subprocess.run(
                [str(path), _NODE_ID],
                capture_output=True,
                check=False,
                timeout=90,
            )
        except subprocess.TimeoutExpired as error:
            self.fail(f"{hook_variable} exceeded 90 seconds: {type(error).__name__}")
        if completed.returncode != 0:
            self.fail(f"{hook_variable} failed with exit code {completed.returncode}")

    def _assert_runtime_survives(self, hook_variable: str) -> None:
        if not os.environ.get(hook_variable):
            self.skipTest(f"set {hook_variable} to an executable restart hook")
        marker = f"AKERNEL_RESTART_{uuid.uuid4().hex}"
        path = f"/tmp/{marker}"
        with Sandbox(
            node_id=_NODE_ID, runtime=_RUNTIME, cpu=1000, memory=2048
        ) as sandbox:
            original_id = sandbox.id
            sandbox.files.write(path, marker)
            self._restart(hook_variable)

            deadline = time.monotonic() + 120
            last_error = None
            while time.monotonic() < deadline:
                try:
                    result = sandbox.commands.run("printf RECONNECTED", timeout=10)
                    content = sandbox.files.read(path)
                except (BackendOperationError, TimeoutError) as error:
                    last_error = error
                    time.sleep(1)
                    continue
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(result.stdout, "RECONNECTED")
                self.assertEqual(sandbox.id, original_id)
                self.assertEqual(content, marker)
                return
            self.fail(f"runtime did not reconnect within 120 seconds: {last_error}")

    def test_sandboxd_restart_preserves_running_sandbox(self):
        self._assert_runtime_survives("AKERNEL_TEST_RESTART_SANDBOXD")

    def test_node_manager_fast_restart_preserves_running_sandbox(self):
        self._assert_runtime_survives("AKERNEL_TEST_RESTART_NODE_MANAGER")


if __name__ == "__main__":
    unittest.main()
