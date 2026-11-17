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

"""Public lifecycle budgets must survive native serialization unchanged."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import adx_environment

from akernel_sdk import BackendOperationError, Sandbox
from akernel_sdk import sandbox as public
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends import adx
from akernel_sdk._backends.base import BackendConfig, SandboxConnectSpec


class InitializationTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.bodies = []
        self.backend = adx.AdxBackend(
            BackendConfig(
                Endpoint("api.example", 443, "https", True),
                Endpoint("api.example", 443, "https", True),
                "test-only-secret",
            )
        )

        def create(native, body):
            self.bodies.append(body)
            native._newly_created = True
            return {"environmentId": f"child-{len(self.bodies)}", "newlyCreated": True}

        for patcher in (
            patch.object(public, "load_backend", return_value=self.backend),
            patch.object(adx._OwnedSandbox, "_create", create),
            patch("adx_environment.commands.Commands.list", return_value=[]),
            patch("adx_environment._transport.EnvironmentClient.delete"),
            patch.object(
                adx._OwnedSandbox,
                "get_info",
                return_value=SimpleNamespace(
                    id="child", state="running", cpu=1000, memory=4096, image=None
                ),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def entrypoints(self):
        return (
            (Sandbox, "create_timeout", {}),
            (Sandbox.create, "timeout", {}),
            (Sandbox.create, "timeout", {"snapshot_id": "saved"}),
            (Sandbox.from_snapshot, "timeout", {"snapshot_id": "saved"}),
        )

    def test_defaults_and_custom_values_reach_every_native_request(self):
        for factory, budget_name, options in self.entrypoints():
            for init, total in ((30, 90), (1, 61), (600, 660), (86400, 86460)):
                with self.subTest(factory=factory, options=options, init=init):
                    with factory(**options, init_timeout=init):
                        body = self.bodies[-1]
                        self.assertEqual(body["initCallTimeoutSeconds"], init)
                        self.assertEqual(body["createTimeoutSeconds"], total)
                    with factory(
                        **options, init_timeout=init, **{budget_name: total + 15}
                    ):
                        self.assertEqual(
                            self.bodies[-1]["createTimeoutSeconds"], total + 15
                        )

    def test_omitted_option_preserves_native_defaults(self):
        for factory, _, options in self.entrypoints():
            with self.subTest(factory=factory, options=options), factory(**options):
                self.assertEqual(self.bodies[-1]["initCallTimeoutSeconds"], 30)
                self.assertEqual(self.bodies[-1]["createTimeoutSeconds"], 90)

    def test_invalid_values_and_conflicts_fail_before_backend_loading(self):
        with patch.object(public, "load_backend") as load:
            for factory, budget_name, options in self.entrypoints():
                for invalid in (True, False, 0, -1, 1.5, "600", None, 86401):
                    with self.subTest(factory=factory, invalid=invalid):
                        with self.assertRaises((TypeError, ValueError)):
                            factory(**options, init_timeout=invalid)
                with self.assertRaisesRegex(ValueError, "init_timeout"):
                    factory(**options, init_timeout=600, **{budget_name: 600})
            load.assert_not_called()
        self.assertEqual(self.bodies, [])

    def test_native_environment_variable_cannot_replace_public_budget(self):
        with patch.dict("os.environ", {"ADX_ENVIRONMENT_CREATE_TIMEOUT": "9999"}):
            with Sandbox(init_timeout=600):
                self.assertEqual(self.bodies[-1]["createTimeoutSeconds"], 660)

    def test_explicit_minimum_and_nondefault_scheduling_are_preserved(self):
        for factory, budget_name, options in self.entrypoints():
            with factory(
                **options, schedule_timeout=45, init_timeout=120, **{budget_name: 195}
            ):
                self.assertEqual(self.bodies[-1]["scheduleTimeoutSeconds"], 45)
                self.assertEqual(self.bodies[-1]["createTimeoutSeconds"], 195)

    def test_protocol_overflow_fails_before_backend_loading(self):
        with patch.object(public, "load_backend") as load:
            for options in (
                {"schedule_timeout": (1 << 63) - 1},
                {"create_timeout": 1 << 63},
            ):
                with self.assertRaises(ValueError):
                    Sandbox(**options)
            load.assert_not_called()

    def test_budget_diagnostics_exclude_connection_and_request_secrets(self):
        with self.assertLogs(adx.logger, level="DEBUG") as logs:
            with Sandbox(
                init_timeout=600,
                env={"SECRET": "private-workload"},
                extra_config={"secret": "private-runtime"},
            ):
                pass
        text = "\n".join(logs.output)
        self.assertIn("init_timeout=600 create_timeout=660", text)
        for secret in ("test-only-secret", "private-workload", "private-runtime"):
            self.assertNotIn(secret, text)

    def test_failed_initialization_retains_server_phase_and_operation_context(self):
        failure = adx_environment.EnvironmentError("Execd readiness deadline exceeded")
        failure.failure = {"reason": "DeadlineExceeded", "message": "Execd readiness"}
        failure.request_id = "create-same-operation"
        failure.outcome = "terminal"
        with patch.object(adx._OwnedSandbox, "_create", side_effect=failure):
            with self.assertRaises(BackendOperationError) as raised:
                Sandbox(init_timeout=600)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(raised.exception.failure, failure.failure)
        self.assertEqual(raised.exception.request_id, failure.request_id)
        self.assertEqual(raised.exception.outcome, "terminal")

    def test_reused_handle_does_not_report_requested_policy_as_server_policy(self):
        native = MagicMock(id="reused", newly_created=False)
        # Reuse can retain a different server policy. The client configuration
        # remains useful for children but is not an acknowledgment from server.
        with Sandbox(init_timeout=600) as source:
            session = adx._Session(
                native, source._session._spec, self.backend._connection
            )
            with self.assertLogs(adx.logger, level="DEBUG") as logs:
                self.assertTrue(session.reload())
            self.assertIn(
                "init_timeout=None policy_source=server", "\n".join(logs.output)
            )
            self.assertNotIn("init_timeout=600", "\n".join(logs.output))
            native.reload.assert_called_once_with()

    def test_fork_inherits_or_overrides_without_changing_source(self):
        with (
            patch.object(
                adx_environment.Environment,
                "create_snapshot",
                return_value=SimpleNamespace(snapshot_id="saved", names=()),
            ),
            patch.object(adx_environment.Environment, "delete_snapshot"),
            Sandbox(init_timeout=600, failover=True) as source,
        ):
            for override, expected in ((None, 600), (45, 45), (None, 600)):
                with self.subTest(override=override):
                    children = source.fork(init_timeout=override)
                    self.assertEqual(
                        self.bodies[-1]["initCallTimeoutSeconds"], expected
                    )
                    self.assertEqual(
                        self.bodies[-1]["createTimeoutSeconds"], expected + 60
                    )
                    self.assertTrue(self.bodies[-1]["failover"])
                    children[0].kill()
            self.assertEqual(source._session._spec.init_timeout, 600)

    def test_fork_budget_conflict_precedes_snapshot_capture(self):
        with Sandbox(init_timeout=600) as source:
            with patch.object(source._session, "create_snapshot") as capture:
                with self.assertRaisesRegex(ValueError, "init_timeout"):
                    source.fork(restore_timeout=600)
                for invalid in (True, 0, 86401, "600"):
                    with self.assertRaises((TypeError, ValueError)):
                        source.fork(init_timeout=invalid)
                capture.assert_not_called()

    def test_connected_fork_uses_explicit_child_policy_or_default(self):
        native = MagicMock(id="existing")
        session = adx._Session(
            native,
            SandboxConnectSpec(
                "existing",
                60,
                None,
                (),
                None,
                False,
            ),
            self.backend._connection,
        )
        session.update_network_policy(None)
        with (
            patch.object(
                session,
                "create_snapshot",
                return_value=SimpleNamespace(snapshot_id="saved"),
            ),
            patch.object(adx_environment.Environment, "delete_snapshot"),
        ):
            for override, expected in ((None, 30), (600, 600)):
                result = session.fork(1, None, 300, None, init_timeout=override)
                self.assertEqual(self.bodies[-1]["initCallTimeoutSeconds"], expected)
                result.results[0].terminate()

    def test_reload_retains_policy_identity_and_never_recreates(self):
        with Sandbox(init_timeout=600, failover=True) as source:
            identity, commands, files = source.id, source.commands, source.files
            with patch.object(
                adx_environment.Environment, "reload", return_value=True
            ) as reload:
                self.assertTrue(source.reload())
                reload.assert_called_once_with()
            self.assertEqual(
                (source.id, source.commands, source.files), (identity, commands, files)
            )
            self.assertEqual(source._session._spec.init_timeout, 600)
            self.assertEqual(len(self.bodies), 1)


if __name__ == "__main__":
    unittest.main()
