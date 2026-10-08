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

"""A bad API key cannot use the public SDK control endpoint."""

import os
import subprocess
import sys
import unittest
import uuid

from akernel_sdk import resources

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class AuthContractIntegrationTest(unittest.TestCase):
    def test_invalid_key_is_rejected_without_affecting_valid_client(self):
        self.assertTrue(resources())
        child_env = os.environ.copy()
        child_env["AKERNEL_TOKEN"] = "e2e-invalid-" + uuid.uuid4().hex
        child = subprocess.run(
            [sys.executable, "-c", "from akernel_sdk import resources; resources()"],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertNotEqual(child.returncode, 0, "invalid key was accepted")
        output = (child.stdout + child.stderr).lower()
        self.assertTrue(
            "401" in output or "unauthorized" in output or "unauthenticated" in output,
            "invalid key failed for a reason other than authentication",
        )
        self.assertTrue(resources())


if __name__ == "__main__":
    unittest.main()
