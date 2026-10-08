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

"""A second tenant cannot read or delete a sandbox owned by the first tenant."""

import json
import os
import ssl
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

import adx_sandbox

from akernel_sdk import Sandbox
from akernel_sdk._addresses import api_endpoint_from_env
from tests.e2e.full._keys import create_key, management_enabled, revoke_key

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


def _list_instances(token: str) -> list[dict]:
    endpoint = api_endpoint_from_env()
    context = None
    if endpoint.use_tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    items: list[dict] = []
    page_token = ""
    seen_tokens: set[str] = set()
    while True:
        query = {"pageSize": "1000"}
        if page_token:
            query["pageToken"] = page_token
        request = urllib.request.Request(
            endpoint.base_url()
            + "/api/instances?"
            + urllib.parse.urlencode(query),
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(
            request, timeout=20, context=context
        ) as response:
            payload = json.load(response)
        page_items = payload.get("items") if isinstance(payload, dict) else None
        next_token = (
            payload.get("nextPageToken") if isinstance(payload, dict) else None
        )
        if not isinstance(page_items, list) or not isinstance(next_token, str):
            raise AssertionError("instance list has an invalid paginated response")
        items.extend(page_items)
        if not next_token:
            return items
        if next_token in seen_tokens:
            raise AssertionError("instance list repeated a page token")
        seen_tokens.add(next_token)
        page_token = next_token


@unittest.skipUnless(_ENABLED, "requires SDK credentials")
class TenantIsolationIntegrationTest(unittest.TestCase):
    def _other_connection(self):
        key_id = None
        other_token = os.environ.get("AKERNEL_SECOND_TENANT_TOKEN")
        if not other_token:
            if not management_enabled():
                self.skipTest(
                    "set AKERNEL_SECOND_TENANT_TOKEN or AKERNEL_TEST_MANAGE_KEYS=1"
                )
            key_id, other_token = create_key()
        endpoint = api_endpoint_from_env()
        return key_id, adx_sandbox.ConnectionConfig(
            server_address=endpoint.authority(),
            token=other_token,
            use_tls=endpoint.use_tls,
        )

    def test_cross_tenant_instance_lookup_is_denied(self):
        key_id, other = self._other_connection()
        try:
            with Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048) as sandbox:
                self.assertTrue(sandbox.is_running())
                with self.assertRaises(adx_sandbox.PermissionDenied):
                    adx_sandbox.Sandbox.from_id(sandbox.id, connection=other)
        finally:
            if key_id is not None:
                revoke_key(key_id)

    def test_cross_tenant_delete_is_denied_and_owner_keeps_running(self):
        key_id, other = self._other_connection()
        try:
            with Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048) as sandbox:
                with self.assertRaises(adx_sandbox.PermissionDenied):
                    adx_sandbox.Sandbox.delete(sandbox.id, connection=other)
                self.assertTrue(sandbox.is_running())
                result = sandbox.commands.run("printf owner-still-running")
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(result.stdout, "owner-still-running")
        finally:
            if key_id is not None:
                revoke_key(key_id)

    def test_cross_tenant_list_excludes_owner_instance(self):
        key_id, other = self._other_connection()
        try:
            with Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048) as sandbox:
                owner_token = os.environ["AKERNEL_TOKEN"]
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    owner_items = _list_instances(owner_token)
                    owner_ids = {item.get("id") for item in owner_items}
                    if sandbox.id in owner_ids:
                        break
                    time.sleep(0.2)
                else:
                    self.fail("owner's running Sandbox never entered the public list")
                other_ids = {item.get("id") for item in _list_instances(other.token)}
                self.assertNotIn(sandbox.id, other_ids)
        finally:
            if key_id is not None:
                revoke_key(key_id)

    def test_tenant_key_cannot_create_an_administrator_key(self):
        if not management_enabled():
            self.skipTest("set AKERNEL_TEST_MANAGE_KEYS=1")
        key_id, other = self._other_connection()
        endpoint = api_endpoint_from_env()
        request = urllib.request.Request(
            endpoint.base_url() + "/api/admin/v1/keys",
            data=b'{"tenantId":"e2e-forbidden-admin-create"}',
            method="POST",
            headers={
                "Authorization": "Bearer " + other.token,
                "Content-Type": "application/json",
            },
        )
        context = None
        if endpoint.use_tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request, timeout=20, context=context)
            try:
                self.assertEqual(denied.exception.code, 403)
            finally:
                denied.exception.close()
        finally:
            if key_id is not None:
                revoke_key(key_id)


if __name__ == "__main__":
    unittest.main()
