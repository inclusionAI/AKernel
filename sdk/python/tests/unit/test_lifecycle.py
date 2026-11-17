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

"""Lifecycle ownership, authoritative state and filtered pagination regressions."""

import unittest
from unittest.mock import MagicMock, patch

import httpx

from akernel_sdk import Sandbox, SandboxQuery, SandboxState
from akernel_sdk import sandbox as public
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends.adx import AdxBackend
from akernel_sdk._backends.base import BackendConfig, SandboxConnectSpec
from akernel_sdk._backends.errors import BackendOperationError
from akernel_sdk.types import SandboxInfo


class PublicLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.backend = MagicMock()
        self.session = MagicMock(id="sandbox-a")
        self.session.get_info.return_value = SandboxInfo(
            "sandbox-a", "running", 1000, 1024, "python"
        )
        self.backend.create.return_value = self.session
        self.backend.connect.return_value = self.session
        patcher = patch.object(public, "load_backend", return_value=self.backend)
        self.load = patcher.start()
        self.addCleanup(patcher.stop)

    def test_class_and_instance_identity_and_lookup(self):
        source = Sandbox()
        self.assertEqual(source.sandbox_id, source.id)
        self.assertEqual(source.get_info().sandbox_id, source.id)
        self.backend.get_sandbox.return_value = source.get_info()
        self.assertEqual(Sandbox.get_info("sandbox-a").id, source.id)
        self.assertEqual(Sandbox.get_by_id("sandbox-a").id, source.id)
        source.kill()

    def test_close_keeps_remote_and_owned_kill_after_close_deletes(self):
        source = Sandbox()
        source.close()
        source.close()
        self.session.terminate.assert_not_called()
        self.session.close.assert_called_once()
        source.kill()
        source.kill()
        self.session.terminate.assert_called_once()

    def test_detached_legacy_kill_and_explicit_remote_delete(self):
        source = Sandbox(detached=True)
        source.kill()
        self.session.terminate.assert_not_called()
        source.kill(delete_remote=True)
        source.kill(delete_remote=True)
        self.session.terminate.assert_called_once()

    def test_remote_delete_error_is_retryable_after_local_close(self):
        source = Sandbox()
        self.session.terminate.side_effect = [BackendOperationError("unknown"), None]
        with self.assertRaises(BackendOperationError):
            source.kill()
        source.kill()
        self.assertEqual(self.session.terminate.call_count, 2)
        self.session.close.assert_called_once()

    def test_by_id_delete_preserves_named_delete_and_errors(self):
        self.backend.delete_sandbox.return_value = True
        self.assertTrue(Sandbox.kill("sandbox-a"))
        self.backend.delete_sandbox.assert_called_once_with("sandbox-a")
        Sandbox.delete(sandbox_id="sandbox-b")
        self.backend.delete_sandbox.assert_called_with("sandbox-b")
        Sandbox.delete("legacy-name")
        self.backend.delete_named.assert_called_once_with("legacy-name")
        with self.assertRaises(ValueError):
            Sandbox.delete("legacy-name", sandbox_id="sandbox-a")

    def test_pause_alias_and_unknown_outcomes_are_not_false(self):
        source = Sandbox()
        self.session.pause.side_effect = [True, False, BackendOperationError("unknown")]
        self.assertTrue(source.pause(timeout=42))
        self.assertFalse(source.beta_pause(timeout=42))
        with self.assertRaises(BackendOperationError):
            source.pause()
        self.assertEqual(self.session.pause.call_count, 3)
        source.close()
        with self.assertRaises(RuntimeError):
            source.pause()
        source.kill()

    def test_connect_never_creates_and_uses_fresh_facades(self):
        with Sandbox.connect("sandbox-a", cwd="/existing") as connected:
            self.assertEqual(connected.id, "sandbox-a")
            spec = self.backend.connect.call_args.args[0]
            self.assertEqual(spec.command_cwd, "/existing")
            self.session.files.make_dir.assert_not_called()
            self.backend.create.assert_not_called()
        self.session.terminate.assert_called_once()

    def test_attachment_binding_failure_must_not_delete_target(self):
        failure = BackendOperationError("metadata unavailable")
        self.session.get_info.side_effect = failure
        with self.assertRaises(BackendOperationError) as raised:
            Sandbox.connect("sandbox-a")
        self.assertIs(raised.exception, failure)
        self.session.terminate.assert_not_called()
        self.session.close.assert_called_once()
        self.assertEqual(failure.environment_id, "sandbox-a")

    def test_lazy_pages_preserve_failed_cursor(self):
        info = SandboxInfo("sandbox-a", "paused", 1000, 1024, "python")
        self.backend.list_sandboxes.side_effect = [
            ([info], "next"),
            BackendOperationError("unavailable"),
            ([], None),
        ]
        query = SandboxQuery(state=[SandboxState.PAUSED])
        pages = Sandbox.list(query=query, limit=1)
        self.backend.list_sandboxes.assert_not_called()
        self.assertEqual(pages.next_items(), [info])
        with self.assertRaises(BackendOperationError):
            pages.next_items()
        self.assertEqual(pages.next_token, "next")
        self.assertEqual(pages.next_items(), [])
        self.assertFalse(pages.has_next)
        self.backend.list_sandboxes.assert_called_with(query, "next", 1)

    def test_validation_precedes_backend_access(self):
        for method, options in (
            (Sandbox.connect, {"sandbox_id": ""}),
            (Sandbox.connect, {"sandbox_id": "a", "timeout": 0}),
            (Sandbox.connect, {"sandbox_id": "a", "cwd": "relative"}),
            (Sandbox.connect, {"sandbox_id": "a", "detached": 1}),
            (Sandbox.list, {"limit": True}),
            (Sandbox.list, {"limit": 1001}),
            (Sandbox.list, {"query": {}}),
            (Sandbox.kill, {"sandbox_id": ""}),
        ):
            with (
                self.subTest(method=method, options=options),
                self.assertRaises((TypeError, ValueError)),
            ):
                method(**options)
        self.load.assert_not_called()


class NativeLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.backend = AdxBackend(
            BackendConfig(
                Endpoint("api.example", 443, "https", True),
                Endpoint("gateway.example", 443, "https", True),
                "test-only",
                True,
            )
        )

    def test_state_filter_fills_page_without_losing_server_cursor(self):
        requests = []
        pages = [
            {
                "items": [
                    {"id": "failed", "status": "failed"},
                    {"id": "paused", "status": "paused"},
                ],
                "nextPageToken": "one",
            },
            {"items": [{"id": "running", "status": "running"}], "nextPageToken": "two"},
            {"items": [{"id": "last", "status": "paused"}]},
        ]

        def respond(_client, request, **kwargs):
            requests.append(request)
            return httpx.Response(200, json=pages.pop(0), request=request)

        with patch.object(httpx.Client, "send", autospec=True, side_effect=respond):
            first, token = self.backend.list_sandboxes(SandboxQuery(), None, 2)
            second, end = self.backend.list_sandboxes(SandboxQuery(), token, 2)
        self.assertEqual([row.id for row in first], ["paused", "running"])
        self.assertEqual([row.id for row in second], ["last"])
        self.assertEqual(token, "two")
        self.assertIsNone(end)
        self.assertEqual([r.url.params["pageSize"] for r in requests], ["2", "1", "2"])
        self.assertEqual(requests[2].url.params["pageToken"], "two")

    def test_absent_and_forbidden_delete_are_distinct(self):
        for status in (404, 403):
            requests = []

            def respond(_client, request, _requests=requests, _status=status, **kwargs):
                _requests.append(request)
                return httpx.Response(
                    _status, json={"message": "missing or denied"}, request=request
                )

            with patch.object(httpx.Client, "send", autospec=True, side_effect=respond):
                if status == 404:
                    self.assertFalse(self.backend.delete_sandbox("absent"))
                else:
                    with self.assertRaises(BackendOperationError):
                        self.backend.delete_sandbox("forbidden")
            self.assertTrue(all(r.method == "GET" for r in requests))

    def test_connect_resumes_once_and_never_creates_or_deletes(self):
        control = MagicMock()
        control.environment_info.side_effect = [
            {"status": "paused"},
            {"status": "running"},
        ]
        native = MagicMock(id="sandbox-a")
        native.commands.list.return_value = []
        with (
            patch("akernel_sdk._backends.adx.EnvironmentClient", return_value=control),
            patch(
                "akernel_sdk._backends.adx._OwnedSandbox.from_id", return_value=native
            ),
            patch("akernel_sdk._backends.adx.time.sleep"),
        ):
            session = self.backend.connect(
                SandboxConnectSpec("sandbox-a", 60, None, (), None, False)
            )
        self.assertEqual(session.id, "sandbox-a")
        control.resume.assert_called_once_with("sandbox-a")
        control.delete.assert_not_called()
        native.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
