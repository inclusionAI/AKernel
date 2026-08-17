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

"""Let a sandbox call an HTTP service on the SDK machine."""

import http.server
import shlex
import socketserver
import threading

from akernel_sdk import HttpReverseTunnel, Sandbox


class Handler(http.server.BaseHTTPRequestHandler):
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


def main() -> None:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    local_port = server.server_address[1]

    tunnel = HttpReverseTunnel(
        target=f"http://127.0.0.1:{local_port}",
        reverse_port=8765,
        listen_port=8766,
    )
    try:
        with Sandbox(
            cpu=1000,
            memory=2048,
            reverse_tunnel=tunnel,
        ) as sandbox:
            probe = (
                "exec 3<>/dev/tcp/127.0.0.1/8766; "
                "printf 'GET /health HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n"
                "Connection: close\\r\\n\\r\\n' >&3; "
                "while IFS= read -r line <&3; do "
                "case \"$line\" in "
                "*AKERNEL_REVERSE_TUNNEL_OK*) "
                "printf 'AKERNEL_REVERSE_TUNNEL_OK\\n'; exit 0;; "
                "esac; "
                "done; "
                "exit 1"
            )
            result = sandbox.commands.run(
                "bash -c " + shlex.quote(probe),
                timeout=20,
            )
            assert result.exit_code == 0, result.stderr
            assert result.stdout.strip() == "AKERNEL_REVERSE_TUNNEL_OK"
            print(f"{sandbox.reverse_tunnel.url}/health -> {result.stdout.strip()}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
