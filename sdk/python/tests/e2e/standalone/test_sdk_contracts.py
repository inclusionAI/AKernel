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

"""Standalone-safe contracts for the backend-neutral AKernel SDK surface."""

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from akernel_sdk import BackendOperationError, Sandbox, resources

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")
_IMAGE = os.environ.get("AKERNEL_TEST_IMAGE") or None


@unittest.skipUnless(
    _ENABLED,
    "set AKERNEL_RUN_INTEGRATION=1 and the AKernel SDK environment",
)
class SandboxPublicContractIntegrationTest(unittest.TestCase):
    """Exercise public operations that must work for every supported backend."""

    @classmethod
    def setUpClass(cls):
        cls.sandbox = Sandbox(
            cpu=1000,
            memory=2048,
            runtime=_RUNTIME,
            image=_IMAGE,
            env={"AKERNEL_SANDBOX_ENV": "sandbox-value"},
            cwd="/tmp",
        )

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.kill()

    def test_command_environment_cwd_stderr_and_nonzero_exit(self):
        result = self.sandbox.commands.run(
            'printf \'%s|%s\' "$AKERNEL_SANDBOX_ENV" "$AKERNEL_COMMAND_ENV"; '
            "printf 'command-stderr' >&2; exit 7",
            envs={"AKERNEL_COMMAND_ENV": "command-value"},
        )

        self.assertEqual(result.stdout, "sandbox-value|command-value")
        self.assertEqual(result.stderr, "command-stderr")
        self.assertEqual(result.exit_code, 7)

        cwd = self.sandbox.commands.run("pwd")
        self.assertEqual(cwd.exit_code, 0, cwd.stderr)
        self.assertEqual(cwd.stdout.strip(), "/tmp")

    def test_background_command_stdin_eof_and_process_state(self):
        handle = self.sandbox.commands.run(
            "wc -l",
            background=True,
            stdin=True,
        )
        handle.send_stdin("first\nsecond\n")

        running = next(
            process
            for process in self.sandbox.commands.list()
            if process.pid == handle.pid
        )
        self.assertTrue(running.running)
        self.assertEqual(running.command, "wc -l")

        handle.close_stdin()
        result = handle.wait(timeout=30)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2")

        completed = next(
            process
            for process in self.sandbox.commands.list()
            if process.pid == handle.pid
        )
        self.assertFalse(completed.running)

    def test_foreground_command_timeout_preserves_sandbox(self):
        with self.assertRaises(BackendOperationError):
            self.sandbox.commands.run("sleep 3", timeout=1)
        self.assertEqual(
            self.sandbox.commands.run("printf after-timeout").stdout,
            "after-timeout",
        )

    def test_filesystem_binary_metadata_list_rename_remove_and_copy(self):
        root = "/tmp/akernel-sdk-contract"
        source = f"{root}/source.bin"
        renamed = f"{root}/renamed.bin"
        payload = b"\x00AKernel\xff\n"

        if self.sandbox.files.exists(root):
            self.sandbox.commands.run(f"rm -rf -- {root}")
        self.assertTrue(self.sandbox.files.make_dir(root))
        self.assertFalse(self.sandbox.files.make_dir(root))

        written = self.sandbox.files.write(source, payload)
        self.assertEqual(written.path, source)
        self.assertEqual(written.size, len(payload))
        self.assertEqual(self.sandbox.files.read(source, format="bytes"), payload)

        entries = self.sandbox.files.list(root, depth=1)
        self.assertIn(source, {entry.path for entry in entries})
        info = self.sandbox.files.get_info(source)
        self.assertEqual(info.size, len(payload))

        renamed_info = self.sandbox.files.rename(source, renamed)
        self.assertEqual(renamed_info.path, renamed)
        self.assertFalse(self.sandbox.files.exists(source))
        self.assertTrue(self.sandbox.files.exists(renamed))

        with tempfile.TemporaryDirectory() as directory:
            local_source = Path(directory) / "upload.txt"
            local_target = Path(directory) / "download.txt"
            local_source.write_text("copy-round-trip", encoding="utf-8")
            remote_copy = f"{root}/upload.txt"
            self.sandbox.files.copy_from_local(str(local_source), remote_copy)
            self.sandbox.files.copy_to_local(remote_copy, str(local_target))
            self.assertEqual(
                local_target.read_text(encoding="utf-8"),
                "copy-round-trip",
            )
            self.sandbox.files.remove(remote_copy)

        self.sandbox.files.remove(renamed)
        self.sandbox.files.remove(root)
        self.assertFalse(self.sandbox.files.exists(root))

    def test_sandbox_info_matches_requested_resources(self):
        self.assertTrue(self.sandbox.is_running())
        info = self.sandbox.get_info()
        self.assertEqual(info.id, self.sandbox.id)
        self.assertEqual(info.cpu, 1000)
        self.assertEqual(info.memory, 2048)
        self.assertEqual(info.image, _IMAGE)
        self.assertTrue(info.state)

    def test_missing_file_operations_report_errors_without_creating_file(self):
        missing = f"/tmp/akernel-missing-{uuid.uuid4().hex}"
        self.assertFalse(self.sandbox.files.exists(missing))
        with self.assertRaises(BackendOperationError):
            self.sandbox.files.read(missing)
        with self.assertRaises(BackendOperationError):
            self.sandbox.files.get_info(missing)
        self.assertFalse(self.sandbox.files.exists(missing))

    def test_cluster_resources_include_a_schedulable_node(self):
        nodes = resources()
        self.assertTrue(nodes)
        self.assertTrue(
            any(
                node.allocatable.get("CPU", 0) > 0
                and node.allocatable.get("Memory", 0) > 0
                for node in nodes
            ),
            nodes,
        )


@unittest.skipUnless(
    _ENABLED,
    "set AKERNEL_RUN_INTEGRATION=1 and the AKernel SDK environment",
)
class SandboxCleanupIntegrationTest(unittest.TestCase):
    def test_explicit_kill_is_repeatable_and_closes_local_state(self):
        sandbox = Sandbox(
            cpu=1000,
            memory=2048,
            runtime=_RUNTIME,
            image=_IMAGE,
        )
        sandbox_id = sandbox.id
        self.assertTrue(sandbox.is_running())

        sandbox.kill()
        self.assertFalse(sandbox.is_running(), sandbox_id)
        sandbox.kill()


if __name__ == "__main__":
    unittest.main()
