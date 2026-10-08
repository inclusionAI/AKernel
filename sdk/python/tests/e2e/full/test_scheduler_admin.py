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

"""Exercise public scheduler administration on an isolated deployment."""

import json
import os
import ssl
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from akernel_sdk import Sandbox
from akernel_sdk._addresses import api_endpoint_from_env
from tests.e2e.full._keys import create_key, management_enabled, revoke_key

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)


def _request(method: str, path: str, token: str) -> dict:
    endpoint = api_endpoint_from_env()
    request = urllib.request.Request(
        endpoint.base_url() + path,
        method=method,
        headers={"Authorization": "Bearer " + token},
    )
    context = None
    if endpoint.use_tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, timeout=20, context=context) as response:
        return json.load(response)


def _resource_fragments(token: str) -> dict:
    payload = _request("GET", "/global-scheduler/resources", token)
    return payload["resource"]["fragment"]


@unittest.skipUnless(_ENABLED, "requires deployed SDK endpoints and credentials")
class SchedulerAdminIntegrationTest(unittest.TestCase):
    def test_tenant_cannot_read_queue_or_change_node_scheduling(self):
        if not management_enabled():
            self.skipTest("set AKERNEL_TEST_MANAGE_KEYS=1 on an isolated deployment")
        administrator = os.environ["AKERNEL_TOKEN"]
        fragments = _resource_fragments(administrator)
        self.assertTrue(fragments, "scheduler has no registered node")
        node_id = next(iter(fragments))
        before = fragments[node_id]["status"]
        queue = _request("GET", "/global-scheduler/scheduling_queue", administrator)
        self.assertIn("count", queue)
        self.assertIn("instanceInfos", queue)
        self.assertEqual(queue["count"], len(queue["instanceInfos"]))

        key_id, tenant_token = create_key()
        try:
            node_query = urllib.parse.urlencode({"node_id": node_id})
            node_path = f"/global-scheduler/node/localschedulingstatus?{node_query}"
            for method, path in (
                ("GET", "/global-scheduler/scheduling_queue"),
                ("POST", node_path),
                ("DELETE", node_path),
            ):
                with self.subTest(method=method, path=path):
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        _request(method, path, tenant_token)
                    try:
                        self.assertEqual(denied.exception.code, 403)
                    finally:
                        denied.exception.close()
            after = _resource_fragments(administrator)[node_id]["status"]
            self.assertEqual(after, before)
        finally:
            revoke_key(key_id)

    def test_administrator_pause_and_resume_preserves_running_sandbox(self):
        if os.environ.get("AKERNEL_TEST_SCHEDULER_MAINTENANCE") != "1":
            self.skipTest(
                "set AKERNEL_TEST_SCHEDULER_MAINTENANCE=1 on an isolated deployment"
            )
        node_id = os.environ.get("AKERNEL_TEST_SCHEDULER_NODE_ID", "")
        if not node_id:
            self.skipTest(
                "set AKERNEL_TEST_SCHEDULER_NODE_ID to an isolated test worker"
            )
        administrator = os.environ["AKERNEL_TOKEN"]
        fragments = _resource_fragments(administrator)
        self.assertIn(node_id, fragments)
        if fragments[node_id]["status"] != 0:
            self.skipTest("test worker is already closed to new scheduling")
        node_query = urllib.parse.urlencode({"node_id": node_id})
        path = f"/global-scheduler/node/localschedulingstatus?{node_query}"

        with Sandbox(
            runtime=os.environ.get("AKERNEL_TEST_RUNTIME", "runsc"),
            node_id=node_id,
            cpu=1000,
            memory=2048,
        ) as sandbox:
            try:
                paused = _request("POST", path, administrator)
                self.assertEqual(paused["status"], "evicting")
                self._await_node_status(administrator, node_id, 1)
                result = sandbox.commands.run("printf still-running")
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(result.stdout, "still-running")
            finally:
                resumed = _request("DELETE", path, administrator)
                self.assertEqual(resumed["status"], "normal")
                self._await_node_status(administrator, node_id, 0)

    def _await_node_status(self, token: str, node_id: str, expected: int) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _resource_fragments(token)[node_id]["status"] == expected:
                return
            time.sleep(0.2)
        self.fail(f"node {node_id} did not reach scheduling status {expected}")


if __name__ == "__main__":
    unittest.main()
