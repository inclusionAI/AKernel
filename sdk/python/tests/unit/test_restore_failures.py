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

"""Regressions for native bootstrap ownership and restore budget validation."""

import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

import adx_environment

from akernel_sdk import BackendOperationError, HttpReverseTunnel, Sandbox
from akernel_sdk import sandbox as public
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends import adx
from akernel_sdk._backends.base import BackendConfig, SnapshotRestoreSpec
from tests.unit.test_adx_backend import _spec


def restore_spec(**changes):
    return replace(
        SnapshotRestoreSpec(
            snapshot_id="saved",
            idle_timeout=300,
            schedule_timeout=30,
            timeout=90,
            name=None,
            command_cwd=None,
            port_forwardings=(),
            mounts=(),
            reverse_tunnel=HttpReverseTunnel(target="http://127.0.0.1:8765"),
            detached=False,
            failover=False,
            node_id=None,
            network_policy=None,
        ),
        **changes,
    )


class RestoreFailureTest(unittest.TestCase):
    def setUp(self):
        self.backend = adx.AdxBackend(
            BackendConfig(
                Endpoint("api.example", 443, "https", True),
                Endpoint("gateway.example", 443, "https", True),
                "test-only",
                True,
            )
        )

        def capture_create(native, _body):
            self.native = native
            return {"environmentId": "known-child"}

        self.capture_create = capture_create

    def test_real_constructor_interrupt_rolls_back_known_child_and_closes_client(self):
        failure = KeyboardInterrupt("during tunnel bootstrap")
        with (
            patch.object(adx._OwnedSandbox, "_create", self.capture_create),
            patch("adx_environment.tunnel_client.TunnelClient") as tunnel,
            patch("adx_environment._transport.EnvironmentClient.delete") as delete,
        ):
            tunnel.return_value.start.side_effect = failure
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.backend.from_snapshot(restore_spec())
            self.assertIs(raised.exception, failure)
            self.assertEqual(failure.environment_id, "known-child")
            delete.assert_called_once_with("known-child")
            tunnel.return_value.stop.assert_called_once()
            self.assertTrue(self.native._closed)
            self.assertTrue(self.native._client._http._closed)

    def test_constructor_interrupt_keeps_cleanup_error_and_known_id(self):
        failure = KeyboardInterrupt("during tunnel bootstrap")
        cleanup = RuntimeError("delete unavailable")
        with (
            patch.object(adx._OwnedSandbox, "_create", self.capture_create),
            patch("adx_environment.tunnel_client.TunnelClient") as tunnel,
            patch(
                "adx_environment._transport.EnvironmentClient.delete",
                side_effect=cleanup,
            ),
            self.assertLogs(adx.logger, level="WARNING"),
        ):
            tunnel.return_value.start.side_effect = failure
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.backend.from_snapshot(restore_spec())
        self.assertIs(raised.exception, failure)
        self.assertEqual(failure.environment_id, "known-child")
        self.assertIn(cleanup, failure.cleanup_errors)

    def test_fresh_create_tunnel_interrupt_uses_same_ownership_guard(self):
        with (
            patch.object(adx._OwnedSandbox, "_create", self.capture_create),
            patch("adx_environment.tunnel_client.TunnelClient") as tunnel,
            patch("adx_environment._transport.EnvironmentClient.delete") as delete,
        ):
            tunnel.return_value.start.side_effect = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.backend.create(_spec(reverse_tunnel=restore_spec().reverse_tunnel))
        self.assertEqual(raised.exception.environment_id, "known-child")
        delete.assert_called_once_with("known-child")

    def test_route_probe_interrupt_cleans_initialized_session(self):
        native = MagicMock(id="known-child")
        native.commands.list.side_effect = KeyboardInterrupt()
        with (
            patch.object(adx._OwnedSandbox, "create", return_value=native),
            patch.object(adx_environment.Environment, "delete") as delete,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.backend.from_snapshot(restore_spec())
        self.assertEqual(raised.exception.environment_id, "known-child")
        native.close.assert_called_once()
        delete.assert_called_once_with(
            "known-child", connection=self.backend._connection
        )

    def test_native_tunnel_failure_and_failed_rollback_preserve_identity(self):
        cleanup = RuntimeError("delete unavailable")
        with (
            patch.object(adx._OwnedSandbox, "_create", self.capture_create),
            patch("adx_environment.tunnel_client.TunnelClient") as tunnel,
            patch(
                "adx_environment._transport.EnvironmentClient.delete",
                side_effect=cleanup,
            ) as delete,
            self.assertLogs(level="WARNING"),
        ):
            tunnel.return_value.start.return_value = False
            with self.assertRaises(BackendOperationError) as raised:
                self.backend.from_snapshot(restore_spec())
        self.assertEqual(raised.exception.environment_id, "known-child")
        self.assertIn(cleanup, raised.exception.cleanup_errors)
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertEqual(delete.call_count, 2)  # native rollback + fresh-client retry

    def test_unknown_create_does_not_delete_an_unconfirmed_lookup_name(self):
        failure = adx_environment.EnvironmentError("unknown create")
        failure.environment_id = "lookup-name"
        failure.outcome = "unknown"
        failure.request_id = "same-request"
        with (
            patch.object(adx._OwnedSandbox, "_create", side_effect=failure),
            patch("adx_environment._transport.EnvironmentClient.delete") as delete,
        ):
            with self.assertRaises(BackendOperationError) as raised:
                self.backend.from_snapshot(restore_spec(reverse_tunnel=None))
        delete.assert_not_called()
        self.assertEqual(raised.exception.environment_id, "lookup-name")
        self.assertEqual(raised.exception.outcome, "unknown")
        self.assertEqual(raised.exception.request_id, "same-request")

    def test_public_binding_and_delete_failure_retain_both_errors_and_id(self):
        session = MagicMock(id="known-child")
        failure = BackendOperationError("metadata unavailable", request_id="metadata")
        cleanup = BackendOperationError("delete unavailable", request_id="rollback")
        session.get_info.side_effect = failure
        session.terminate.side_effect = cleanup
        backend = MagicMock()
        backend.from_snapshot.return_value = session
        with (
            patch.object(public, "load_backend", return_value=backend),
            self.assertLogs(public.logger, level="WARNING"),
        ):
            with self.assertRaises(BackendOperationError) as raised:
                Sandbox.from_snapshot("saved")
        self.assertIs(raised.exception, failure)
        self.assertEqual(failure.environment_id, "known-child")
        self.assertEqual(failure.cleanup_errors, (cleanup,))
        self.assertEqual(failure.request_id, "metadata")
        self.assertEqual(failure.cleanup_errors[0].request_id, "rollback")

    def test_fresh_public_binding_interrupt_preserves_all_cleanup_errors(self):
        backend = MagicMock()
        session = backend.create.return_value
        session.id = "known-child"
        failure = KeyboardInterrupt()
        cleanup, close = RuntimeError("delete"), RuntimeError("close")
        session.terminate.side_effect = cleanup
        session.close.side_effect = close
        with (
            patch.object(public, "load_backend", return_value=backend),
            patch.object(public, "Filesystem", side_effect=failure),
            self.assertLogs(public.logger, level="WARNING"),
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                Sandbox()
        self.assertIs(raised.exception, failure)
        self.assertEqual(failure.environment_id, "known-child")
        self.assertEqual(failure.cleanup_errors, (cleanup, close))

    def test_session_initialization_failure_cleans_created_sandbox(self):
        native = MagicMock(id="known-child")
        failure = KeyboardInterrupt("binding session")
        with (
            patch.object(adx._OwnedSandbox, "create", return_value=native),
            patch.object(adx, "_Session", side_effect=failure),
            patch.object(adx_environment.Environment, "delete") as delete,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.backend.from_snapshot(restore_spec())
        self.assertIs(raised.exception, failure)
        self.assertEqual(failure.environment_id, "known-child")
        native.close.assert_called_once()
        delete.assert_called_once_with(
            "known-child", connection=self.backend._connection
        )

    def test_session_initialization_keeps_close_and_delete_failures(self):
        native = MagicMock(id="known-child")
        failure = RuntimeError("binding session")
        close_error, delete_error = RuntimeError("close"), RuntimeError("delete")
        native.close.side_effect = close_error
        with (
            patch.object(adx._OwnedSandbox, "create", return_value=native),
            patch.object(adx, "_Session", side_effect=failure),
            patch.object(
                adx_environment.Environment, "delete", side_effect=delete_error
            ),
            self.assertLogs(adx.logger, level="WARNING"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                self.backend.from_snapshot(restore_spec())
        self.assertIs(raised.exception, failure)
        self.assertEqual(failure.environment_id, "known-child")
        self.assertEqual(failure.cleanup_errors, (close_error, delete_error))

    def test_invalid_restore_budget_fails_before_native_create(self):
        for timeout in (1, 20, 59, 60, 70, 89):
            with (
                self.subTest(timeout=timeout),
                patch.object(adx._OwnedSandbox, "create") as create,
                self.assertRaises(ValueError),
            ):
                self.backend.from_snapshot(restore_spec(timeout=timeout))
            create.assert_not_called()

    def test_invalid_fork_budget_fails_before_capture(self):
        native = MagicMock(id="source")
        source = adx._Session(native, restore_spec(), self.backend._connection)
        with patch.object(source, "create_snapshot") as capture:
            for timeout in (1, 20, 70, 89):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    source.fork(3, None, 300, timeout)
        capture.assert_not_called()

    def test_accepted_budget_reaches_real_native_constructor_without_expansion(self):
        bodies = []

        def create(_native, body):
            bodies.append(body)
            return {"environmentId": "known-child"}

        with (
            patch.object(adx._OwnedSandbox, "_create", create),
            patch("adx_environment.commands.Commands.list", return_value=[]),
            patch("adx_environment._transport.EnvironmentClient.delete"),
        ):
            for timeout in (90, 120):
                session = self.backend.from_snapshot(
                    restore_spec(timeout=timeout, reverse_tunnel=None)
                )
                session.terminate()
                self.assertEqual(bodies[-1]["createTimeoutSeconds"], timeout)


if __name__ == "__main__":
    unittest.main()
