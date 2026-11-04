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

"""Unschedulable creates must fail without changing existing sandboxes."""

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
class AdmissionContractIntegrationTest(unittest.TestCase):
    def test_unsupported_runtime_is_rejected(self):
        with self.assertRaises(BackendOperationError):
            Sandbox(
                runtime="e2e-nonexistent-runtime",
                cpu=1000,
                memory=2048,
                schedule_timeout=3,
            )

    def test_impossible_cpu_request_expires_without_assignment(self):
        total_cpu = sum(node.capacity.get("CPU", 0) for node in resources())
        self.assertGreater(total_cpu, 0)
        with self.assertRaises(BackendOperationError):
            Sandbox(
                runtime=_RUNTIME,
                cpu=int(total_cpu) + 1000,
                memory=2048,
                schedule_timeout=3,
            )


if __name__ == "__main__":
    unittest.main()
