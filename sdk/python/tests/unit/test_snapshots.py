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

"""Public snapshot lifecycle, inheritance and real native wire contracts."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from akernel_sdk import Sandbox, SnapshotInfo, SnapshotPaginator
from akernel_sdk import sandbox as public
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends import adx
from akernel_sdk._backends.base import BackendConfig
from akernel_sdk._backends.errors import BackendOperationError
from akernel_sdk.types import HttpReverseTunnel, Mount, NetworkPolicy, SandboxInfo


class PublicSnapshotsTest(unittest.TestCase):
    def setUp(self):
        self.backend = MagicMock()
        self.session = self.backend.from_snapshot.return_value
        self.session.rollback_creation = self.session.terminate
        self.session.id = "clone"
        self.session.get_info.return_value = SandboxInfo(
            "clone", "running", 2500, 6000, "captured-image"
        )
        patcher = patch.object(public, "load_backend", return_value=self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_restore_binds_fresh_facades_and_inherits_resources(self):
        with Sandbox.from_snapshot(SnapshotInfo("saved"), cwd="/not-created") as sb:
            self.assertEqual(sb.id, "clone")
            self.assertEqual(sb.get_info().cpu, 2500)
            self.assertIs(sb.commands._driver, self.session.commands)
            self.assertIsNone(sb.startup_command)
            self.backend.create.assert_not_called()
            self.session.files.make_dir.assert_not_called()
            spec = self.backend.from_snapshot.call_args.args[0]
            self.assertEqual(spec.snapshot_id, "saved")
            self.assertFalse(hasattr(spec, "cpu"))
            self.assertFalse(spec.inherit_entrypoint)
        self.session.terminate.assert_called_once()

    def test_restore_rebinds_tunnel_mounts_policy_and_placement(self):
        tunnel = HttpReverseTunnel(target="http://127.0.0.1:8123")
        mount = Mount(target="/harness", image_url="tools:latest")
        policy = NetworkPolicy(block_network=True)
        sb = Sandbox.from_snapshot(
            "saved",
            reverse_tunnel=tunnel,
            mounts=[mount],
            network_policy=policy,
            node_id="node-a",
            port_forwardings=[8080],
        )
        spec = self.backend.from_snapshot.call_args.args[0]
        self.assertIs(spec.reverse_tunnel, tunnel)
        self.assertEqual(spec.mounts, (mount,))
        self.assertIs(spec.network_policy, policy)
        self.assertEqual(spec.node_id, "node-a")
        self.assertIs(sb.reverse_tunnel, tunnel)
        sb.kill()

    def test_restore_binding_failure_cleans_clone_and_preserves_error(self):
        failure = BackendOperationError("info unavailable", outcome="unknown")
        self.session.get_info.side_effect = failure
        with self.assertRaises(BackendOperationError) as raised:
            Sandbox.from_snapshot("saved")
        self.assertIs(raised.exception, failure)
        self.session.terminate.assert_called_once()
        self.backend.create.assert_not_called()

    def test_ambiguous_restore_is_not_replayed(self):
        failure = BackendOperationError("unknown", request_id="req", outcome="unknown")
        self.backend.from_snapshot.side_effect = failure
        with self.assertRaises(BackendOperationError) as raised:
            Sandbox.from_snapshot("saved")
        self.assertIs(raised.exception, failure)
        self.backend.from_snapshot.assert_called_once()

    def test_snapshot_source_lifetime_is_independent(self):
        self.backend.create.return_value = self.session
        self.session.create_snapshot.return_value = SnapshotInfo("saved", ("turn-1",))
        with Sandbox() as sb:
            info = sb.create_snapshot("turn-1", timeout=45)
            self.assertEqual(info.snapshot_id, "saved")
            self.session.create_snapshot.assert_called_once_with("turn-1", 45)
            self.backend.delete_snapshot.assert_not_called()
        self.backend.delete_snapshot.assert_not_called()
        self.backend.get_snapshot.return_value = info
        self.assertEqual(Sandbox.get_snapshot(info), info)
        Sandbox.delete_snapshot(info)
        self.backend.delete_snapshot.assert_called_once_with("saved")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            sb.create_snapshot()

    def test_snapshot_validation_precedes_backend_mutation(self):
        self.backend.create.return_value = self.session
        sb = Sandbox()
        for kwargs in (
            {"timeout": True},
            {"timeout": 0},
            {"timeout": 3601},
            {"name": " "},
        ):
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaises((TypeError, ValueError)),
            ):
                sb.create_snapshot(**kwargs)
        self.session.create_snapshot.assert_not_called()
        sb.kill()
        for kwargs in (
            {"snapshot_id": ""},
            {"snapshot_id": "x", "timeout": 0},
            {"snapshot_id": "x", "detached": 1},
            {"snapshot_id": "x", "cwd": "relative"},
            {"snapshot_id": "x", "mounts": [object()]},
            {"snapshot_id": "x", "network_policy": {}},
        ):
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaises((TypeError, ValueError)),
            ):
                Sandbox.from_snapshot(**kwargs)
        self.backend.from_snapshot.assert_not_called()

    def test_lazy_pagination_resumes_and_does_not_advance_on_failure(self):
        first, second = SnapshotInfo("one"), SnapshotInfo("two")
        self.backend.list_snapshots.side_effect = [
            ([first], "next"),
            BackendOperationError("temporary"),
            ([second], None),
        ]
        pages = Sandbox.list_snapshots(name="label", limit=1)
        self.backend.list_snapshots.assert_not_called()
        self.assertTrue(pages.has_next)
        self.assertEqual(pages.next_items(), [first])
        self.assertEqual(pages.next_token, "next")
        with self.assertRaises(BackendOperationError):
            pages.next_items()
        self.assertEqual(pages.next_token, "next")
        self.assertEqual(pages.next_items(), [second])
        self.assertFalse(pages.has_next)
        self.assertEqual(pages.next_items(), [])
        self.assertEqual(self.backend.list_snapshots.call_count, 3)
        self.backend.list_snapshots.assert_called_with("label", "next", 1)
        resumed = Sandbox.list_snapshots(next_token="saved-cursor")
        self.assertEqual(resumed.next_token, "saved-cursor")

    def test_confirmed_kill_reports_stopped_without_using_closed_client(self):
        sb = Sandbox.from_snapshot("saved")
        sb.kill()
        self.session.get_info.side_effect = RuntimeError("closed transport")
        self.assertEqual(sb.get_info().state, "stopped")

    def test_unconfirmed_kill_and_detached_close_do_not_report_stopped(self):
        sb = Sandbox.from_snapshot("saved")
        self.session.terminate.side_effect = BackendOperationError("delete unknown")
        with self.assertRaises(BackendOperationError):
            sb.kill()
        self.session.get_info.side_effect = BackendOperationError("cannot observe")
        with self.assertRaises(BackendOperationError):
            sb.get_info()
        self.session.get_info.side_effect = None
        self.session.terminate.side_effect = None
        detached = Sandbox.from_snapshot("saved", detached=True)
        detached.kill()
        self.session.get_info.side_effect = BackendOperationError("closed client")
        with self.assertRaises(BackendOperationError):
            detached.get_info()

    def test_bad_listing_options_and_nonadvancing_cursor(self):
        for kwargs in ({"limit": True}, {"limit": 0}, {"next_token": 7}, {"name": ""}):
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaises((TypeError, ValueError)),
            ):
                Sandbox.list_snapshots(**kwargs)
        paginator = SnapshotPaginator(lambda token: ([], token), "stuck")
        with self.assertRaisesRegex(RuntimeError, "non-advancing"):
            paginator.next_items()


class NativeSnapshotsTest(unittest.TestCase):
    def setUp(self):
        self.config = BackendConfig(
            Endpoint("api.example", 443, "https", True),
            Endpoint("gateway.example", 8443, "https", True),
            "test-only",
            True,
        )
        self.backend = adx.AdxBackend(self.config)

    def test_restore_wire_has_no_fresh_boot_resources_and_preserves_error(self):
        requests = []

        def deny(_client, request, **kwargs):
            requests.append(request)
            return httpx.Response(
                403,
                json={
                    "message": "denied",
                    "error": {
                        "code": "PERMISSION_DENIED",
                        "retry": "never",
                        "outcome": "not_started",
                        "requestId": "restore-test",
                    },
                },
                request=request,
            )

        with (
            patch.object(public, "load_backend", return_value=self.backend),
            patch.object(httpx.Client, "send", autospec=True, side_effect=deny),
            self.assertRaises(BackendOperationError) as raised,
        ):
            Sandbox.from_snapshot(
                "saved",
                cwd="/app",
                node_id="node-a",
                extra_config={
                    "fileBackedTmpfsMounts": [{"target": "/upper", "sizeMB": 64}]
                },
            )
        self.assertEqual(len(requests), 1)
        body = json.loads(requests[0].content)
        self.assertEqual(body["snapshotId"], "saved")
        for key in (
            "cpu",
            "memory",
            "cpu_limit",
            "mem_limit",
            "env",
            "inheritEntrypoint",
        ):
            self.assertNotIn(key, body)
        self.assertEqual(body["rootfs"], {})
        self.assertEqual(body["cwd"], "/app")
        self.assertEqual(
            body["extra_config"],
            {"fileBackedTmpfsMounts": [{"target": "/upper", "sizeMB": 64}]},
        )
        self.assertEqual(raised.exception.code, "PERMISSION_DENIED")
        self.assertEqual(raised.exception.request_id, "restore-test")
        self.assertEqual(raised.exception.outcome, "not_started")

    def test_restore_transport_retries_keep_one_logical_identity(self):
        requests = []

        def fail(_client, request, **kwargs):
            requests.append(request)
            raise httpx.ReadTimeout("lost reply", request=request)

        with (
            patch.object(public, "load_backend", return_value=self.backend),
            patch.object(httpx.Client, "send", autospec=True, side_effect=fail),
            patch.object(adx.time, "sleep"),
            self.assertRaises(BackendOperationError) as raised,
        ):
            Sandbox.from_snapshot("saved")
        self.assertGreater(len(requests), 1)
        self.assertEqual(len({r.headers["X-Request-Id"] for r in requests}), 1)
        self.assertEqual(len({json.loads(r.content)["name"] for r in requests}), 1)
        self.assertTrue(all(r.method == "POST" for r in requests))
        self.assertEqual(raised.exception.outcome, "unknown")
        self.assertEqual(
            raised.exception.request_id, requests[0].headers["X-Request-Id"]
        )

    def test_catalog_uses_native_encoder_and_backend_neutral_values(self):
        requests = []

        def respond(_client, request, **kwargs):
            requests.append(request)
            payload = (
                {
                    "items": [{"snapshotId": "s", "names": ["turn"]}],
                    "nextPageToken": "cursor",
                }
                if request.url.path.endswith("/snapshots")
                else {"snapshotId": "s", "names": ["turn"]}
            )
            return httpx.Response(
                200, json={"code": 200, "data": payload}, request=request
            )

        with patch.object(httpx.Client, "send", autospec=True, side_effect=respond):
            info = self.backend.get_snapshot("s")
            rows, cursor = self.backend.list_snapshots("turn", "previous", 2)
            self.backend.delete_snapshot("s")
        self.assertIs(type(info), SnapshotInfo)
        self.assertEqual(rows, [info])
        self.assertEqual(cursor, "cursor")
        self.assertEqual(
            dict(requests[1].url.params),
            {
                "name": "turn",
                "pageToken": "previous",
                "pageSize": "2",
            },
        )
        self.assertEqual(requests[2].method, "DELETE")
        self.assertTrue(requests[2].headers["X-ADX-Request-ID"])

    def test_missing_snapshot_delete_is_idempotent_but_permission_failure_visible(self):
        for status in (404, 403):

            def respond(_client, request, response_status=status, **kwargs):
                return httpx.Response(
                    response_status,
                    json={"message": "missing or denied"},
                    request=request,
                )

            with patch.object(httpx.Client, "send", autospec=True, side_effect=respond):
                if status == 404:
                    self.backend.delete_snapshot("missing")
                else:
                    with self.assertRaises(BackendOperationError) as raised:
                        self.backend.delete_snapshot("denied")
                    self.assertEqual(raised.exception.status_code, 403)

    def test_create_snapshot_unknown_outcome_keeps_request_context_and_no_retry(self):
        native = adx._OwnedSandbox.__new__(adx._OwnedSandbox)
        native._closed = False
        native._tunnel_client = None
        native._sid = "source"
        from adx_environment._transport import EnvironmentClient

        native._client = EnvironmentClient(connection=self.backend._connection)
        native._commands = MagicMock()
        native._files = MagicMock()
        requests = []

        def fail(_client, request, **kwargs):
            requests.append(request)
            raise httpx.ReadTimeout("lost reply", request=request)

        session = adx._Session(native, MagicMock(), self.backend._connection)
        try:
            with (
                patch.object(httpx.Client, "send", autospec=True, side_effect=fail),
                self.assertRaises(BackendOperationError) as raised,
            ):
                session.create_snapshot("turn", 45)
            self.assertEqual(len(requests), 1)
            self.assertEqual(
                json.loads(requests[0].content),
                {
                    "name": "turn",
                    "timeoutSeconds": 45,
                },
            )
            self.assertEqual(raised.exception.outcome, "unknown")
            self.assertEqual(
                raised.exception.request_id, requests[0].headers["X-ADX-Request-ID"]
            )
        finally:
            native._client.close()

    def test_restore_waits_for_route_without_replaying_create(self):
        native = MagicMock(id="clone", newly_created=True)
        error = adx.adx_environment.EnvironmentError("not ready")
        error.status_code = 503
        native.commands.list.side_effect = [error, []]
        with (
            patch.object(adx._OwnedSandbox, "create", return_value=native) as create,
            patch.object(adx.time, "sleep"),
            patch.object(adx.adx_environment.Environment, "delete"),
            patch.object(public, "load_backend", return_value=self.backend),
        ):
            native.get_info.return_value = SimpleNamespace(
                id="clone", state="running", cpu=2000, memory=8192, image="saved-image"
            )
            sb = Sandbox.from_snapshot("s")
            self.assertEqual(create.call_count, 1)
            self.assertEqual(native.commands.list.call_count, 2)
            native.files.make_dir.assert_not_called()
            self.assertNotIn("cpu", create.call_args.kwargs)
            self.assertTrue(create.call_args.kwargs["data_plane_security"])
            sb.kill()

    def test_readiness_failure_cleans_clone_without_repeating_restore(self):
        native = MagicMock(id="clone", newly_created=True)
        native.commands.list.side_effect = ValueError("bad response")
        with (
            patch.object(adx._OwnedSandbox, "create", return_value=native) as create,
            patch.object(adx.adx_environment.Environment, "delete") as delete,
            patch.object(public, "load_backend", return_value=self.backend),
            self.assertRaises(BackendOperationError),
        ):
            Sandbox.from_snapshot("s")
        create.assert_called_once()
        delete.assert_called_once_with("clone", connection=self.backend._connection)

    def test_pending_command_pid_is_resolved_by_stable_id(self):
        commands = MagicMock()
        commands.run.return_value = SimpleNamespace(pid=None, id="submitted")
        commands.list.side_effect = [
            [],
            [
                SimpleNamespace(id="other", pid=4),
                SimpleNamespace(id="submitted", pid=23),
            ],
        ]
        driver = adx._CommandsDriver(commands)
        with patch.object(adx.time, "sleep"):
            self.assertEqual(
                driver.start("do-once", envs=None, cwd=None, stdin=False).pid, 23
            )
        commands.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
