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

import os
import time
import unittest

from akernel_sdk import Sandbox
from akernel_sdk._backends.errors import BackendOperationError

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")
_RECOVERY_ENABLED = (
    _ENABLED and os.environ.get("AKERNEL_RUN_RECOVERY_INTEGRATION") == "1"
)


@unittest.skipUnless(
    _ENABLED,
    "set AKERNEL_RUN_INTEGRATION=1 and the AKernel SDK environment",
)
class SandboxIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = Sandbox(cpu=1000, memory=2048, runtime=_RUNTIME)

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.kill()

    def test_command_and_process_list(self):
        result = self.sandbox.commands.run("printf AKERNEL_COMMAND_OK")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "AKERNEL_COMMAND_OK")

        handle = self.sandbox.commands.run("sleep 30", background=True)
        process = next(
            item for item in self.sandbox.commands.list() if item.pid == handle.pid
        )
        self.assertEqual(process.command, "sleep 30")
        self.assertTrue(process.running)
        handle.kill()

    def test_filesystem(self):
        self.sandbox.files.write("/tmp/akernel-integration.txt", "filesystem-ok")
        self.assertEqual(
            self.sandbox.files.read("/tmp/akernel-integration.txt"),
            "filesystem-ok",
        )
        self.assertTrue(self.sandbox.files.exists("/tmp/akernel-integration.txt"))

    def test_pty(self):
        output = bytearray()
        with self.sandbox.pty.create(on_data=output.extend) as session:
            session.send_stdin(b"printf 'AKERNEL_PTY_OK\\n'\n")
            session.resize(rows=40, cols=120)
            session.send_stdin(b"exit 7\n")
            self.assertEqual(session.wait(timeout=30), 7)
        self.assertIn(b"AKERNEL_PTY_OK", output)

    def test_pty_sessions_are_independent(self):
        first_output = bytearray()
        second_output = bytearray()

        with (
            self.sandbox.pty.create(on_data=first_output.extend) as first,
            self.sandbox.pty.create(on_data=second_output.extend) as second,
        ):
            first.send_stdin(b"printf 'PTY_FIRST\\n'\nexit 3\n")
            second.send_stdin(b"printf 'PTY_SECOND\\n'\nexit 4\n")
            self.assertEqual(first.wait(timeout=30), 3)
            self.assertEqual(second.wait(timeout=30), 4)

        self.assertIn(b"PTY_FIRST", first_output)
        self.assertNotIn(b"PTY_SECOND", first_output)
        self.assertIn(b"PTY_SECOND", second_output)
        self.assertNotIn(b"PTY_FIRST", second_output)

    def test_pty_interrupts_foreground_process(self):
        output = bytearray()
        with self.sandbox.pty.create(on_data=output.extend) as session:
            session.send_stdin(b"sleep 30\n")
            time.sleep(0.5)
            session.send_stdin(b"\x03")
            session.send_stdin(b"printf 'PTY_AFTER_INTERRUPT\\n'\nexit 0\n")
            self.assertEqual(session.wait(timeout=30), 0)

        self.assertIn(b"PTY_AFTER_INTERRUPT", output)


@unittest.skipUnless(
    _ENABLED,
    "set AKERNEL_RUN_INTEGRATION=1 and the AKernel SDK environment",
)
class SandboxCheckpointIntegrationTest(unittest.TestCase):
    def test_checkpoint_restore_and_delete(self):
        source = Sandbox(cpu=1000, memory=2048, runtime=_RUNTIME)
        restored = None
        checkpoint = None
        try:
            source_id = source.id
            created = source.commands.run(
                "printf checkpoint-before > /tmp/akernel-checkpoint-state && sync"
            )
            self.assertEqual(created.exit_code, 0)

            checkpoint = source.checkpoint(timeout=180)
            self.assertTrue(source.is_running())
            checkpoint_ids = {item.id for item in Sandbox.list_checkpoints()}
            self.assertIn(checkpoint.id, checkpoint_ids)

            changed = source.commands.run(
                "printf source-after > /tmp/akernel-checkpoint-state && sync"
            )
            self.assertEqual(changed.exit_code, 0)

            restored = Sandbox.restore(checkpoint)
            self.assertNotEqual(restored.id, source_id)
            restored_value = restored.commands.run("cat /tmp/akernel-checkpoint-state")
            self.assertEqual(restored_value.exit_code, 0)
            self.assertEqual(restored_value.stdout, "checkpoint-before")
            self.assertEqual(
                source.commands.run("cat /tmp/akernel-checkpoint-state").stdout,
                "source-after",
            )
        finally:
            if restored is not None:
                restored.kill()
            source.kill()
            if checkpoint is not None:
                Sandbox.delete_checkpoint(checkpoint)


@unittest.skipUnless(
    _RECOVERY_ENABLED,
    "set AKERNEL_RUN_RECOVERY_INTEGRATION=1 with the SDK environment",
)
class SandboxColdRecoveryIntegrationTest(unittest.TestCase):
    def test_reload_without_snapshot_cold_starts_same_logical_sandbox(self):
        sandbox = Sandbox(
            cpu=1000,
            memory=2048,
            runtime=_RUNTIME,
            failover=True,
        )
        try:
            logical_id = sandbox.id
            facades = (sandbox.commands, sandbox.files, sandbox.pty)
            completed = sandbox.commands.run("printf completed-before-cold-start")
            sandbox.files.write("/tmp/cold-start-only", "old-runtime")
            pending = sandbox.commands.run("sleep 60", background=True)

            self.assertIs(sandbox.reload(), True)

            self.assertEqual(sandbox.id, logical_id)
            self.assertEqual((sandbox.commands, sandbox.files, sandbox.pty), facades)
            self.assertEqual(completed.stdout, "completed-before-cold-start")
            self.assertEqual(
                sandbox.commands.run("test ! -e /tmp/cold-start-only").exit_code,
                0,
            )
            self.assertEqual(
                sandbox.commands.run("printf command-after-cold-start").stdout,
                "command-after-cold-start",
            )
            try:
                old_result = pending.wait(timeout=10)
            except BackendOperationError:
                pass
            else:
                self.assertNotEqual(old_result.exit_code, 0)
        finally:
            sandbox.kill()


if __name__ == "__main__":
    unittest.main()
