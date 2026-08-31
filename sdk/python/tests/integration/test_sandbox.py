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

_CHECKPOINT_COMMAND = r"""python3 - <<'PY'
import socket

request = (
    b"POST /checkpoint HTTP/1.1\r\n"
    b"Host: localhost\r\n"
    b"Content-Length: 0\r\n"
    b"Connection: close\r\n\r\n"
)
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(300)
    client.connect("/run/akernel/rrt.sock")
    client.sendall(request)
    response = bytearray()
    while True:
        chunk = client.recv(4096)
        if not chunk:
            break
        response.extend(chunk)

status = bytes(response).split(b"\r\n", 1)[0]
if b" 200 " not in status:
    raise RuntimeError(bytes(response).decode("utf-8", "replace"))
print(bytes(response).rsplit(b"\r\n\r\n", 1)[-1].decode())
PY"""


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
class SandboxReloadIntegrationTest(unittest.TestCase):
    def test_internal_checkpoint_and_reload(self):
        sandbox = Sandbox(
            cpu=1000,
            memory=2048,
            runtime=_RUNTIME,
            failover=True,
        )
        try:
            sandbox_id = sandbox.id
            commands = sandbox.commands
            files = sandbox.files
            pty = sandbox.pty
            created = sandbox.commands.run(
                "printf checkpoint-before > /tmp/akernel-checkpoint-state"
            )
            self.assertEqual(created.exit_code, 0)

            checkpoint = sandbox.commands.run(_CHECKPOINT_COMMAND, timeout=300)
            self.assertEqual(checkpoint.exit_code, 0, checkpoint.stderr)
            self.assertIn('"status":"completed"', checkpoint.stdout)
            self.assertTrue(sandbox.is_running())

            changed = sandbox.commands.run(
                "printf source-after > /tmp/akernel-checkpoint-state"
            )
            self.assertEqual(changed.exit_code, 0)

            self.assertTrue(sandbox.reload())
            self.assertEqual(sandbox.id, sandbox_id)
            self.assertIs(sandbox.commands, commands)
            self.assertIs(sandbox.files, files)
            self.assertIs(sandbox.pty, pty)
            restored_value = sandbox.commands.run(
                "cat /tmp/akernel-checkpoint-state"
            )
            self.assertEqual(restored_value.exit_code, 0)
            self.assertEqual(restored_value.stdout, "checkpoint-before")
            network = sandbox.commands.run(
                "python3 -c 'import socket; "
                "s=socket.create_connection((\"example.com\", 443), 10); "
                "s.close()'",
                timeout=30,
            )
            self.assertEqual(network.exit_code, 0, network.stderr)
        finally:
            sandbox.kill()


if __name__ == "__main__":
    unittest.main()
