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

"""Public port, reverse-tunnel, and dynamic network-policy contracts."""

import http.server
import os
import shlex
import socketserver
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from akernel_sdk import BackendOperationError, HttpReverseTunnel, NetworkPolicy, Sandbox

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


class _HostHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"REVERSE_TUNNEL_E2E_OK\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class NetworkFeaturesIntegrationTest(unittest.TestCase):
    def test_declared_port_url_and_undeclared_port_rejection(self):
        port = 18081
        with Sandbox(
            runtime=_RUNTIME,
            cpu=1000,
            memory=2048,
            port_forwardings=[port],
        ) as sandbox:
            with self.assertRaises(ValueError):
                sandbox.get_port_url(port + 1)
            self.assertTrue(
                sandbox.get_port_url(port).endswith(f"/{sandbox.id}/{port}")
            )

    def test_internal_port_url_produces_an_edge_address(self):
        port = 18082
        with Sandbox(
            runtime=_RUNTIME,
            cpu=1000,
            memory=2048,
            port_forwardings=[port],
        ) as sandbox:
            public = urlsplit(sandbox.get_port_url(port))
            internal = urlsplit(sandbox.get_port_url(port, internal=True))
            self.assertEqual(internal.path, public.path)
            self.assertIn(internal.scheme, {"http", "https"})
            self.assertTrue(internal.hostname)

    def test_reverse_tunnel_reaches_sdk_host_service(self):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _HostHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        tunnel = HttpReverseTunnel(
            target=f"http://127.0.0.1:{server.server_address[1]}",
            reverse_port=18765,
            listen_port=18766,
        )
        try:
            with Sandbox(
                runtime=_RUNTIME, cpu=1000, memory=2048, reverse_tunnel=tunnel
            ) as sandbox:
                probe = (
                    "exec 3<>/dev/tcp/127.0.0.1/18766; "
                    "printf 'GET /health HTTP/1.1\\r\\nHost: localhost\\r\\n"
                    "Connection: close\\r\\n\\r\\n' >&3; "
                    "while IFS= read -r line <&3; do "
                    'case "$line" in *REVERSE_TUNNEL_E2E_OK*) '
                    "printf REVERSE_TUNNEL_E2E_OK; exit 0;; esac; done; exit 1"
                )
                result = sandbox.commands.run(
                    "bash -c " + shlex.quote(probe), timeout=20
                )
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(result.stdout, "REVERSE_TUNNEL_E2E_OK")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _assert_tunnel(self, sandbox, listen_port):
        # The owning SDK actively reconnects after a successful policy update.
        # Keep this below the default heartbeat interval to catch regressions.
        probe = (
            "import urllib.request; "
            f"r=urllib.request.urlopen('http://127.0.0.1:{listen_port}/health', "
            "timeout=3); "
            "assert r.read() == b'REVERSE_TUNNEL_E2E_OK\\n'"
        )
        started = time.monotonic()
        deadline = started + 10
        while True:
            result = sandbox.commands.run("python3 -c " + shlex.quote(probe), timeout=8)
            if result.exit_code == 0:
                finished = time.monotonic()
                self.assertLess(finished, deadline, "tunnel recovery exceeded 10s")
                print(
                    f"tunnel ready: listen_port={listen_port} "
                    f"elapsed={finished - started:.3f}s",
                    flush=True,
                )
                return
            if time.monotonic() >= deadline:
                self.fail(f"reverse tunnel did not recover: {result.stderr}")
            time.sleep(0.2)

    def _assert_blocked_services(self, sandbox, published_port):
        self.assertEqual(sandbox.commands.run("printf CONTROL_OK").stdout, "CONTROL_OK")
        sandbox.files.write("/tmp/network-policy-marker", "FILES_OK")
        self.assertEqual(sandbox.files.read("/tmp/network-policy-marker"), "FILES_OK")
        output = bytearray()
        with sandbox.pty.create(on_data=output.extend) as session:
            session.send_stdin(b"printf 'BLOCK_PTY_OK\\n'\nexit 0\n")
            self.assertEqual(session.wait(timeout=15), 0)
        self.assertIn(b"BLOCK_PTY_OK", output)
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 15
        while True:
            try:
                with opener.open(
                    sandbox.get_port_url(published_port) + "/network-policy-marker",
                    timeout=3,
                ) as response:
                    self.assertEqual(response.read(), b"FILES_OK")
                break
            except (HTTPError, URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        # A bounded socket timeout is distinct from an API/command deadline.
        denied = sandbox.commands.run(
            "python3 -c "
            + shlex.quote(
                "import socket; s=socket.socket(); s.settimeout(2); "
                "s.connect(('1.1.1.1',53))"
            ),
            timeout=5,
        )
        self.assertNotEqual(denied.exit_code, 0, "block allowed a new outbound flow")

    def _exercise_blocked_tunnel(self, listen_port, initially_blocked):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _HostHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        target = f"http://127.0.0.1:{server.server_address[1]}"
        tunnel = (
            HttpReverseTunnel(target=target)
            if listen_port == 8766
            else HttpReverseTunnel(
                target=target, reverse_port=listen_port - 1, listen_port=listen_port
            )
        )
        published_port = 18083
        try:
            with Sandbox(
                image=os.environ.get("AKERNEL_TEST_HTTP_IMAGE", "python:3.12-slim"),
                runtime=_RUNTIME,
                cpu=1000,
                memory=2048,
                reverse_tunnel=tunnel,
                port_forwardings=[published_port],
                network_policy=NetworkPolicy.block() if initially_blocked else None,
            ) as sandbox:
                http = sandbox.commands.run(
                    f"python3 -m http.server {published_port} "
                    "--bind 0.0.0.0 --directory /tmp",
                    background=True,
                )
                try:
                    self._assert_tunnel(sandbox, listen_port)
                    # Includes initial deny, unrestricted -> deny, deny -> allow,
                    # and a second deny after clearing the same live tunnel.
                    for policy in (NetworkPolicy.block(), None, NetworkPolicy.block()):
                        sandbox.update_network_policy(policy)
                        self._assert_tunnel(sandbox, listen_port)
                        if policy is not None:
                            self._assert_blocked_services(sandbox, published_port)
                    # Updates can arrive before the asynchronous reconnect has
                    # completed. The final acknowledged policy must still work
                    # with the same public handle and no manual tunnel restart.
                    for policy in (
                        None,
                        NetworkPolicy.block(),
                        NetworkPolicy(),
                        NetworkPolicy.block(),
                    ):
                        sandbox.update_network_policy(policy)
                    self._assert_tunnel(sandbox, listen_port)
                    self._assert_blocked_services(sandbox, published_port)
                finally:
                    http.kill()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_reverse_tunnel_with_creation_time_block(self):
        for listen_port in (8766, 18766):
            with self.subTest(listen_port=listen_port):
                self._exercise_blocked_tunnel(listen_port, initially_blocked=True)

    def test_reverse_tunnel_survives_block_clear_and_reblock(self):
        for listen_port in (8766, 18766):
            with self.subTest(listen_port=listen_port):
                self._exercise_blocked_tunnel(listen_port, initially_blocked=False)

    def test_dynamic_block_and_clear_preserve_control_operations(self):
        with Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048) as sandbox:
            sandbox.update_network_policy(NetworkPolicy.block())
            try:
                blocked = sandbox.commands.run(
                    "bash -c 'exec 3<>/dev/tcp/1.1.1.1/53'", timeout=8
                )
            except BackendOperationError as error:
                cause = error.__cause__
                processing_deadline = (
                    getattr(cause, "status_code", None) == 504
                    and getattr(cause, "code", None) == "PROCESSING_DEADLINE_EXCEEDED"
                )
                self.assertTrue(
                    "timed out" in str(error).lower() or processing_deadline,
                    str(error),
                )
            else:
                self.assertNotEqual(blocked.exit_code, 0)
            self.assertEqual(
                sandbox.commands.run("printf CONTROL_OK").stdout, "CONTROL_OK"
            )
            sandbox.update_network_policy(None)
            self.assertEqual(
                sandbox.commands.run("printf CLEARED_OK").stdout, "CLEARED_OK"
            )


if __name__ == "__main__":
    unittest.main()
