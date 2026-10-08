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

"""Replay a real create after discarding its first confirmed client result."""

import os
import unittest
import uuid
from unittest.mock import patch

from adx_sandbox._transport import SandboxClient, _CreateOutcomeUnknown

from akernel_sdk import Sandbox

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
    and bool(os.environ.get("AKERNEL_REDIS_HOST"))
    and os.environ.get("AKERNEL_TEST_DESTRUCTIVE_CLAIMS") == "1"
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


@unittest.skipUnless(
    _ENABLED,
    "requires isolated deployment, AKERNEL_REDIS_HOST, and "
    "AKERNEL_TEST_DESTRUCTIVE_CLAIMS=1",
)
class UnknownCreateResultIntegrationTest(unittest.TestCase):
    def test_retry_after_lost_final_result_keeps_request_identity(self):
        name = f"e2e-unknown-{uuid.uuid4().hex[:12]}"
        original = SandboxClient._create_info_attempt
        requests: list[tuple[str, str]] = []
        confirmed_ids: list[str] = []
        discarded = False
        sandbox = None

        def discard_first_result(client, body, *, request_id, request_timeout):
            nonlocal discarded
            requests.append((request_id, str(body.get("name"))))
            result = original(
                client,
                body,
                request_id=request_id,
                request_timeout=request_timeout,
            )
            confirmed_ids.append(
                str(result.get("sandboxId") or result.get("instanceId"))
            )
            if not discarded:
                discarded = True
                raise _CreateOutcomeUnknown(
                    "injected loss after confirmed create",
                    request_id=request_id,
                    code="OUTCOME_UNKNOWN",
                    retry="same_operation",
                    outcome="unknown",
                    instance_id=name,
                )
            return result

        try:
            with patch.object(
                SandboxClient, "_create_info_attempt", discard_first_result
            ):
                sandbox = Sandbox(
                    name=name,
                    detached=True,
                    runtime=_RUNTIME,
                    cpu=1000,
                    memory=2048,
                )
            self.assertTrue(discarded)
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(requests[0][1], name)
            self.assertEqual(confirmed_ids, [sandbox.id, sandbox.id])
            self.assertEqual(sandbox.commands.run("printf replayed").stdout, "replayed")
        finally:
            if sandbox is not None:
                sandbox.kill()
            Sandbox.delete(name)


if __name__ == "__main__":
    unittest.main()
