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

"""Fork partial success, single capture and ownership/cleanup contracts."""

import json
import unittest
from unittest.mock import MagicMock, patch

import httpx

from akernel_sdk import (
    BackendOperationError,
    ForkCleanupError,
    HttpReverseTunnel,
    NetworkPolicy,
    Sandbox,
    SandboxInfo,
    SnapshotInfo,
)
from akernel_sdk import sandbox as public
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends import adx
from akernel_sdk._backends.base import (
    BackendConfig,
    ForkOutcome,
    SnapshotRestoreSpec,
)


def child(identity):
    session = MagicMock(id=identity)
    session.terminate.return_value = True
    session.rollback_creation = session.terminate
    session.get_info.return_value = SandboxInfo(
        identity, "running", 1250, 2048, "captured-image"
    )
    return session


class PublicForkTest(unittest.TestCase):
    def setUp(self):
        self.backend = MagicMock()
        self.session = child("source")
        self.backend.create.return_value = self.session
        patcher = patch.object(public, "load_backend", return_value=self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = Sandbox()
        self.addCleanup(self.source.kill)

    def test_partial_failure_preserves_order_fresh_handles_and_source(self):
        first, third = child("first"), child("third")
        failure = BackendOperationError(
            "no capacity", code="NO_RESOURCE", outcome="not_started"
        )
        self.session.fork.return_value = ForkOutcome(
            "temporary", [first, failure, third]
        )
        results = self.source.fork(count=3)
        self.assertEqual(len(results), 3)
        self.assertIs(results[1], failure)
        self.assertEqual((results[0].id, results[2].id), ("first", "third"))
        self.assertIsNot(results[0].commands, results[2].commands)
        self.assertIs(results[0].commands._driver, first.commands)
        self.assertIs(results[2].files._driver, third.files)
        self.assertIsNone(results[0].startup_command)
        self.backend.from_snapshot.assert_not_called()
        self.session.terminate.assert_not_called()
        self.assertEqual(self.source.id, "source")
        results[0].kill()
        third.terminate.assert_not_called()
        self.session.terminate.assert_not_called()
        results[2].kill()

    def test_invalid_options_fail_before_capture(self):
        for options in (
            {"count": 0},
            {"count": 21},
            {"count": True},
            {"count": 1.5},
            {"snapshot_timeout": 0},
            {"snapshot_timeout": 3601},
            {"snapshot_timeout": True},
            {"restore_timeout": 0},
            {"restore_timeout": True},
            {"idle_timeout": -1},
            {"idle_timeout": True},
        ):
            with (
                self.subTest(options=options),
                self.assertRaises((TypeError, ValueError)),
            ):
                self.source.fork(**options)
        self.session.fork.assert_not_called()
        self.source.kill()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.source.fork()
        self.session.fork.assert_not_called()

    def test_explicit_timeout_units_are_forwarded(self):
        self.session.fork.return_value = ForkOutcome("temporary", [child("first")])
        (result,) = self.source.fork(
            idle_timeout=0, snapshot_timeout=45, restore_timeout=70
        )
        self.session.fork.assert_called_once_with(1, 0, 45, 70)
        result.kill()

    def test_snapshot_cleanup_failure_keeps_child_handles_and_original_error(self):
        first = child("first")
        failure = BackendOperationError(
            "delete unknown", outcome="unknown", request_id="r"
        )
        self.session.fork.return_value = ForkOutcome("temporary", [first], failure)
        with self.assertRaises(ForkCleanupError) as raised:
            self.source.fork()
        error = raised.exception
        self.assertEqual(error.snapshot_id, "temporary")
        self.assertIs(error.cleanup_error, failure)
        self.assertIs(error.__cause__, failure)
        self.assertEqual(error.results[0].id, "first")
        first.terminate.assert_not_called()
        error.results[0].kill()
        first.terminate.assert_called_once()
        self.session.terminate.assert_not_called()

    def test_child_binding_failure_is_local_and_cleans_only_that_child(self):
        first, second = child("first"), child("second")
        failure = BackendOperationError("child info failed")
        first.get_info.side_effect = failure
        self.session.fork.return_value = ForkOutcome("temporary", [first, second])
        results = self.source.fork(count=2)
        self.assertIs(results[0], failure)
        first.terminate.assert_called_once()
        second.terminate.assert_not_called()
        self.session.terminate.assert_not_called()
        results[1].kill()

    def test_detached_source_has_owned_children(self):
        self.source.kill()
        self.source = Sandbox(detached=True)
        forked = child("owned")
        self.session.fork.return_value = ForkOutcome("temporary", [forked])
        (result,) = self.source.fork()
        self.assertFalse(result._detached)
        result.kill()
        forked.terminate.assert_called_once()
        self.source.kill()

    def test_failed_binding_cleanup_retains_retryable_owned_handle(self):
        first = child("first")
        failure = BackendOperationError("binding failed")
        cleanup = BackendOperationError("delete failed", outcome="unknown")
        first.get_info.side_effect = failure
        first.terminate.side_effect = cleanup
        self.session.fork.return_value = ForkOutcome("temporary", [first])
        with self.assertLogs(public.logger, level="WARNING"):
            with self.assertRaises(ForkCleanupError) as raised:
                self.source.fork()
        error = raised.exception
        self.assertIs(error.results[0], failure)
        self.assertIs(error.cleanup_error, cleanup)
        self.assertEqual(error.pending_sandboxes[0].id, "first")
        self.assertEqual(error.child_cleanup_errors, (cleanup,))
        first.terminate.side_effect = None
        error.pending_sandboxes[0].kill()
        self.assertEqual(first.terminate.call_count, 2)

    def test_interrupt_during_binding_cleans_all_known_children(self):
        first, second = child("first"), child("second")
        first.get_info.side_effect = KeyboardInterrupt()
        self.session.fork.return_value = ForkOutcome("temporary", [first, second])
        with self.assertRaises(KeyboardInterrupt):
            self.source.fork(count=2)
        first.terminate.assert_called()
        second.terminate.assert_called_once()
        self.session.terminate.assert_not_called()


class NativeForkTest(unittest.TestCase):
    def setUp(self):
        config = BackendConfig(
            Endpoint("api.example", 443, "https", True),
            Endpoint("gateway.example", 443, "https", True),
            "test-only",
            True,
        )
        self.backend = adx.AdxBackend(config)
        self.spec = SnapshotRestoreSpec(
            snapshot_id="source-origin",
            extra_config={
                "fileBackedTmpfsMounts": [{"target": "/upper", "sizeMB": 64}]
            },
            idle_timeout=480,
            schedule_timeout=35,
            timeout=95,
            name="original",
            command_cwd="/work",
            port_forwardings=(8080,),
            mounts=(),
            reverse_tunnel=HttpReverseTunnel(target="http://127.0.0.1:8765"),
            detached=True,
            failover=True,
            node_id="source-node",
            network_policy=None,
        )
        self.native = MagicMock(id="source", newly_created=True)
        self.source = adx._Session(self.native, self.spec, self.backend._connection)
        self.capture = patch.object(
            self.source, "create_snapshot", return_value=SnapshotInfo("temporary")
        ).start()
        self.restore = patch.object(adx.AdxBackend, "_restore_snapshot").start()
        self.delete = patch.object(adx.AdxBackend, "_delete_snapshot").start()
        self.addCleanup(patch.stopall)

    def live_child(self, identity):
        return adx._Session(
            MagicMock(id=identity, newly_created=True), self.spec,
            self.backend._connection,
        )

    def busy_snapshot(self):
        return BackendOperationError(
            "snapshot is in use", code="SNAPSHOT_IN_USE", status_code=409,
            outcome="not_started", retry="never",
        )

    def test_busy_snapshot_is_retained_until_last_owned_child_is_deleted(self):
        first, second = self.live_child("first"), self.live_child("second")
        self.restore.side_effect = [first, second]
        self.delete.side_effect = [self.busy_snapshot(), None]
        outcome = self.source.fork(2, None, 300, None)
        self.assertIsNone(outcome.cleanup_error)
        self.assertEqual(first.fork_snapshot_id, "temporary")
        self.assertEqual(second.fork_snapshot_id, "temporary")
        with patch.object(adx.adx_environment.Environment, "delete") as delete:
            first.close()
            self.assertEqual(self.delete.call_count, 1)
            first.terminate()
            self.assertEqual(self.delete.call_count, 1)
            second.terminate()
            self.assertEqual(self.delete.call_count, 2)
            second.terminate()
            self.assertEqual(self.delete.call_count, 2)
        self.assertEqual(delete.call_count, 2)

    def test_failed_child_delete_does_not_release_its_snapshot_reference(self):
        first = self.live_child("first")
        self.restore.return_value = first
        self.delete.side_effect = [self.busy_snapshot(), None]
        self.assertIsNone(self.source.fork(1, None, 300, None).cleanup_error)
        with patch.object(adx.adx_environment.Environment, "delete") as delete:
            delete.side_effect = [RuntimeError("delete failed"), None]
            with self.assertRaises(BackendOperationError):
                first.terminate()
            self.assertEqual(self.delete.call_count, 1)
            first.terminate()
        self.assertEqual(self.delete.call_count, 2)

    def test_last_child_snapshot_cleanup_failure_remains_retryable(self):
        first = self.live_child("first")
        self.restore.return_value = first
        failure = BackendOperationError("delete unavailable", status_code=503)
        self.delete.side_effect = [self.busy_snapshot(), failure, None]
        self.source.fork(1, None, 300, None)
        with patch.object(adx.adx_environment.Environment, "delete") as delete:
            with self.assertRaises(ForkCleanupError) as raised:
                first.terminate()
            self.assertEqual(raised.exception.snapshot_id, "temporary")
            self.assertIs(raised.exception.cleanup_error, failure)
            first.terminate()
        delete.assert_called_once()
        self.assertEqual(self.delete.call_count, 3)

    def test_unrelated_snapshot_conflict_is_not_treated_as_retained_reference(self):
        failure = BackendOperationError("conflict", status_code=409,
                                        code="CONFLICT", outcome="not_started")
        self.restore.return_value = self.live_child("first")
        self.delete.side_effect = failure
        self.assertIs(self.source.fork(1, None, 300, None).cleanup_error, failure)

    def test_one_capture_partial_results_inheritance_and_snapshot_cleanup(self):
        first, third = child("first"), child("third")
        failure = BackendOperationError("capacity", outcome="not_started")
        self.restore.side_effect = [first, failure, third]
        outcome = self.source.fork(3, None, 50, None)
        self.assertEqual(outcome.results, [first, failure, third])
        self.assertIsNone(outcome.cleanup_error)
        self.capture.assert_called_once_with(None, 50)
        self.assertEqual(self.restore.call_count, 3)
        specs = [call.args[0] for call in self.restore.call_args_list]
        self.assertEqual({spec.snapshot_id for spec in specs}, {"temporary"})
        restored = specs[0]
        self.assertEqual(restored.idle_timeout, 480)
        self.assertEqual(restored.timeout, 95)
        self.assertEqual(restored.command_cwd, "/work")
        self.assertEqual(restored.port_forwardings, (8080,))
        self.assertEqual(restored.extra_config, self.spec.extra_config)
        self.assertIs(restored.reverse_tunnel, self.spec.reverse_tunnel)
        self.assertTrue(restored.failover)
        self.assertFalse(restored.detached)
        self.assertIsNone(restored.name)
        self.assertIsNone(restored.node_id)
        self.assertIsNone(restored.network_policy)
        self.assertFalse(hasattr(restored, "cpu"))
        self.delete.assert_called_once_with("temporary", self.backend._connection)
        self.native.close.assert_not_called()
        first.terminate.assert_not_called()
        third.terminate.assert_not_called()

    def test_all_children_can_fail_and_snapshot_is_still_deleted(self):
        failure = BackendOperationError("no capacity")
        self.restore.side_effect = failure
        outcome = self.source.fork(2, 120, 60, 120)
        self.assertEqual(outcome.results, [failure, failure])
        self.assertEqual(self.restore.call_args.args[0].idle_timeout, 120)
        self.assertEqual(self.restore.call_args.args[0].timeout, 120)
        self.delete.assert_called_once()

    def test_acknowledged_policy_update_is_sent_in_each_child_create(self):
        policy = NetworkPolicy.block()
        self.source.update_network_policy(policy)
        self.restore.side_effect = [child("first"), child("second")]
        self.source.fork(2, None, 60, None)
        for call in self.restore.call_args_list:
            self.assertIs(call.args[0].network_policy, policy)

    def test_unconfirmed_policy_update_blocks_fork_until_successful_reapply(self):
        self.native.update_network_policy.side_effect = RuntimeError("lost response")
        with self.assertRaises(BackendOperationError):
            self.source.update_network_policy(NetworkPolicy.block())
        with self.assertRaises(BackendOperationError) as raised:
            self.source.fork(2, None, 60, None)
        self.assertEqual(raised.exception.code, "NETWORK_POLICY_UNCONFIRMED")
        self.assertEqual(raised.exception.outcome, "not_started")
        self.capture.assert_not_called()
        self.native.update_network_policy.side_effect = None
        self.source.update_network_policy(None)
        self.restore.return_value = child("after-reapply")
        self.source.fork(1, None, 60, None)
        self.assertIsNone(self.restore.call_args.args[0].network_policy)

    def test_interrupted_policy_update_blocks_fork_before_capture(self):
        self.native.update_network_policy.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.source.update_network_policy(NetworkPolicy.block())
        with self.assertRaises(BackendOperationError):
            self.source.fork(1, None, 60, None)
        self.capture.assert_not_called()

    def test_capture_failure_raises_without_creates_or_unknown_snapshot_deletion(self):
        failure = BackendOperationError(
            "capture unknown", outcome="unknown", request_id="r"
        )
        self.capture.side_effect = failure
        with self.assertRaises(BackendOperationError) as raised:
            self.source.fork(3, None, 60, None)
        self.assertIs(raised.exception, failure)
        self.restore.assert_not_called()
        self.delete.assert_not_called()

    def test_unknown_child_is_not_replayed_and_successful_sibling_survives(self):
        failure = BackendOperationError(
            "create unknown",
            outcome="unknown",
            request_id="r",
            environment_id="uncertain",
        )
        success = child("success")
        self.restore.side_effect = [failure, success]
        outcome = self.source.fork(2, None, 60, None)
        self.assertIs(outcome.results[0], failure)
        self.assertIs(outcome.results[1], success)
        self.assertEqual(self.restore.call_count, 2)
        success.terminate.assert_not_called()
        self.delete.assert_called_once()

    def test_delete_failure_is_reported_without_killing_successes(self):
        success = child("success")
        self.restore.return_value = success
        failure = BackendOperationError("delete unavailable", request_id="cleanup")
        self.delete.side_effect = failure
        with self.assertLogs(adx.logger, level="WARNING"):
            outcome = self.source.fork(1, None, 60, None)
        self.assertIs(outcome.cleanup_error, failure)
        self.assertEqual(outcome.snapshot_id, "temporary")
        self.assertEqual(outcome.results, [success])
        success.terminate.assert_not_called()

    def test_interrupt_releases_returned_children_and_snapshot(self):
        success = child("success")
        self.restore.side_effect = [success, KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.source.fork(2, None, 60, None)
        success.terminate.assert_called_once()
        self.delete.assert_called_once()
        self.native.close.assert_not_called()

    def test_closed_session_does_not_capture(self):
        self.source._closed = True
        with self.assertRaises(BackendOperationError):
            self.source.fork(1, None, 60, None)
        self.capture.assert_not_called()

    def test_native_wire_uses_existing_snapshot_and_delete_routes(self):
        # Exercise capture/delete through the pinned native HTTP transport.
        patch.stopall()
        self.native = adx._OwnedSandbox.__new__(adx._OwnedSandbox)
        self.native._closed = False
        self.native._newly_created = True
        self.native._tunnel_client = None
        self.native._sid = "source"
        from adx_environment._transport import EnvironmentClient

        self.native._client = EnvironmentClient(connection=self.backend._connection)
        self.native._commands = MagicMock()
        self.native._files = MagicMock()
        self.source = adx._Session(self.native, self.spec, self.backend._connection)
        requests = []

        def respond(_client, request, **kwargs):
            requests.append(request)
            return httpx.Response(
                200,
                json={"code": 200, "data": {"snapshotId": "temporary", "names": []}},
                request=request,
            )

        try:
            with (
                patch.object(httpx.Client, "send", autospec=True, side_effect=respond),
                patch.object(
                    adx.AdxBackend,
                    "_restore_snapshot",
                    side_effect=[child("a"), child("b")],
                ),
            ):
                outcome = self.source.fork(2, None, 40, None)
            self.assertEqual(len(outcome.results), 2)
            self.assertEqual([r.method for r in requests], ["POST", "DELETE"])
            self.assertEqual(
                requests[0].url.path,
                "/api/environment/v1/environments/source/snapshots",
            )
            self.assertEqual(json.loads(requests[0].content), {"timeoutSeconds": 40})
            self.assertEqual(
                requests[1].url.path, "/api/environment/v1/snapshots/temporary"
            )
        finally:
            self.native._client.close()


if __name__ == "__main__":
    unittest.main()
