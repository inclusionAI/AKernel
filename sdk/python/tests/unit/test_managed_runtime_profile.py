import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import httpx

from akernel_sdk import (
    BackendOperationError,
    DockerRuntime,
    ServiceBusyException,
    TimeoutException,
    build_image,
)
from akernel_sdk._managed_build import upload_context
from akernel_sdk.types import CommandResult


class ManagedProfileContract(unittest.TestCase):
    def test_command_timeout_preserves_partial_build_log_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            sandbox = MagicMock(id="owned-helper")
            failure = TimeoutException(
                "execution deadline",
                code="COMMAND_TIMEOUT",
                command_result=CommandResult("stdout", "stderr", None),
            )
            sandbox.commands.run.side_effect = failure
            with (
                patch("akernel_sdk.sandbox.Sandbox", return_value=sandbox),
                patch("akernel_sdk._managed_build.upload_context"),
                self.assertRaises(TimeoutException) as caught,
            ):
                build_image(root, profile="harbor-v1", log_path=root / "build.log")
            self.assertIs(caught.exception, failure)
            self.assertEqual((root / "build.log").read_text(), "stdoutstderr")
            sandbox.kill.assert_called_once()

    def test_partial_log_write_failure_cannot_replace_command_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            sandbox = MagicMock(id="owned-helper")
            failure = TimeoutException(
                "execution deadline", command_result=CommandResult("partial", "", None)
            )
            sandbox.commands.run.side_effect = failure
            with (
                patch("akernel_sdk.sandbox.Sandbox", return_value=sandbox),
                patch("akernel_sdk._managed_build.upload_context"),
                patch.object(Path, "write_text", side_effect=OSError("disk full")),
                self.assertRaises(TimeoutException) as caught,
            ):
                build_image(root, profile="harbor-v1", log_path=root / "build.log")
            self.assertIs(caught.exception, failure)
            if hasattr(failure, "add_note"):
                self.assertTrue(
                    any("partial build log" in note for note in failure.__notes__)
                )
            sandbox.kill.assert_called_once()

    def test_invalid_preparation_requests_fail_before_creating_a_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            for options in (
                {"cpu_request": 0},
                {"cpu_request": True},
                {"memory_request": -1},
                {"memory_request": False},
                {"memory_request": 1.5},
            ):
                with (
                    self.subTest(options=options),
                    patch("akernel_sdk.sandbox.Sandbox") as factory,
                    patch("akernel_sdk._managed_build.Retention") as retention,
                ):
                    with self.assertRaisesRegex(
                        ValueError, "request must be a positive integer"
                    ):
                        build_image(root, profile="harbor-ephemeral", **options)
                    factory.assert_not_called()
                    retention.assert_not_called()

    def test_preparation_requests_are_bounded_by_execution_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            for cpu_request, memory_request, expected in (
                (None, None, (1000, 2048)),
                (10, 64, (10, 64)),
                (2000, 64, (1000, 64)),
                (10, 4096, (10, 2048)),
                (2000, 4096, (1000, 2048)),
            ):
                sandbox = MagicMock()
                sandbox.commands.run.side_effect = RuntimeError("stop after admission")
                with self.subTest(
                    cpu_request=cpu_request, memory_request=memory_request
                ):
                    with (
                        patch(
                            "akernel_sdk.sandbox.Sandbox", return_value=sandbox
                        ) as factory,
                        self.assertRaisesRegex(RuntimeError, "stop after admission"),
                    ):
                        build_image(
                            root,
                            profile="harbor-v1",
                            cpu_request=cpu_request,
                            memory_request=memory_request,
                        )
                    options = factory.call_args.kwargs
                    self.assertEqual((options["cpu"], options["memory"]), expected)
                    self.assertEqual(
                        (options["cpu_limit"], options["mem_limit"]), (1000, 2048)
                    )
                    sandbox.kill.assert_called_once()

    def test_only_capability_name_is_submitted(self):
        options = DockerRuntime(profile="harbor-v1").sandbox_options()
        self.assertEqual(
            options, {"extra_config": {"managedRuntimeProfile": "harbor-v1"}}
        )
        self.assertNotIn("mounts", options)
        for value in ["../node", "http://node", ""]:
            with self.assertRaises(ValueError):
                DockerRuntime(profile=value)

    def test_context_archive_preserves_links_modes_and_empty_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "empty").mkdir()
            (root / "file").write_text("data")
            (root / "file").chmod(0o640)
            (root / "link").symlink_to("file")
            sandbox = MagicMock()

            def inspect(source, _target):
                with tarfile.open(source) as archive:
                    self.assertTrue(archive.getmember("./link").issym())
                    self.assertEqual(archive.getmember("./file").mode, 0o640)
                    self.assertTrue(archive.getmember("./empty").isdir())

            sandbox.files.copy_from_local.side_effect = inspect
            upload_context(sandbox, root, "/work/context")

    def test_image_build_needs_no_client_registry_or_node_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            sandbox = MagicMock()
            sandbox.id = "owned-builder"
            handle = {
                "reference": "private.test/task@sha256:" + "a" * 64,
                "image_process": {
                    "Env": ["MODE=native"],
                    "WorkingDir": "/app",
                    "User": "1000",
                    "Cmd": ["/bin/bash"],
                },
            }

            def execute(command, **_kwargs):
                return CommandResult(
                    stdout=json.dumps(handle)
                    if command == "cat /work/artifact.json"
                    else "",
                    stderr="",
                    exit_code=0,
                )

            sandbox.commands.run.side_effect = execute
            with patch("akernel_sdk.sandbox.Sandbox", return_value=sandbox) as factory:
                result = build_image(root, profile="harbor-v1")
            self.assertEqual(
                factory.call_args.kwargs["extra_config"],
                {"managedRuntimeProfile": "harbor-v1"},
            )
            self.assertNotIn("mounts", factory.call_args.kwargs)
            self.assertNotIn("image", factory.call_args.kwargs)
            self.assertEqual(result.envs, {"MODE": "native"})
            self.assertEqual(result.cwd, "/app")
            self.assertTrue(result.has_startup_command)
            sandbox.kill.assert_called_once()

    def test_ephemeral_build_registers_plan_before_push_and_returns_expiry(self):
        repository = "registry.test/akernel-ephemeral/" + "c" * 32 + "/service-0"
        tag = "build-" + "e" * 32
        reference = repository + "@sha256:" + "a" * 64
        builder = MagicMock()
        retention = MagicMock()
        retention.request.side_effect = [
            {"id": "e" * 32, "tag": tag, "profile": "harbor-ephemeral-b"},
            {},
            {"expires_at": 1750, "pressure_evictable": True},
        ]
        files = iter([{"tag": repository + ":" + tag}, {"reference": reference}])

        def execute(command, **_kwargs):
            if "image-build" in command and "--plan-only" not in command:
                self.assertEqual(retention.request.call_args.args, ("plan",))
            body = ""
            if command == "cat /work/artifact.json":
                body = json.dumps(next(files))
            return CommandResult(body, "", 0)

        builder.commands.run.side_effect = execute
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("akernel_sdk.sandbox.Sandbox", return_value=builder) as factory,
            patch("akernel_sdk._managed_build.Retention", return_value=retention),
        ):
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            result = build_image(
                root,
                profile="harbor-ephemeral",
                ttl_seconds=750,
                cpu_request=2000,
                memory_request=4096,
            )
        self.assertEqual(result.expires_at, 1750)
        self.assertTrue(result.pressure_evictable)
        self.assertEqual(
            (factory.call_args.kwargs["cpu"], factory.call_args.kwargs["memory"]),
            (1000, 2048),
        )
        self.assertEqual(
            factory.call_args.kwargs["extra_config"],
            {"managedRuntimeProfile": "harbor-ephemeral-b"},
        )
        self.assertEqual(
            retention.request.call_args_list,
            [
                call("begin", ttl_seconds=750),
                call("plan", publication="e" * 32, tag=repository + ":" + tag),
                call("commit", publication="e" * 32, reference=reference),
            ],
        )
        builder.kill.assert_called_once()

    def test_unified_profile_checks_archive_before_reserving_a_build(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"AKERNEL_HARBOR_V1_TEMPORARY_IMAGES": "1"}),
            patch("akernel_sdk.sandbox.Sandbox") as builder,
            patch("akernel_sdk._managed_build.Retention") as retention,
        ):
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            retention.return_value.request.return_value = {"checkpoint_archive": False}
            with self.assertRaisesRegex(
                BackendOperationError, "archive is unavailable"
            ):
                build_image(root, profile="harbor-v1")
            retention.return_value.request.assert_called_once_with(
                "archive-capabilities"
            )
            builder.assert_not_called()

    def test_capacity_rejection_precedes_preparation_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            token = root / "token"
            token.write_text("fixture-token")
            response = httpx.Response(
                503, json={
                    "error": "Registry is busy",
                    "code": "REGISTRY_CAPACITY_UNAVAILABLE",
                },
            )
            with (
                patch.dict("os.environ", {
                    "AKERNEL_IMAGE_RETENTION_URL": "http://127.0.0.1:8098",
                    "AKERNEL_IMAGE_RETENTION_TOKEN_FILE": str(token),
                }),
                patch(
                    "akernel_sdk._ephemeral_images.httpx.post", return_value=response
                ),
                patch("akernel_sdk.sandbox.Sandbox") as builder,
                self.assertRaises(ServiceBusyException) as caught,
            ):
                build_image(root, profile="harbor-ephemeral")
        self.assertEqual(caught.exception.outcome, "not_started")
        self.assertEqual(caught.exception.code, "REGISTRY_CAPACITY_UNAVAILABLE")
        builder.assert_not_called()

    def test_preparation_failure_releases_reservation_and_preserves_error(self):
        publication = "e" * 32
        retention = MagicMock()
        retention.request.side_effect = [
            {
                "id": publication,
                "profile": "harbor-ephemeral-a",
                "prepare_until": 1500,
            },
            {},
        ]
        failed = TimeoutException("preparation timed out")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("akernel_sdk._managed_build.Retention", return_value=retention),
            patch("akernel_sdk.sandbox.Sandbox", side_effect=failed),
            self.assertRaises(TimeoutException) as caught,
        ):
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            build_image(root, profile="harbor-ephemeral")
        self.assertIs(caught.exception, failed)
        self.assertEqual(
            retention.request.call_args_list,
            [
                call("begin", ttl_seconds=None),
                call("cancel-build", publication=publication),
            ],
        )

    def test_effective_empty_and_nonempty_image_startup(self):
        for process, expected in [
            ({}, False),
            ({"Entrypoint": [], "Cmd": None}, False),
            ({"Entrypoint": None, "Cmd": []}, False),
            ({"Entrypoint": ["/server"], "Cmd": []}, True),
            ({"Entrypoint": [], "Cmd": ["/bin/bash"]}, True),
        ]:
            with (
                self.subTest(process=process),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / "Dockerfile").write_text("FROM scratch\nENTRYPOINT []\n")
                sandbox = MagicMock()
                handle = {
                    "reference": "private.test/task@sha256:" + "a" * 64,
                    "image_process": process,
                }

                def execute(command, _handle=handle, **_kwargs):
                    return CommandResult(
                        stdout=json.dumps(_handle)
                        if command == "cat /work/artifact.json"
                        else "",
                        stderr="",
                        exit_code=0,
                    )

                sandbox.commands.run.side_effect = execute
                with patch("akernel_sdk.sandbox.Sandbox", return_value=sandbox):
                    result = build_image(root, profile="harbor-v1")
                self.assertIs(result.has_startup_command, expected)
                sandbox.kill.assert_called_once()

    def test_failed_build_retains_primary_error_and_cleanup_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            sandbox = MagicMock()
            sandbox.id = "owned-builder"
            primary = RuntimeError("upstream build failed")

            def execute(command, **_kwargs):
                if "image-build" in command:
                    raise primary
                return CommandResult(stdout="", stderr="", exit_code=0)

            sandbox.commands.run.side_effect = execute
            sandbox.kill.side_effect = RuntimeError("cleanup unavailable")
            with (
                patch("akernel_sdk.sandbox.Sandbox", return_value=sandbox),
                self.assertRaises(RuntimeError) as error,
            ):
                build_image(root, profile="harbor-v1")
            self.assertIs(error.exception, primary)
            self.assertTrue(getattr(error.exception, "cleanup_errors", ()))


if __name__ == "__main__":
    unittest.main()
