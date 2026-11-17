import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from akernel_sdk import DockerRuntime, TimeoutException, build_image
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
                {"cpu_request": 1001},
                {"memory_request": 2049},
                {"memory_request": 1.5},
            ):
                with (
                    self.subTest(options=options),
                    patch("akernel_sdk.sandbox.Sandbox") as factory,
                    self.assertRaisesRegex(ValueError, "request must be an integer"),
                ):
                    build_image(root, profile="harbor-v1", **options)
                factory.assert_not_called()

    def test_preparation_requests_preserve_existing_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n")
            sandbox = MagicMock()
            sandbox.commands.run.side_effect = RuntimeError("stop after admission")
            with patch("akernel_sdk.sandbox.Sandbox", return_value=sandbox) as factory:
                with self.assertRaisesRegex(RuntimeError, "stop after admission"):
                    build_image(
                        root, profile="harbor-v1", cpu_request=10, memory_request=64
                    )
            options = factory.call_args.kwargs
            self.assertEqual((options["cpu"], options["memory"]), (10, 64))
            self.assertEqual((options["cpu_limit"], options["mem_limit"]), (1000, 2048))
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
