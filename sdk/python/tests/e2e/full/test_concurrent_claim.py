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

"""Concurrent same-name creates must converge to one logical ownership."""

import concurrent.futures
import os
import threading
import unittest
import uuid

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
class ConcurrentClaimIntegrationTest(unittest.TestCase):
    def test_same_spec_concurrent_creates_share_one_sandbox(self):
        name = f"e2e-claim-{uuid.uuid4().hex[:12]}"
        gate = threading.Barrier(3)
        created: list[Sandbox] = []
        errors: list[Exception] = []

        def create() -> Sandbox:
            gate.wait(timeout=10)
            return Sandbox(
                name=name,
                detached=True,
                runtime=_RUNTIME,
                cpu=1000,
                memory=2048,
            )

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(create) for _ in range(2)]
                gate.wait(timeout=10)
                for future in futures:
                    try:
                        created.append(future.result(timeout=90))
                    except Exception as error:
                        errors.append(error)

            self.assertFalse(errors, f"concurrent creates failed: {errors!r}")
            self.assertEqual(created[0].id, created[1].id)
            self.assertEqual(created[0].commands.run("printf first").stdout, "first")
            self.assertEqual(created[1].commands.run("printf second").stdout, "second")
        finally:
            cleanup_errors = []
            for sandbox in created:
                try:
                    sandbox.kill()
                except Exception as error:
                    cleanup_errors.append(error)
            try:
                Sandbox.delete(name)
            except Exception as error:
                cleanup_errors.append(error)
            self.assertFalse(cleanup_errors, f"cleanup failed: {cleanup_errors!r}")


if __name__ == "__main__":
    unittest.main()
