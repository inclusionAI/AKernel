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

"""Independent command, process, and filesystem end-to-end assertions."""

import hashlib
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from akernel_sdk import Sandbox

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class CommandAndFilesystemIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048)

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.kill()

    def test_command_cwd_override_and_large_output(self):
        result = self.sandbox.commands.run(
            "pwd; head -c 65536 /dev/zero | tr '\\000' 'x'",
            cwd="/tmp",
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("/tmp\n"))
        self.assertEqual(result.stdout[5:], "x" * 65536)

    def test_background_wait_timeout_preserves_running_result(self):
        handle = self.sandbox.commands.run(
            "sleep 3; printf finished", background=True
        )
        try:
            pending = handle.wait(timeout=0.01)
            self.assertIsNone(pending.exit_code)
            self.assertEqual(pending.status, "RUNNING")
            self.assertEqual(pending.error_code, "WAIT_TIMEOUT")
            self.assertTrue(pending.error_message)
            completed = handle.wait(timeout=15)
            self.assertEqual(completed.exit_code, 0, completed.stderr)
            self.assertEqual(completed.stdout, "finished")
        finally:
            handle.kill()

    def test_background_process_kill_releases_process(self):
        handle = self.sandbox.commands.run("sleep 60", background=True)
        self.assertTrue(
            any(p.pid == handle.pid and p.running for p in self.sandbox.commands.list())
        )
        self.assertTrue(handle.kill())
        deadline = time.monotonic() + 5
        while any(
            p.pid == handle.pid and p.running for p in self.sandbox.commands.list()
        ):
            if time.monotonic() >= deadline:
                self.fail(f"process {handle.pid} remained running after kill")
            time.sleep(0.05)

    def test_nested_directory_binary_and_empty_file_round_trip(self):
        root = f"/tmp/e2e-files-{uuid.uuid4().hex[:10]}"
        child = f"{root}/child"
        try:
            self.assertTrue(self.sandbox.files.make_dir(root))
            self.assertTrue(self.sandbox.files.make_dir(child))
            self.sandbox.files.write(f"{root}/empty", b"")
            binary = bytes(range(256)) * 4
            self.sandbox.files.write(f"{child}/payload", binary)
            self.assertEqual(
                self.sandbox.files.read(f"{root}/empty", format="bytes"), b""
            )
            self.assertEqual(
                self.sandbox.files.read(f"{child}/payload", format="bytes"), binary
            )
            paths = {entry.path for entry in self.sandbox.files.list(root, depth=2)}
            self.assertIn(f"{child}/payload", paths)
            self.assertIn(f"{root}/empty", paths)
        finally:
            self.sandbox.commands.run(f"rm -rf -- {root}")

    def test_directory_copy_round_trip_preserves_tree(self):
        remote = f"/tmp/e2e-copy-{uuid.uuid4().hex[:10]}"
        try:
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source"
                source.mkdir()
                (source / "nested").mkdir()
                (source / "nested" / "payload.bin").write_bytes(b"\x00copy\xff")
                (source / "empty.txt").write_bytes(b"")
                self.sandbox.files.copy_from_local(str(source), remote)

                target = Path(directory) / "target"
                self.sandbox.files.copy_to_local(remote, str(target))
                self.assertEqual(
                    (target / "nested" / "payload.bin").read_bytes(), b"\x00copy\xff"
                )
                self.assertEqual((target / "empty.txt").read_bytes(), b"")
        finally:
            self.sandbox.commands.run(f"rm -rf -- {remote}")

    def test_megabyte_binary_round_trip_has_identical_digest(self):
        remote = f"/tmp/e2e-binary-{uuid.uuid4().hex[:10]}"
        payload = os.urandom(1024 * 1024)
        try:
            self.sandbox.files.write(remote, payload)
            received = self.sandbox.files.read(remote, format="bytes")
            self.assertEqual(len(received), len(payload))
            self.assertEqual(
                hashlib.sha256(received).digest(), hashlib.sha256(payload).digest()
            )
        finally:
            self.sandbox.files.remove(remote)


if __name__ == "__main__":
    unittest.main()
