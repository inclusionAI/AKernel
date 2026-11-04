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

"""Real API Key creation, expiry, and revocation through the public API."""

import os
import subprocess
import sys
import time
import unittest

from tests.e2e.full._keys import create_key, management_enabled, revoke_key

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
    and management_enabled()
)


def _probe(token: str) -> tuple[bool, bool]:
    environment = os.environ.copy()
    environment["AKERNEL_TOKEN"] = token
    child = subprocess.run(
        [sys.executable, "-c", "from akernel_sdk import resources; resources()"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    output = (child.stdout + child.stderr).lower()
    denied = any(
        marker in output for marker in ("401", "unauthorized", "unauthenticated")
    )
    return child.returncode == 0, denied


@unittest.skipUnless(
    _ENABLED,
    "requires an administrator key and AKERNEL_TEST_MANAGE_KEYS=1",
)
class KeyLifecycleIntegrationTest(unittest.TestCase):
    def test_revoked_tenant_key_stops_authenticating(self):
        key_id, token = create_key()
        revoked = False
        try:
            self.assertEqual(_probe(token), (True, False))
            revoke_key(key_id)
            revoked = True
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if _probe(token) == (False, True):
                    return
                time.sleep(0.5)
            self.fail("revoked key remained usable beyond the cache window")
        finally:
            if not revoked:
                revoke_key(key_id)

    def test_expired_tenant_key_stops_authenticating(self):
        key_id, token = create_key(expires_at=int(time.time()) + 5)
        try:
            self.assertEqual(_probe(token), (True, False))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if _probe(token) == (False, True):
                    return
                time.sleep(0.5)
            self.fail("expired key remained usable")
        finally:
            revoke_key(key_id)


if __name__ == "__main__":
    unittest.main()
