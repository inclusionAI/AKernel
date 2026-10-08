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
import unittest
from urllib.parse import urlsplit

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

    def test_dynamic_block_and_clear_preserve_control_operations(self):
        with Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048) as sandbox:
            sandbox.update_network_policy(NetworkPolicy.block())
            try:
                blocked = sandbox.commands.run(
                    "bash -c 'exec 3<>/dev/tcp/1.1.1.1/53'", timeout=8
                )
            except BackendOperationError as error:
                self.assertIn("timed out", str(error).lower())
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
