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

import http.server
import os
import socketserver
import threading
import time
import unittest

from akernel_sdk import HttpReverseTunnel, Sandbox
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

_INSTALL_CURL_COMMAND = (
    "apt-get update && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl"
)

_CHECKPOINT_COMMAND = (
    "curl --fail-with-body --silent --show-error "
    "--unix-socket /run/akernel/rrt.sock "
    "--request POST http://localhost/checkpoint"
)

_REVERSE_TUNNEL_PROBE = (
    "curl --fail --silent --show-error --max-time 10 "
    "http://127.0.0.1:8766/health"
)


class _ReverseTunnelHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b"AKERNEL_REVERSE_TUNNEL_OK\n"
            self.send_response(200)
        else:
            body = b"not found\n"
            self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


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
    def _wait_for_reverse_tunnel(self, sandbox, *, timeout=60):
        deadline = time.monotonic() + timeout
        last_result = None
        while time.monotonic() < deadline:
            last_result = sandbox.commands.run(_REVERSE_TUNNEL_PROBE, timeout=20)
            if (
                last_result.exit_code == 0
                and last_result.stdout.strip() == "AKERNEL_REVERSE_TUNNEL_OK"
            ):
                return
            time.sleep(0.5)

        assert last_result is not None
        self.fail(
            "reverse tunnel did not recover: "
            f"exit_code={last_result.exit_code}, "
            f"stdout={last_result.stdout!r}, stderr={last_result.stderr!r}"
        )

    def test_internal_checkpoint_reload_and_reverse_tunnel(self):
        server = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0),
            _ReverseTunnelHandler,
        )
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        tunnel = HttpReverseTunnel(
            target=f"http://127.0.0.1:{server.server_address[1]}",
            reverse_port=8765,
            listen_port=8766,
        )
        sandbox = None
        try:
            sandbox = Sandbox(
                cpu=1000,
                memory=2048,
                storage_mb=256,
                runtime=_RUNTIME,
                reverse_tunnel=tunnel,
                failover=True,
            )
            sandbox_id = sandbox.id
            commands = sandbox.commands
            files = sandbox.files
            pty = sandbox.pty
            self.assertIs(sandbox.reverse_tunnel, tunnel)
            install_curl = sandbox.commands.run(_INSTALL_CURL_COMMAND, timeout=300)
            self.assertEqual(install_curl.exit_code, 0, install_curl.stderr)
            self._wait_for_reverse_tunnel(sandbox)
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
            self.assertIs(sandbox.reverse_tunnel, tunnel)
            restored_value = sandbox.commands.run("cat /tmp/akernel-checkpoint-state")
            self.assertEqual(restored_value.exit_code, 0)
            self.assertEqual(restored_value.stdout, "checkpoint-before")
            self._wait_for_reverse_tunnel(sandbox)
            network = sandbox.commands.run(
                "curl --fail --silent --show-error --max-time 10 "
                "--output /dev/null https://example.com/",
                timeout=30,
            )
            self.assertEqual(network.exit_code, 0, network.stderr)
        finally:
            if sandbox is not None:
                sandbox.kill()
            server.shutdown()
            server.server_close()


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
            with self.assertRaisesRegex(
                BackendOperationError,
                "pre-reload command handle was not restored after sandbox cold start",
            ):
                pending.wait(timeout=10)
        finally:
            sandbox.kill()


if __name__ == "__main__":
    unittest.main()
