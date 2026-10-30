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

"""E2B-style snapshot creation preserves the existing restore contract."""

import unittest
from unittest.mock import MagicMock, patch

from akernel_sdk import BackendOperationError, Sandbox, SnapshotInfo
from akernel_sdk import sandbox as public
from akernel_sdk.types import HttpReverseTunnel, Mount, NetworkPolicy, SandboxInfo


class SnapshotCreateTest(unittest.TestCase):
    def setUp(self):
        self.backend = MagicMock()
        self.session = self.backend.from_snapshot.return_value
        self.session.rollback_creation = self.session.terminate
        self.session.id = "restored-child"
        self.session.get_info.return_value = SandboxInfo(
            "restored-child", "running", 2500, 6000, "captured-image"
        )
        patcher = patch.object(public, "load_backend", return_value=self.backend)
        self.load = patcher.start()
        self.addCleanup(patcher.stop)

    def test_positional_id_restores_without_fresh_boot_side_effects(self):
        with Sandbox.create("saved") as restored:
            self.assertEqual(restored.id, "restored-child")
            self.assertEqual(restored.get_info().cpu, 2500)
            self.assertEqual(restored.get_info().memory, 6000)
            self.assertIs(restored.commands._driver, self.session.commands)
            self.assertIsNone(restored.startup_command)
            self.session.files.make_dir.assert_not_called()
        self.backend.create.assert_not_called()
        self.backend.from_snapshot.assert_called_once()
        self.session.terminate.assert_called_once()
        self.backend.delete_snapshot.assert_not_called()

    def test_keyword_id_and_snapshot_info_are_accepted(self):
        for identity in ("  saved  ", SnapshotInfo("saved", ("label",))):
            with self.subTest(identity=identity), Sandbox.create(snapshot_id=identity):
                spec = self.backend.from_snapshot.call_args.args[0]
                self.assertEqual(spec.snapshot_id, "saved")

    def test_options_and_facades_match_legacy_restore(self):
        options = dict(
            idle_timeout=600,
            schedule_timeout=90,
            timeout=180,
            name="branch",
            cwd="/not-created",
            port_forwardings=[8080],
            mounts=[Mount(target="/tools", image_url="tools:latest")],
            extra_config={
                "fileBackedTmpfsMounts": [{"target": "/upper", "sizeMB": 64}]
            },
            reverse_tunnel=HttpReverseTunnel(target="http://127.0.0.1:8123"),
            detached=False,
            failover=True,
            node_id="node-a",
            network_policy=NetworkPolicy.block(),
        )
        with Sandbox.from_snapshot("saved", **options) as legacy:
            previous_spec = self.backend.from_snapshot.call_args.args[0]
            with Sandbox.create("saved", **options) as restored:
                self.assertEqual(
                    self.backend.from_snapshot.call_args.args[0], previous_spec
                )
                self.assertIsNot(restored, legacy)
                self.assertIsNot(restored.commands, legacy.commands)
                self.assertIsNot(restored.files, legacy.files)
                self.assertIs(restored.reverse_tunnel, options["reverse_tunnel"])
        self.assertEqual(self.backend.from_snapshot.call_count, 2)
        self.backend.create.assert_not_called()

    def test_owned_and_detached_lifecycle_match_legacy_restore(self):
        for factory in (Sandbox.create, Sandbox.from_snapshot):
            for detached in (False, True):
                self.session.reset_mock()
                with self.subTest(factory=factory.__name__, detached=detached):
                    with factory("saved", detached=detached):
                        pass
                    self.assertEqual(
                        self.session.terminate.call_count, 0 if detached else 1
                    )
                    self.session.close.assert_called_once()

    def test_invalid_options_fail_before_loading_backend(self):
        for options in (
            {"snapshot_id": ""},
            {"snapshot_id": 42},
            {"snapshot_id": "saved", "timeout": 0},
            {"snapshot_id": "saved", "schedule_timeout": True},
            {"snapshot_id": "saved", "detached": 1},
            {"snapshot_id": "saved", "cwd": "relative"},
            {"snapshot_id": "saved", "port_forwardings": [8080, 8080]},
            {"snapshot_id": "saved", "mounts": [object()]},
            {"snapshot_id": "saved", "network_policy": {}},
            {"snapshot_id": "saved", "extra_config": []},
            {"snapshot_id": "saved", "extra_config": {"size": float("nan")}},
        ):
            with (
                self.subTest(options=options),
                self.assertRaises((TypeError, ValueError)),
            ):
                Sandbox.create(**options)
        self.load.assert_not_called()

    def test_restore_rejects_fresh_overrides_and_unknown_template_option(self):
        for options in (
            {"template": "label"},
            {"snapshot_id": "saved", "cpu": 1000},
            {"snapshot_id": "saved", "dockerfile": object()},
        ):
            with (
                self.subTest(options=options),
                self.assertRaises((TypeError, ValueError)),
            ):
                Sandbox.create(**options)
        self.load.assert_not_called()

    def test_fresh_create_supports_image_and_cluster_default(self):
        self.backend.create.return_value = self.session
        with Sandbox.create(
            image="python:3.13-slim", cpu=2000, memory=1024, envs={"HELLO": "world"}
        ) as created:
            self.assertEqual(created.id, "restored-child")
            spec = self.backend.create.call_args.args[0]
            self.assertEqual(spec.image, "python:3.13-slim")
            self.assertEqual(spec.cpu, 2000)
            self.assertEqual(spec.memory, 1024)
            self.assertEqual(spec.env, {"HELLO": "world"})
        with Sandbox.create():
            self.assertIsNone(self.backend.create.call_args.args[0].image)
        self.backend.from_snapshot.assert_not_called()

    def test_unknown_outcome_and_interruption_are_not_replayed(self):
        for failure in (
            BackendOperationError(
                "reply lost", request_id="restore-request", outcome="unknown"
            ),
            KeyboardInterrupt(),
        ):
            self.backend.from_snapshot.reset_mock()
            self.backend.from_snapshot.side_effect = failure
            with (
                self.subTest(failure=type(failure)),
                self.assertRaises(type(failure)) as raised,
            ):
                Sandbox.create("saved")
            self.assertIs(raised.exception, failure)
            self.backend.from_snapshot.assert_called_once()
            self.backend.create.assert_not_called()

    def test_binding_failure_retains_identity_and_cleanup_error(self):
        failure = BackendOperationError("metadata unavailable", request_id="metadata")
        cleanup = BackendOperationError("delete unavailable", request_id="rollback")
        self.session.get_info.side_effect = failure
        self.session.terminate.side_effect = cleanup
        with self.assertLogs(public.logger, level="WARNING"):
            with self.assertRaises(BackendOperationError) as raised:
                Sandbox.create("saved")
        self.assertIs(raised.exception, failure)
        self.assertEqual(failure.environment_id, "restored-child")
        self.assertEqual(failure.cleanup_errors, (cleanup,))
        self.assertEqual(failure.request_id, "metadata")
        self.backend.from_snapshot.assert_called_once()
        self.session.terminate.assert_called_once()

    def test_subclass_is_preserved_without_fresh_constructor(self):
        class ChildSandbox(Sandbox):
            def __init__(self, *args, **kwargs):
                raise AssertionError("restore must not run fresh constructor")

        with ChildSandbox.create("saved") as restored:
            self.assertIsInstance(restored, ChildSandbox)
            self.assertEqual(restored.id, "restored-child")


if __name__ == "__main__":
    unittest.main()
