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

"""The public ``ak`` CLI works against the same deployed SDK endpoint."""

import os
import subprocess
import sys
import unittest

from akernel_sdk import Sandbox

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class CliContractIntegrationTest(unittest.TestCase):
    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "akernel_sdk.cli", *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_resources_list_exec_and_delete(self):
        sandbox = Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048)
        try:
            resources_result = self._cli("resources")
            self.assertEqual(resources_result.returncode, 0, resources_result.stderr)

            listed = self._cli("list", "--quiet")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn(sandbox.id, listed.stdout.splitlines())

            executed = self._cli(
                "exec", sandbox.id, "--", "sh", "-c", "printf CLI_E2E_OK"
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertIn("CLI_E2E_OK", executed.stdout)

            deleted = self._cli("delete", sandbox.id)
            self.assertEqual(deleted.returncode, 0, deleted.stderr)
            self.assertFalse(sandbox.is_running())
        finally:
            sandbox.kill()


if __name__ == "__main__":
    unittest.main()
