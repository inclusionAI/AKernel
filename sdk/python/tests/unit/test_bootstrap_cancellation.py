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

"""Preserve cancellation and known resources before restore returns a session."""

import unittest
from unittest.mock import MagicMock, patch

from akernel_sdk import BackendOperationError
from akernel_sdk._backends import adx
from tests.unit import test_review_regressions as previous
from tests.unit.test_restore_failures import restore_spec


class BootstrapCancellationTest(unittest.TestCase):
    def setUp(self):
        previous.ForkCancellationTest.setUp(self)

    def busy(self):
        return BackendOperationError(
            "snapshot busy", code="SNAPSHOT_IN_USE", outcome="not_started"
        )

    def rollback(self, primary, native, connection, session):
        try:
            adx._rollback_created(primary, native, connection, session)
        except BaseException as received:
            self.fail(
                f"cleanup replaced original failure with {type(received).__name__}"
            )

    def test_readiness_and_rollback_interruptions_preserve_primary_and_retry_handle(
        self,
    ):
        for primary_type, cleanup_type in (
            (KeyboardInterrupt, KeyboardInterrupt),
            (SystemExit, KeyboardInterrupt),
            (KeyboardInterrupt, SystemExit),
        ):
            with self.subTest(primary=primary_type, cleanup=cleanup_type):
                primary, cleanup = primary_type("readiness"), cleanup_type("rollback")
                native = MagicMock(id="bootstrap-child", newly_created=True)
                native.commands.list.side_effect = primary
                with (
                    patch.object(adx._OwnedSandbox, "create", return_value=native),
                    patch.object(
                        adx.adx_environment.Environment,
                        "delete",
                        side_effect=[cleanup, None],
                    ) as delete,
                    patch.object(
                        adx.AdxBackend, "_delete_snapshot", side_effect=self.busy()
                    ),
                ):
                    with self.assertRaises(BaseException) as raised:
                        self.public.fork()
                    self.assertIs(raised.exception, primary)
                    self.assertEqual(primary.environment_id, native.id)
                    self.assertIn(cleanup, primary.cleanup_errors)
                    self.assertEqual(primary.created_environment_ids, (native.id,))
                    self.assertEqual(primary.pending_environment_ids, (native.id,))
                    self.assertEqual(primary.snapshot_id, "temporary")
                    self.assertEqual(len(primary.pending_sandboxes), 1)
                    self.assertEqual(delete.call_count, 1)
                    primary.pending_sandboxes[0].kill()
                    self.assertEqual(delete.call_count, 2)
                    native.close.assert_called_once()
                    self.assertFalse(hasattr(primary, "_fork_pending_sessions"))

    def test_native_binding_interruption_before_session_returns_retains_cleanup_handle(
        self,
    ):
        primary, cleanup = KeyboardInterrupt("session binding"), SystemExit("delete")
        native = MagicMock(id="unbound-child", newly_created=True)
        with (
            patch.object(adx._OwnedSandbox, "create", return_value=native),
            patch.object(adx._Session, "__init__", side_effect=primary),
            patch.object(
                adx.adx_environment.Environment, "delete", side_effect=[cleanup, None]
            ) as delete,
            patch.object(adx.AdxBackend, "_delete_snapshot", side_effect=self.busy()),
        ):
            with self.assertRaises(BaseException) as raised:
                self.public.fork()
            self.assertIs(raised.exception, primary)
            self.assertEqual(primary.environment_id, native.id)
            self.assertEqual(primary.pending_environment_ids, (native.id,))
            self.assertIn(cleanup, primary.cleanup_errors)
            primary.pending_sandboxes[0].kill()
            self.assertEqual(delete.call_count, 2)
            native.close.assert_called_once()

    def test_bootstrap_pending_child_merges_with_earlier_failed_cleanup(self):
        primary, cleanup = KeyboardInterrupt("readiness"), KeyboardInterrupt("rollback")
        earlier = previous.session("earlier")
        earlier_error = SystemExit("earlier cleanup")
        earlier.rollback_creation.side_effect = earlier_error
        native = MagicMock(id="bootstrap-child", newly_created=True)
        native.commands.list.side_effect = primary
        restore = adx.AdxBackend._restore_snapshot
        attempts = 0

        def restore_second(spec, connection):
            nonlocal attempts
            attempts += 1
            return earlier if attempts == 1 else restore(spec, connection)

        with (
            patch.object(
                adx.AdxBackend, "_restore_snapshot", side_effect=restore_second
            ),
            patch.object(adx._OwnedSandbox, "create", return_value=native),
            patch.object(
                adx.adx_environment.Environment, "delete", side_effect=cleanup
            ) as delete,
            patch.object(adx.AdxBackend, "_delete_snapshot", side_effect=self.busy()),
            self.assertRaises(BaseException) as raised,
        ):
            self.public.fork(count=2)
        self.assertIs(raised.exception, primary)
        self.assertEqual(set(primary.pending_environment_ids), {earlier.id, native.id})
        self.assertEqual(set(primary.created_environment_ids), {earlier.id, native.id})
        self.assertEqual(
            {handle.id for handle in primary.pending_sandboxes}, {earlier.id, native.id}
        )
        self.assertIn(cleanup, primary.cleanup_errors)
        self.assertIn(earlier_error, primary.cleanup_errors)
        delete.assert_called_once()
        earlier.rollback_creation.assert_called_once()

    def test_ordinary_rollback_error_control_preserves_primary_and_nested_cause(self):
        primary, cleanup = KeyboardInterrupt("readiness"), RuntimeError("delete")
        native = MagicMock(id="bootstrap-child", newly_created=True)
        native.commands.list.side_effect = primary
        with (
            patch.object(adx._OwnedSandbox, "create", return_value=native),
            patch.object(
                adx.adx_environment.Environment, "delete", side_effect=cleanup
            ),
            self.assertRaises(BaseException) as raised,
        ):
            self.adapter.from_snapshot(restore_spec())
        self.assertIs(raised.exception, primary)
        self.assertEqual(primary.environment_id, native.id)
        self.assertIs(primary.cleanup_errors[0].__cause__, cleanup)

    def test_no_session_cleanup_interruptions_are_retained_and_delete_still_attempted(
        self,
    ):
        primary = KeyboardInterrupt("binding")
        close_error, delete_error = (
            SystemExit("local close"),
            KeyboardInterrupt("delete"),
        )
        native = MagicMock(id="unbound-child", newly_created=True)
        native.close.side_effect = close_error
        with patch.object(
            adx.adx_environment.Environment, "delete", side_effect=delete_error
        ) as delete:
            self.rollback(primary, native, self.adapter._connection, None)
        self.assertEqual(primary.environment_id, native.id)
        self.assertEqual(primary.cleanup_errors, (close_error, delete_error))
        delete.assert_called_once_with(native.id, connection=self.adapter._connection)
        self.assertEqual(primary._fork_pending_sessions[0].id, native.id)

    def test_no_session_confirmed_delete_is_not_marked_pending_after_close_interrupt(
        self,
    ):
        primary, cleanup = KeyboardInterrupt("binding"), SystemExit("local close")
        native = MagicMock(id="unbound-child", newly_created=True)
        native.close.side_effect = cleanup
        with patch.object(adx.adx_environment.Environment, "delete") as delete:
            self.rollback(primary, native, self.adapter._connection, None)
        self.assertEqual(primary.environment_id, native.id)
        self.assertEqual(primary.cleanup_errors, (cleanup,))
        self.assertFalse(getattr(primary, "_fork_pending_sessions", ()))
        delete.assert_called_once()

    def test_no_session_unconfirmed_ownership_never_automatically_deletes(self):
        for ownership in (False, "missing", "true"):
            with self.subTest(ownership=ownership):
                primary, cleanup = KeyboardInterrupt("binding"), SystemExit("close")
                native = MagicMock(id="unconfirmed-child", newly_created=ownership)
                if ownership == "missing":
                    del native.newly_created
                native.close.side_effect = cleanup
                with patch.object(adx.adx_environment.Environment, "delete") as delete:
                    self.rollback(primary, native, self.adapter._connection, None)
                    self.assertEqual(primary.cleanup_errors, (cleanup,))
                    self.assertEqual(primary.environment_id, native.id)
                    delete.assert_not_called()
                    native.close.side_effect = None
                    primary._fork_pending_sessions[0].terminate()
                    delete.assert_called_once_with(
                        native.id, connection=self.adapter._connection
                    )

    def test_ordinary_primary_is_not_replaced_by_rollback_cancellation(self):
        primary, cleanup = RuntimeError("binding"), KeyboardInterrupt("rollback")
        child = previous.session("known-child")
        child.rollback_creation.side_effect = cleanup
        self.rollback(primary, MagicMock(id=child.id), None, child)
        self.assertEqual(primary.environment_id, child.id)
        self.assertEqual(primary.cleanup_errors, (cleanup,))
        self.assertIs(primary._fork_pending_sessions[0], child)

    def test_native_tunnel_constructor_preserves_second_interruption_and_known_child(
        self,
    ):
        primary, cleanup = KeyboardInterrupt("tunnel start"), SystemExit("delete")
        natives = []

        def capture(native, _body):
            native._newly_created = True
            natives.append(native)
            return {"environmentId": "native-constructor-child", "newlyCreated": True}

        with (
            patch.object(adx._OwnedSandbox, "_create", new=capture),
            patch("adx_environment.tunnel_client.TunnelClient") as tunnel,
            patch.object(
                adx.adx_environment.Environment, "delete", side_effect=[cleanup, None]
            ) as delete,
        ):
            tunnel.return_value.start.side_effect = primary
            with self.assertRaises(BaseException) as raised:
                self.adapter.from_snapshot(restore_spec())
            self.assertIs(raised.exception, primary)
            self.assertEqual(primary.environment_id, "native-constructor-child")
            self.assertIn(cleanup, primary.cleanup_errors)
            self.assertTrue(natives[0]._client._http._closed)
            primary._fork_pending_sessions[0].terminate()
            self.assertEqual(delete.call_count, 2)

    def test_native_constructor_close_interruption_still_closes_client_and_rolls_back(
        self,
    ):
        primary, cleanup = KeyboardInterrupt("tunnel start"), KeyboardInterrupt("stop")
        natives = []

        def capture(native, _body):
            native._newly_created = True
            natives.append(native)
            return {"environmentId": "native-constructor-child", "newlyCreated": True}

        with (
            patch.object(adx._OwnedSandbox, "_create", new=capture),
            patch("adx_environment.tunnel_client.TunnelClient") as tunnel,
            patch.object(adx.adx_environment.Environment, "delete") as delete,
        ):
            tunnel.return_value.start.side_effect = primary
            tunnel.return_value.stop.side_effect = cleanup
            with self.assertRaises(BaseException) as raised:
                self.adapter.from_snapshot(restore_spec())
            self.assertIs(raised.exception, primary)
            self.assertEqual(primary.environment_id, "native-constructor-child")
            self.assertIn(cleanup, primary.cleanup_errors)
            self.assertTrue(natives[0]._client._http._closed)
            self.assertFalse(getattr(primary, "_fork_pending_sessions", ()))
            delete.assert_called_once()
