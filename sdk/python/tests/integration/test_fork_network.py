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

"""Real fork rebindings: N tunnels, private published ports and captured policy.

Uses the same integration opt-in/image as test_fork. The configured external
TCP fixture must be reachable before deny checks; an unreachable baseline is a
failure, not evidence of policy enforcement. No cluster configuration is changed.
"""

import http.server
import json
import os
import shlex
import ssl
import threading
import time
import unittest
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from akernel_sdk import HttpReverseTunnel, NetworkPolicy, Sandbox
from tests.integration import test_fork


@unittest.skipUnless(
    test_fork._ENABLED and test_fork._IMAGE,
    "enable integration and set a python3 image",
)
class ForkNetworkIntegrationTest(test_fork.ForkIntegrationFixture, unittest.TestCase):
    def setUp(self):
        self.host_nonce = uuid.uuid4().hex
        nonce = self.host_nonce

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"nonce": nonce, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.listen_port = 18766 if "creation_policy" in self._testMethodName else 8766
        self.published_port = 18091
        self.sandbox_options = dict(
            reverse_tunnel=HttpReverseTunnel(
                target=f"http://127.0.0.1:{self.server.server_port}",
                listen_port=self.listen_port,
                reverse_port=self.listen_port - 1,
            ),
            port_forwardings=[self.published_port],
            network_policy=(
                NetworkPolicy.block()
                if "creation_policy" in self._testMethodName
                else None
            ),
        )
        super().setUp()
        self.source.files.write("/tmp/fork-network-marker", "source")
        self.source.commands.run(
            f"python3 -m http.server {self.published_port} "
            "--bind 0.0.0.0 --directory /tmp",
            background=True,
        )
        self.opener = build_opener(
            ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context())
        )

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive(), "host service thread leaked")

    def assert_tunnel(self, sandbox):
        path = "/fork/" + sandbox.id
        script = (
            "import json,urllib.request; "
            "op=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
            f"r=op.open('http://127.0.0.1:{self.listen_port}{path}',timeout=3); "
            "print(r.read().decode())"
        )
        deadline = time.monotonic() + 20
        while True:
            result = sandbox.commands.run(
                "python3 -c " + shlex.quote(script), timeout=8
            )
            if result.exit_code == 0:
                self.assertEqual(
                    json.loads(result.stdout), {"nonce": self.host_nonce, "path": path}
                )
                return
            if time.monotonic() >= deadline:
                self.fail(f"{sandbox.id}: reverse tunnel failed: {result.stderr}")
            time.sleep(0.2)

    def assert_port(self, sandbox, expected):
        request = Request(
            sandbox.get_port_url(self.published_port) + "/fork-network-marker",
            headers={"X-Auth": os.environ["AKERNEL_TOKEN"]},
        )
        deadline = time.monotonic() + 20
        while True:
            try:
                with self.opener.open(request, timeout=3) as response:
                    self.assertEqual(response.read().decode(), expected)
                return
            except (HTTPError, URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    def assert_egress(self, sandbox, allowed):
        host = os.environ.get("AKERNEL_TEST_EGRESS_HOST", "1.1.1.1")
        port = int(os.environ.get("AKERNEL_TEST_EGRESS_PORT", "443"))
        script = (
            "import socket,sys\n"
            "try:\n"
            f" s=socket.create_connection(({host!r},{port}),timeout=3); s.close()\n"
            "except OSError:\n"
            " print('EGRESS_DENIED'); sys.exit(3)\n"
            "print('EGRESS_ALLOWED')\n"
        )
        result = sandbox.commands.run("python3 -c " + shlex.quote(script), timeout=8)
        self.assertEqual(result.exit_code, 0 if allowed else 3, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "EGRESS_ALLOWED" if allowed else "EGRESS_DENIED"
        )

    def fork_two(self):
        results = self.remember(self.source.fork(count=2))
        self.assertTrue(all(isinstance(result, Sandbox) for result in results), results)
        self.assertEqual(len(self.snapshot_ids), 1)
        for index, child in enumerate(results):
            child.files.write("/tmp/fork-network-marker", f"child-{index}")
            self.assertEqual(child.commands.run("pwd").stdout.strip(), "/tmp")
            self.assert_tunnel(child)
            self.assert_port(child, f"child-{index}")
        self.assert_tunnel(self.source)
        self.assert_port(self.source, "source")
        return results

    def test_fork_rebinds_tunnels_and_private_published_ports(self):
        children = self.fork_two()
        self.source.kill()
        for index, child in enumerate(children):
            self.assert_tunnel(child)
            self.assert_port(child, f"child-{index}")
        print(
            "proof:",
            json.dumps(
                {
                    "case": "fork_network_rebinding",
                    "children": 2,
                    "listen_port": self.listen_port,
                    "tunnel_per_child": "passed",
                    "published_port_private_writes": "passed",
                    "cwd_inherited": "passed",
                    "children_survive_source_delete": True,
                }
            ),
            flush=True,
        )

    def test_fork_inherits_updated_policy_and_child_clear_is_isolated(self):
        self.assert_egress(self.source, True)
        self.source.update_network_policy(NetworkPolicy.block())
        self.assert_tunnel(self.source)
        self.assert_egress(self.source, False)
        first, second = self.fork_two()
        for child in (first, second):
            self.assert_egress(child, False)
        first.update_network_policy(None)
        self.assert_tunnel(first)
        self.assert_port(first, "child-0")
        self.assert_egress(first, True)
        self.assert_egress(second, False)
        self.assert_egress(self.source, False)
        print(
            "proof:",
            json.dumps(
                {
                    "case": "fork_updated_policy",
                    "children": 2,
                    "reachable_allow_baseline": True,
                    "captured_block_inherited": True,
                    "child_clear_isolated": True,
                    "tunnel_and_ports_under_block": "passed",
                }
            ),
            flush=True,
        )

    def test_fork_inherits_creation_policy_with_custom_tunnel(self):
        control = Sandbox(
            image=test_fork._IMAGE,
            memory=1024,
            cpu=1000,
            idle_timeout=600,
            runtime=os.environ.get("AKERNEL_TEST_RUNTIME", "runsc"),
        )
        self.handles.append(control)
        print("owned control:", control.id, flush=True)
        self.assert_egress(control, True)
        self.assert_egress(self.source, False)
        children = self.fork_two()
        for child in children:
            self.assert_egress(child, False)
        print(
            "proof:",
            json.dumps(
                {
                    "case": "fork_creation_policy",
                    "children": 2,
                    "listen_port": self.listen_port,
                    "reachable_allow_baseline": True,
                    "captured_block_inherited": True,
                    "tunnel_and_ports_under_block": "passed",
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
