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

"""Fork reconciliation retains complete errors and unconfirmed remote users."""

import unittest
from unittest.mock import MagicMock, patch

from akernel_sdk import BackendOperationError, ForkCleanupError
from akernel_sdk._backends import adx
from akernel_sdk._backends.base import ForkOutcome
from tests.unit import test_review_regressions as previous
from tests.unit.test_adx_backend import _spec
from tests.unit.test_review_regressions import session


class ForkReconciliationTest(unittest.TestCase):
    def setUp(self):
        previous.ForkCancellationTest.setUp(self)

    def busy(self):
        return BackendOperationError(
            "snapshot busy", code="SNAPSHOT_IN_USE", outcome="not_started"
        )

    def unconfirmed_child(self, ownership=False):
        native = MagicMock(id="unconfirmed", newly_created=ownership)
        if ownership == "missing":
            del native.newly_created
        return native, adx._Session(native, _spec(), self.adapter._connection)

    def test_later_creation_error_is_retained_before_public_loop_reaches_it(self):
        first = session("first")
        interruption = KeyboardInterrupt("binding first")
        first.get_info.side_effect = interruption
        rollback = RuntimeError("later rollback failed")
        later = BackendOperationError(
            "later create uncertain",
            environment_id="later-child",
            operation_id="restore-later",
            request_id="request-later",
            outcome="unknown",
            cleanup_errors=(rollback,),
        )
        with (
            patch.object(
                adx.AdxBackend, "_restore_snapshot", side_effect=[first, later]
            ),
            patch.object(adx.AdxBackend, "_delete_snapshot", side_effect=self.busy()),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.public.fork(count=2)
        self.assertIs(raised.exception, interruption)
        self.assertEqual(interruption.creation_errors, (later,))
        retained = interruption.creation_errors[0]
        self.assertEqual(retained.environment_id, "later-child")
        self.assertEqual(retained.operation_id, "restore-later")
        self.assertEqual(retained.request_id, "request-later")
        self.assertEqual(retained.outcome, "unknown")
        self.assertIs(retained.cleanup_errors[0], rollback)
        self.assertNotIn("later-child", interruption.created_environment_ids)
        self.assertNotIn("later-child", interruption.pending_environment_ids)
        self.assertTrue(
            all(h.id != "later-child" for h in interruption.pending_sandboxes)
        )

    def test_complete_creation_errors_merge_with_earlier_binding_errors(self):
        failed, interrupted = session("bind-failed"), session("interrupted")
        binding = RuntimeError("earlier facade binding failed")
        failed.get_info.side_effect = binding
        interruption = SystemExit("bind interrupted")
        interrupted.get_info.side_effect = interruption
        later = BackendOperationError("later failed", environment_id="later")
        with (
            patch.object(
                adx.AdxBackend,
                "_restore_snapshot",
                side_effect=[failed, interrupted, later],
            ),
            patch.object(adx.AdxBackend, "_delete_snapshot"),
            self.assertRaises(SystemExit) as raised,
        ):
            self.public.fork(count=3)
        self.assertIs(raised.exception, interruption)
        self.assertEqual(
            set(map(id, interruption.creation_errors)), {id(binding), id(later)}
        )
        self.assertEqual(len(interruption.creation_errors), 2)

    def test_already_visited_creation_error_is_not_duplicated(self):
        interrupted = session("interrupted")
        interruption = KeyboardInterrupt()
        interrupted.get_info.side_effect = interruption
        early, later = RuntimeError("early"), RuntimeError("later")
        with (
            patch.object(
                adx.AdxBackend,
                "_restore_snapshot",
                side_effect=[early, interrupted, later],
            ),
            patch.object(adx.AdxBackend, "_delete_snapshot"),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.public.fork(count=3)
        self.assertEqual(interruption.creation_errors, (early, later))

    def test_local_only_binding_rollback_retains_child_without_automatic_delete(self):
        for ownership in (False, "missing", "true"):
            with self.subTest(ownership=ownership):
                native, child = self.unconfirmed_child(ownership)
                interruption = KeyboardInterrupt("bind unconfirmed")
                native.get_info.side_effect = interruption
                with (
                    patch.object(
                        adx.AdxBackend, "_restore_snapshot", return_value=child
                    ),
                    patch.object(
                        adx.AdxBackend,
                        "_delete_snapshot",
                        side_effect=[self.busy(), self.busy(), None],
                    ),
                    patch.object(adx.adx_environment.Environment, "delete") as delete,
                ):
                    with self.assertRaises(KeyboardInterrupt) as raised:
                        self.public.fork()
                    self.assertIs(raised.exception, interruption)
                    self.assertEqual(interruption.pending_environment_ids, (child.id,))
                    self.assertEqual(len(interruption.pending_sandboxes), 1)
                    native.close.assert_called_once()
                    delete.assert_not_called()
                    interruption.pending_sandboxes[0].kill()
                    delete.assert_called_once_with(
                        child.id, connection=self.adapter._connection
                    )

    def test_normal_binding_error_also_retains_unconfirmed_child(self):
        native, child = self.unconfirmed_child()
        binding = RuntimeError("bind error")
        native.get_info.side_effect = binding
        with (
            patch.object(adx.AdxBackend, "_restore_snapshot", return_value=child),
            patch.object(
                adx.AdxBackend, "_delete_snapshot", side_effect=[self.busy(), None]
            ),
            patch.object(adx.adx_environment.Environment, "delete") as delete,
        ):
            with self.assertRaises(ForkCleanupError) as raised:
                self.public.fork()
            error = raised.exception
            self.assertIs(error.results[0].__cause__, binding)
            self.assertEqual(error.pending_sandboxes[0].id, child.id)
            delete.assert_not_called()
            error.pending_sandboxes[0].kill()

    def test_backend_restore_interruption_preserves_unconfirmed_child(self):
        native, child = self.unconfirmed_child()
        interruption = KeyboardInterrupt("later restore")
        with (
            patch.object(
                adx.AdxBackend, "_restore_snapshot", side_effect=[child, interruption]
            ),
            patch.object(
                adx.AdxBackend, "_delete_snapshot", side_effect=[self.busy(), None]
            ),
            patch.object(adx.adx_environment.Environment, "delete") as delete,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.public.fork(count=2)
            self.assertIs(raised.exception, interruption)
            self.assertEqual(interruption.pending_environment_ids, (child.id,))
            self.assertEqual(interruption.pending_sandboxes[0].id, child.id)
            native.close.assert_called_once()
            delete.assert_not_called()
            interruption.pending_sandboxes[0].kill()

    def test_rollback_acknowledges_remote_deletion_separately_from_local_close(self):
        for ownership in (True, False, "missing"):
            with self.subTest(ownership=ownership):
                native, child = self.unconfirmed_child(ownership)
                with patch.object(adx.adx_environment.Environment, "delete") as delete:
                    disposition = child.rollback_creation()
                self.assertIs(disposition, ownership is True)
                self.assertEqual(delete.call_count, int(ownership is True))
                native.close.assert_called_once()

    def test_legacy_none_rollback_is_not_a_remote_delete_acknowledgement(self):
        child = session("legacy-no-ack")
        child.rollback_creation.return_value = None
        interruption = KeyboardInterrupt("binding")
        child.get_info.side_effect = interruption
        self.source.fork = MagicMock(return_value=ForkOutcome("snapshot", [child]))
        with (
            patch.object(self.adapter, "delete_snapshot"),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.public.fork()
        self.assertEqual(interruption.pending_environment_ids, (child.id,))
        self.assertEqual(len(interruption.pending_sandboxes), 1)
        child.rollback_creation.assert_called_once()

    def test_unconfirmed_rollback_is_visible_even_if_snapshot_delete_succeeded(self):
        native, child = self.unconfirmed_child()
        native.get_info.side_effect = RuntimeError("binding failed")
        self.source.fork = MagicMock(return_value=ForkOutcome("snapshot", [child]))
        with (
            patch.object(adx.adx_environment.Environment, "delete") as delete,
            self.assertRaises(ForkCleanupError) as raised,
        ):
            self.public.fork()
        self.assertEqual(raised.exception.cleanup_error.code, "CLEANUP_UNCONFIRMED")
        self.assertEqual(raised.exception.pending_sandboxes[0].id, child.id)
        self.assertEqual(raised.exception.child_cleanup_errors, ())
        delete.assert_not_called()

    def test_unvisited_unconfirmed_child_is_retained_on_public_interruption(self):
        first = session("interrupted")
        interruption = KeyboardInterrupt("binding first")
        first.get_info.side_effect = interruption
        native, later = self.unconfirmed_child()
        self.source.fork = MagicMock(
            return_value=ForkOutcome("snapshot", [first, later])
        )
        with (
            patch.object(self.adapter, "delete_snapshot"),
            patch.object(adx.adx_environment.Environment, "delete") as delete,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.public.fork(count=2)
            self.assertIs(raised.exception, interruption)
            self.assertEqual(interruption.pending_environment_ids, (later.id,))
            native.close.assert_called_once()
            delete.assert_not_called()
            interruption.pending_sandboxes[0].kill()
            delete.assert_called_once_with(
                later.id, connection=self.adapter._connection
            )
