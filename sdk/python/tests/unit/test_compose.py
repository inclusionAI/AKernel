"""Compose phases, immutable inputs, failure diagnostics and restore attachment."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from akernel_sdk import ComposeOperationError, ComposeProject, DockerRuntime
from akernel_sdk._backends.errors import BackendOperationError
from akernel_sdk.types import CommandResult


class ComposeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.context = self.root / "source"
        self.context.mkdir()
        (self.context / "Dockerfile").write_text("FROM scratch\n")
        self.file = self.context / "docker-compose.yaml"
        self.file.write_text(
            "services:\n  api:\n    build: .\n  load:\n    image: alpine:3.20\n"
        )
        self.sandbox = MagicMock(id="source")
        self.sandbox.commands.run.return_value = CommandResult(
            stdout="", stderr="", exit_code=0
        )

    def prepare(self, **kwargs):
        kwargs.setdefault("network", "host")
        return ComposeProject.prepare(
            self.context, [self.file], output_dir=self.root / "prepared", **kwargs
        )

    def test_frozen_inputs_drive_both_discovery_and_remote_upload(self):
        binary = self.root / "bridge"
        binary.write_bytes(b"fixture")
        images = MagicMock()
        images.mounts.return_value = []
        with patch(
            "akernel_sdk._compose.prepare_compose_images", return_value=images
        ) as discover:
            project = self.prepare(
                auto_images=True, runtime=DockerRuntime(bridge_binary=binary)
            )
        frozen = discover.call_args.args[0]
        self.assertEqual((frozen / "Dockerfile").read_text(), "FROM scratch\n")
        (self.context / "Dockerfile").write_text("FROM changed\n")
        self.file.write_text("services: {different: {image: wrong}}")
        with patch.object(DockerRuntime, "start"):
            project.start_runtime(self.sandbox)
        uploaded = Path(self.sandbox.files.copy_from_local.call_args.args[0])
        self.assertEqual((uploaded / "Dockerfile").read_text(), "FROM scratch\n")
        staged = "".join(c.args[1] for c in self.sandbox.files.write.call_args_list)
        self.assertIn("load:", staged)
        self.assertNotIn("different:", staged)
        self.assertNotIn("harbor", project.command(["ps"]))

    def test_unknown_build_outcome_is_not_replayed(self):
        project = self.prepare()
        with patch.object(DockerRuntime, "start"):
            project.start_runtime(self.sandbox)
        self.sandbox.commands.run.reset_mock()
        failure = BackendOperationError(
            "lost reply", outcome="unknown", request_id="same-request"
        )
        self.sandbox.commands.run.side_effect = failure
        with self.assertRaises(BackendOperationError) as raised:
            project.build()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.sandbox.commands.run.call_count, 1)

    def test_build_and_up_are_separate_policy_boundaries(self):
        project = self.prepare(buildkit=False)
        with patch.object(DockerRuntime, "start"):
            project.start_runtime(self.sandbox)
        self.sandbox.commands.run.reset_mock()
        project.build(no_cache=True)
        commands = [c.args[0] for c in self.sandbox.commands.run.call_args_list]
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].startswith("DOCKER_BUILDKIT=0 "))
        self.assertIn("build --no-cache", commands[0])
        self.assertNotIn(" up ", commands[0])
        project.up()
        self.assertIn("up -d --wait", self.sandbox.commands.run.call_args.args[0])

    def test_automatic_images_verify_pinned_inputs_and_never_pull_them_again(self):
        binary = self.root / "bridge"
        binary.write_bytes(b"fixture")
        images = MagicMock()
        images.mounts.return_value = []
        original = (
            "services:\n  api:\n    build: .\n  load:\n    image: "
            "registry.example/load:release@sha256:" + "a" * 64 + "\n"
        )
        self.file.write_text(original)
        with patch("akernel_sdk._compose.prepare_compose_images", return_value=images):
            project = self.prepare(
                auto_images=True, runtime=DockerRuntime(bridge_binary=binary)
            )
        attached = ComposeProject.attach(self.sandbox, project.descriptor)
        attached.pull()
        self.assertIn("start.py --verify", self.sandbox.commands.run.call_args.args[0])
        self.sandbox.commands.run.reset_mock()
        attached.build(no_cache=True)
        attached.up()
        commands = [c.args[0] for c in self.sandbox.commands.run.call_args_list]
        self.assertIn("build --no-cache", commands[0])
        self.assertIn("start.py --verify", commands[1])
        self.assertIn("--pull never", commands[2])
        self.assertNotIn("--no-build", commands[2])
        self.assertFalse(any(" pull " in c for c in commands))
        self.assertEqual(self.file.read_text(), original)
        self.assertEqual((self.root / "prepared/source-0.yaml").read_text(), original)

    def test_automatic_image_verification_failure_is_not_hidden_or_retried(self):
        project = self.prepare()
        descriptor = project.descriptor
        descriptor["automatic_images"] = True
        attached = ComposeProject.attach(self.sandbox, descriptor)
        self.sandbox.commands.run.return_value = CommandResult(
            stdout="", stderr="missing ROFS snapshot", exit_code=1
        )
        for phase in ("pull", "up"):
            with self.subTest(phase=phase):
                self.sandbox.commands.run.reset_mock()
                with self.assertRaises(ComposeOperationError) as raised:
                    getattr(attached, phase)()
                self.assertEqual(raised.exception.phase, phase)
                self.assertEqual(raised.exception.stderr, "missing ROFS snapshot")
                self.assertEqual(self.sandbox.commands.run.call_count, 1)

    def test_unmanaged_pull_keeps_native_compose_behavior(self):
        project = ComposeProject.attach(self.sandbox, self.prepare().descriptor)
        project.pull()
        self.assertTrue(
            self.sandbox.commands.run.call_args.args[0].endswith(
                "pull --ignore-buildable"
            )
        )

    def test_missing_exit_status_is_an_unknown_operation_outcome(self):
        failure = ComposeOperationError(
            "build", CommandResult(stdout="partial", stderr="", exit_code=None)
        )
        self.assertEqual(failure.outcome, "unknown")

    def test_zero_timeout_image_verification_waits_without_foreground_timeout(self):
        project = self.prepare()
        descriptor = project.descriptor
        descriptor["automatic_images"] = True
        project = ComposeProject.attach(self.sandbox, descriptor)
        handle = self.sandbox.commands.run.return_value = MagicMock(
            command_id="verify-id"
        )
        handle.wait.side_effect = [
            CommandResult(stdout="", stderr="", exit_code=None),
            CommandResult(stdout="verified", stderr="", exit_code=0),
        ]
        project.pull(timeout=0)
        self.assertIs(self.sandbox.commands.run.call_args.kwargs["background"], True)
        handle.kill.assert_not_called()

    def test_failed_cancel_keeps_original_timeout_and_command_identity(self):
        from threading import Event

        from akernel_sdk import TimeoutException

        project = ComposeProject.attach(self.sandbox, self.prepare().descriptor)
        handle = self.sandbox.commands.run.return_value = MagicMock(
            command_id="owned-command"
        )
        cleanup = RuntimeError("kill response lost")
        handle.kill.side_effect = cleanup
        cancellation = Event()
        cancellation.set()
        with self.assertRaises(TimeoutException) as raised:
            project.run(["ps"], timeout=0, cancellation=cancellation)
        self.assertEqual(raised.exception.command_id, "owned-command")
        self.assertEqual(raised.exception.cleanup_errors, (cleanup,))

    def test_failed_up_retains_output_and_collects_logs_without_restarting(self):
        project = self.prepare()
        with patch.object(DockerRuntime, "start"):
            project.start_runtime(self.sandbox)
        self.sandbox.commands.run.reset_mock()
        self.sandbox.commands.run.side_effect = [
            CommandResult(
                stdout="service partial", stderr="health failed", exit_code=7
            ),
            CommandResult(stdout="diagnostic", stderr="", exit_code=0),
        ]
        with self.assertRaises(ComposeOperationError) as raised:
            project.up()
        self.assertEqual(raised.exception.phase, "up")
        self.assertEqual(raised.exception.exit_code, 7)
        self.assertEqual(raised.exception.stderr, "health failed")
        self.assertEqual(self.sandbox.commands.run.call_count, 2)
        self.assertEqual(
            (self.root / "prepared/services.log").read_text(), "diagnostic"
        )

    def test_attach_rebinds_only_and_retains_managed_upper_geometry(self):
        project = self.prepare(runtime=DockerRuntime(writable_mb=2048))
        with patch.object(DockerRuntime, "start"):
            project.start_runtime(self.sandbox)
        other = MagicMock(id="restored")
        attached = ComposeProject.attach(other, project.descriptor)
        other.commands.run.assert_not_called()
        other.files.copy_from_local.assert_not_called()
        self.assertEqual(attached.sandbox_options(), project.sandbox_options())
        with self.assertRaises(RuntimeError):
            attached.start_runtime(other)
        attached.service("load").exec("echo restored", user="user", envs={"A": "x y"})
        self.assertIn("exec -T -u user -e", other.commands.run.call_args.args[0])
        self.assertIn("load /bin/sh -c", other.commands.run.call_args.args[0])

    def test_explicit_network_fails_before_remote_mutations(self):
        self.file.write_text("services: {api: {networks: [private]}}")
        with self.assertRaisesRegex(ValueError, "explicit networks"):
            self.prepare()
        self.sandbox.commands.run.assert_not_called()

    def test_service_identity_rejects_scaled_service(self):
        project = self.prepare()
        attached = ComposeProject.attach(self.sandbox, project.descriptor)
        self.sandbox.commands.run.return_value = CommandResult(
            stdout="one\ntwo\n", stderr="", exit_code=0
        )
        with self.assertRaisesRegex(BackendOperationError, "exactly one"):
            attached.service("api").identity()

    def test_prebuilt_images_do_not_add_undeclared_services_or_builds(self):
        images = MagicMock()
        images.mounts.return_value = []
        project = self.prepare(
            runtime=DockerRuntime(images=images),
            prebuilt_images={"api": "registry/api:seed", "absent": "unused"},
        )
        other = ComposeProject.attach(self.sandbox, project.descriptor)
        other.build()
        self.sandbox.commands.run.assert_not_called()
        other.up()
        self.assertIn(
            "--no-build --pull never", self.sandbox.commands.run.call_args.args[0]
        )
        self.assertNotIn("absent", project.descriptor["services"])
        text = (self.root / "prepared/host.json").read_text()
        self.assertNotIn('"build"', text)

    def test_custom_hostnames_are_preserved_in_compatibility_overlay(self):
        self.file.write_text(
            "services: {worker: {image: alpine, hostname: custom.example}}"
        )
        self.prepare()
        hosts = json.loads((self.root / "prepared/host.json").read_text())["services"]
        self.assertEqual(hosts["worker"]["hostname"], "custom.example")
        self.assertIn("custom.example:127.0.0.1", hosts["worker"]["extra_hosts"])

    def test_partial_runtime_start_is_not_replayed(self):
        project = self.prepare()
        with patch.object(
            DockerRuntime, "start", side_effect=BackendOperationError("partial")
        ) as start:
            with self.assertRaises(BackendOperationError):
                project.start_runtime(self.sandbox)
            with self.assertRaises(RuntimeError):
                project.start_runtime(self.sandbox)
        start.assert_called_once()

    def test_copy_cleanup_failure_does_not_replace_transfer_failure(self):
        project = self.prepare()
        attached = ComposeProject.attach(self.sandbox, project.descriptor)
        failure = BackendOperationError("copy unavailable", request_id="transfer")
        cleanup = BackendOperationError("cleanup unavailable", request_id="cleanup")
        self.sandbox.files.copy_from_local.side_effect = failure
        self.sandbox.files.remove.side_effect = cleanup
        with self.assertRaises(BackendOperationError) as raised:
            attached.service("api").upload("local", "/target")
        self.assertIs(raised.exception, failure)
        self.assertEqual(failure.cleanup_errors, (cleanup,))

    def test_zero_timeout_waits_without_deadline_and_cancellation_keeps_identity(self):
        from threading import Event

        from akernel_sdk import TimeoutException

        project = self.prepare()
        attached = ComposeProject.attach(self.sandbox, project.descriptor)
        handle = self.sandbox.commands.run.return_value = MagicMock(
            command_id="stable-command"
        )
        handle.wait.side_effect = [
            CommandResult(stdout="", stderr="", exit_code=None),
            CommandResult(stdout="finished", stderr="", exit_code=0),
        ]
        self.assertEqual(attached.run(["ps"], timeout=0).stdout, "finished")
        self.assertTrue(self.sandbox.commands.run.call_args.kwargs["background"])
        cancellation = Event()
        cancellation.set()
        with self.assertRaises(TimeoutException) as raised:
            attached.run(["ps"], timeout=0, cancellation=cancellation)
        self.assertEqual(raised.exception.command_id, "stable-command")
        handle.kill.assert_called_once()
