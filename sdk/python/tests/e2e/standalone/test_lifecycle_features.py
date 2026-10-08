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

"""Named ownership, retry convergence, and idle lifecycle contracts."""

import os
import unittest
import uuid

from akernel_sdk import BackendOperationError, Sandbox

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class LifecycleFeaturesIntegrationTest(unittest.TestCase):
    def test_detached_name_remains_reserved_after_client_close(self):
        name = f"e2e-detached-{uuid.uuid4().hex[:12]}"
        first = None
        reopened = None
        try:
            first = Sandbox(
                name=name, detached=True, runtime=_RUNTIME, cpu=1000, memory=2048
            )
            self.assertTrue(first.is_running())
            first_id = first.id
            first.kill()
            reopened = Sandbox(
                name=name, detached=True, runtime=_RUNTIME, cpu=1000, memory=2048
            )
            self.assertEqual(reopened.id, first_id)
            self.assertEqual(
                reopened.commands.run("printf reopened").stdout, "reopened"
            )
        finally:
            if reopened is not None:
                reopened.kill()
            if first is not None:
                first.kill()
                Sandbox.delete(name)

    def test_same_name_with_different_resources_conflicts(self):
        name = f"e2e-conflict-{uuid.uuid4().hex[:12]}"
        first = None
        competing = None
        try:
            first = Sandbox(
                name=name, detached=True, runtime=_RUNTIME, cpu=1000, memory=2048
            )
            with self.assertRaises(BackendOperationError):
                competing = Sandbox(
                    name=name,
                    detached=True,
                    runtime=_RUNTIME,
                    cpu=2000,
                    memory=2048,
                )
            self.assertEqual(first.commands.run("printf owner").stdout, "owner")
        finally:
            if competing is not None:
                competing.kill()
            if first is not None:
                first.kill()
                Sandbox.delete(name)

    def test_reload_without_checkpoint_does_not_create_a_new_runtime(self):
        with Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048) as sandbox:
            sandbox_id = sandbox.id
            self.assertFalse(sandbox.reload())
            self.assertEqual(sandbox.id, sandbox_id)
            self.assertEqual(sandbox.commands.run("printf alive").stdout, "alive")


if __name__ == "__main__":
    unittest.main()
