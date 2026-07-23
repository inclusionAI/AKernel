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

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


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


if __name__ == "__main__":
    unittest.main()
