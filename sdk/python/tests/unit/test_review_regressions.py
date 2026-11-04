"""PR #128 regressions: replayed policy, stable IDs, cancellation and access."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from adx_environment._transport import PermissionDenied

from akernel_sdk import (
    BackendOperationError,
    Client,
    NetworkPolicy,
    PermissionDeniedException,
    Sandbox,
    SandboxInfo,
    SnapshotInfo,
)
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends import adx
from akernel_sdk._backends.base import BackendConfig, ForkOutcome
from akernel_sdk.commands import CommandHandle, Commands
from tests.unit.test_adx_backend import _spec


def backend():
    endpoint = Endpoint("fixture.invalid", 443, "https", True)
    return adx.AdxBackend(BackendConfig(endpoint, endpoint, "fixture", True))


def session(identity):
    value = MagicMock(id=identity)
    value.terminate.return_value = True
    value.rollback_creation = value.terminate
    value.get_info.return_value = SandboxInfo(identity, "running", 1000, 1024, "image")
    return value


class ReusedPolicyTest(unittest.TestCase):
    def test_unconfirmed_creation_cannot_fork_until_policy_reapplied(self):
        for ownership in (False, None, "true"):
            with self.subTest(ownership=ownership):
                calls = []

                def send(
                    _client, request, _ownership=ownership, _calls=calls, **_kwargs
                ):
                    body = json.loads(request.content)
                    _calls.append(body)
                    if body.get("action") == "process.list":
                        return httpx.Response(
                            200, json={"processes": []}, request=request
                        )
                    result = {"environmentId": "default-shared", "status": "running"}
                    if _ownership is not None:
                        result["newlyCreated"] = _ownership
                    return httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        content="event: final\ndata: " + json.dumps(result) + "\n\n",
                        request=request,
                    )

                adapter = backend()
                with patch.object(
                    httpx.Client, "send", autospec=True, side_effect=send
                ):
                    source = adapter.create(_spec(name="shared", detached=True))
                self.addCleanup(source.close)
                with (
                    patch.object(
                        source, "create_snapshot", return_value=SnapshotInfo("s")
                    ) as capture,
                    patch.object(
                        adx.AdxBackend,
                        "_restore_snapshot",
                        return_value=session("child"),
                    ),
                    patch.object(adx.AdxBackend, "_delete_snapshot"),
                ):
                    with self.assertRaises(BackendOperationError) as raised:
                        source.fork(1, None, 300, None)
                    self.assertEqual(
                        raised.exception.code, "NETWORK_POLICY_UNCONFIRMED"
                    )
                    self.assertEqual(raised.exception.outcome, "not_started")
                    capture.assert_not_called()
                    policy = NetworkPolicy.block()
                    with patch.object(source._sandbox, "update_network_policy"):
                        source.update_network_policy(policy)
                    with (
                        patch.object(
                            adx.AdxBackend,
                            "_restore_snapshot",
                            return_value=session("child"),
                        ) as restore,
                        patch.object(adx.AdxBackend, "_delete_snapshot"),
                    ):
                        source.fork(1, None, 300, None)
                    self.assertIs(restore.call_args.args[0].network_policy, policy)


class StableCommandTest(unittest.TestCase):
    def handles(self):
        first, second = (
            MagicMock(pid=42, id="command-A"),
            MagicMock(pid=42, id="command-B"),
        )
        for handle, output in ((first, "A"), (second, "B")):
            handle.wait.return_value = SimpleNamespace(
                stdout=output, stderr="", exit_code=0
            )
        native = MagicMock()
        native.get.side_effect = {"command-A": first, "command-B": second}.__getitem__
        native.run.side_effect = [first, second]
        return native, first, second

    def test_reconnected_handles_keep_identity_wait_and_control_on_pid_collision(self):
        native, first, second = self.handles()
        commands = Commands(adx._CommandsDriver(native))
        a = commands.connect("command-A")
        b = commands.connect("command-B")
        self.assertEqual((a.command_id, b.command_id), ("command-A", "command-B"))
        self.assertEqual((a.wait().stdout, b.wait().stdout), ("A", "B"))
        a.send_stdin("input")
        a.close_stdin()
        a.kill()
        first.send_stdin.assert_any_call("input", False)
        first.send_stdin.assert_any_call("", True)
        first.kill.assert_called_once()
        second.send_stdin.assert_not_called()
        second.kill.assert_not_called()
        native.run.assert_not_called()

    def test_background_start_reference_survives_interleaved_pid_reuse(self):
        native, _first, _second = self.handles()
        driver = adx._CommandsDriver(native)
        first = driver.start("first", envs=None, cwd=None, stdin=False)
        second = driver.start("second", envs=None, cwd=None, stdin=False)
        a, b = CommandHandle(first, driver), CommandHandle(second, driver)
        self.assertEqual((a.command_id, b.command_id), ("command-A", "command-B"))
        self.assertEqual((a.wait().stdout, b.wait().stdout), ("A", "B"))
        self.assertEqual(native.run.call_count, 2)


class NativePermissionTest(unittest.TestCase):
    def test_real_native_directory_permission_conversion_is_preserved(self):
        client = Client(api_url="https://fixture.invalid", api_key="fixture")
        for operation in ("get_info", "get_by_id", "kill", "connect"):
            with self.subTest(operation=operation):
                requests = []

                def deny(_client, request, _requests=requests, **_kwargs):
                    _requests.append(request.method)
                    return httpx.Response(
                        403, json={"message": "denied"}, request=request
                    )

                with patch.object(
                    httpx.Client, "send", autospec=True, side_effect=deny
                ):
                    with self.assertRaises(PermissionDeniedException) as raised:
                        getattr(client.Sandbox, operation)("default-forbidden")
                error = raised.exception
                self.assertIsInstance(error.__cause__, PermissionDenied)
                self.assertEqual(error.code, "PERMISSION_DENIED")
                self.assertEqual(error.environment_id, "default-forbidden")
                self.assertEqual(error.retry, "never")
                self.assertEqual(error.outcome, "not_started")
                self.assertIsNone(error.status_code)
                self.assertEqual(requests, ["GET"])


class ForkCancellationTest(unittest.TestCase):
    def test_final_delete_interruption_keeps_failed_cleanup_handle(self):
        child = session("first")
        interruption = KeyboardInterrupt("final delete")
        deletion = RuntimeError("child delete")
        snapshot_deletion = RuntimeError("snapshot delete")
        child.terminate.side_effect = deletion
        with (
            patch.object(adx.AdxBackend, "_restore_snapshot", return_value=child),
            patch.object(
                adx.AdxBackend,
                "_delete_snapshot",
                side_effect=[interruption, snapshot_deletion],
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.public.fork()
        self.assertIs(raised.exception, interruption)
        self.assertEqual(interruption.snapshot_id, "temporary")
        self.assertEqual(interruption.pending_environment_ids, ("first",))
        self.assertIn(deletion, interruption.cleanup_errors)
        self.assertIn(snapshot_deletion, interruption.cleanup_errors)
        child.terminate.side_effect = None
        interruption.pending_sandboxes[0].kill()

    def test_second_interruption_in_cleanup_preserves_first_and_continues(self):
        first, second = session("first"), session("second")
        interruption = SystemExit("original cancellation")
        cleanup_interruption = KeyboardInterrupt("cleanup interruption")
        first.terminate.side_effect = cleanup_interruption
        with (
            patch.object(
                adx.AdxBackend,
                "_restore_snapshot",
                side_effect=[first, second, interruption],
            ),
            patch.object(adx.AdxBackend, "_delete_snapshot") as delete,
            self.assertRaises(SystemExit) as raised,
        ):
            self.public.fork(count=3)
        self.assertIs(raised.exception, interruption)
        self.assertIn(cleanup_interruption, interruption.cleanup_errors)
        self.assertEqual(interruption.pending_environment_ids, ("first",))
        second.terminate.assert_called_once()
        delete.assert_called_once()

    def setUp(self):
        self.adapter = backend()
        self.native = MagicMock(id="source", newly_created=True)
        self.source = adx._Session(self.native, _spec(), self.adapter._connection)
        for owner, method, value in (
            (self.source, "create_snapshot", SnapshotInfo("temporary")),
            (self.adapter, "create", self.source),
        ):
            patcher = patch.object(owner, method, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("akernel_sdk.sandbox.load_backend", return_value=self.adapter)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.public = Sandbox()
        self.addCleanup(self.public.close)

    def test_restore_interruption_preserves_failed_child_and_snapshot_cleanup(self):
        child = session("first")
        interruption = KeyboardInterrupt("restore")
        deletion = RuntimeError("child delete failed")
        busy = BackendOperationError("snapshot busy", code="SNAPSHOT_IN_USE")
        child.terminate.side_effect = deletion
        with (
            patch.object(
                adx.AdxBackend, "_restore_snapshot", side_effect=[child, interruption]
            ),
            patch.object(adx.AdxBackend, "_delete_snapshot", side_effect=busy),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.public.fork(count=2)
        self.assertIs(raised.exception, interruption)
        self.assertEqual(interruption.snapshot_id, "temporary")
        self.assertEqual(interruption.pending_environment_ids, ("first",))
        self.assertIn(deletion, interruption.cleanup_errors)
        self.assertIn(busy, interruption.cleanup_errors)
        self.assertEqual(interruption.pending_sandboxes[0].id, "first")
        child.terminate.side_effect = None
        interruption.pending_sandboxes[0].kill()
        self.assertEqual(child.terminate.call_count, 2)
        self.native.close.assert_not_called()

    def test_final_snapshot_deletion_interruption_cleans_children_and_retains_identity(
        self,
    ):
        first, second = session("first"), session("second")
        interruption = KeyboardInterrupt("final snapshot delete")
        with (
            patch.object(
                adx.AdxBackend, "_restore_snapshot", side_effect=[first, second]
            ),
            patch.object(
                adx.AdxBackend, "_delete_snapshot", side_effect=[interruption, None]
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.public.fork(count=2)
        self.assertIs(raised.exception, interruption)
        self.assertEqual(interruption.snapshot_id, "temporary")
        self.assertEqual(interruption.created_environment_ids, ("first", "second"))
        self.assertEqual(interruption.pending_environment_ids, ())
        self.assertEqual(interruption.pending_sandboxes, ())
        first.terminate.assert_called_once()
        second.terminate.assert_called_once()

    def test_public_binding_interruption_retains_all_unresolved_handles(self):
        first, second, third = session("first"), session("second"), session("third")
        interruption = KeyboardInterrupt("binding")
        second.get_info.side_effect = interruption
        second_delete, third_delete = (
            RuntimeError("delete second"),
            RuntimeError("delete third"),
        )
        second.terminate.side_effect = second_delete
        third.terminate.side_effect = third_delete
        snapshot_delete = RuntimeError("delete snapshot")
        with (
            patch.object(
                self.source,
                "fork",
                return_value=ForkOutcome(
                    "temporary", [first, second, third], snapshot_delete
                ),
            ),
            patch.object(
                adx.AdxBackend, "_delete_snapshot", side_effect=snapshot_delete
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.public.fork(count=3)
        self.assertIs(raised.exception, interruption)
        self.assertEqual(interruption.snapshot_id, "temporary")
        self.assertEqual(set(interruption.pending_environment_ids), {"second", "third"})
        self.assertEqual(
            {s.id for s in interruption.pending_sandboxes}, {"second", "third"}
        )
        for error in (second_delete, third_delete, snapshot_delete):
            self.assertIn(error, interruption.cleanup_errors)
        first.terminate.assert_called_once()
        second.terminate.side_effect = third.terminate.side_effect = None
        for handle in interruption.pending_sandboxes:
            handle.kill()
