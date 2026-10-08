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

"""Sever the create TCP response after the real API confirms the result."""

from __future__ import annotations

import json
import os
import socket
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx

from akernel_sdk import Sandbox
from akernel_sdk._addresses import api_endpoint_from_env

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
    and bool(os.environ.get("AKERNEL_REDIS_HOST"))
    and os.environ.get("AKERNEL_TEST_DESTRUCTIVE_CLAIMS") == "1"
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")
_CREATE_PATH = "/api/sandbox/v1/sandboxes"


def _running_final_id(body: bytes) -> str | None:
    """Recognize an authoritative running SSE final before dropping its reply."""
    event = ""
    data_lines: list[str] = []
    for line in body.decode("utf-8").splitlines() + [""]:
        if not line:
            if event == "final" and data_lines:
                try:
                    result = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    return None
                if result.get("status") == "running":
                    return result.get("sandboxId") or result.get("instanceId")
            event = ""
            data_lines = []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    return None


class _DisconnectProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream: str):
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.upstream = upstream
        self.lock = threading.Lock()
        self.create_attempts: list[tuple[str, str]] = []
        self.dropped_final_id: str | None = None
        self.upstream_errors: list[str] = []


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # Headers can contain credentials; never log proxied requests.
        pass

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def _forward(self) -> None:
        proxy: _DisconnectProxy = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            not in {
                "host",
                "content-length",
                "connection",
                "transfer-encoding",
                "accept-encoding",
            }
        }
        headers["Accept-Encoding"] = "identity"
        is_create = self.command == "POST" and self.path == _CREATE_PATH
        if is_create:
            request_name = json.loads(body or b"{}").get("name", "")
            with proxy.lock:
                proxy.create_attempts.append(
                    (self.headers.get("X-Request-Id", ""), request_name)
                )
        try:
            with httpx.Client(verify=False, trust_env=False, timeout=120) as client:
                response = client.request(
                    self.command,
                    f"{proxy.upstream}{self.path}",
                    headers=headers,
                    content=body,
                )
        except httpx.HTTPError as error:
            with proxy.lock:
                proxy.upstream_errors.append(type(error).__name__)
            self.send_error(502)
            return

        final_id = (
            _running_final_id(response.content)
            if is_create and response.status_code == 200
            else None
        )
        with proxy.lock:
            drop_response = final_id is not None and proxy.dropped_final_id is None
            if drop_response:
                proxy.dropped_final_id = final_id
        if drop_response:
            # The server has completed the create. The SDK observes a real
            # connection failure without seeing even one byte of its reply.
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return

        self.send_response(response.status_code)
        content_type = response.headers.get("Content-Type")
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)


@unittest.skipUnless(
    _ENABLED,
    "requires isolated deployment, AKERNEL_REDIS_HOST, and "
    "AKERNEL_TEST_DESTRUCTIVE_CLAIMS=1",
)
class CreateTransportDisconnectIntegrationTest(unittest.TestCase):
    def test_lost_tcp_reply_retries_same_create(self):
        upstream = api_endpoint_from_env().base_url()
        proxy = _DisconnectProxy(upstream)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        name = f"e2e-disconnect-{uuid.uuid4().hex[:12]}"
        sandbox = None
        try:
            with patch.dict(
                os.environ,
                {"AKERNEL_SERVER_ADDRESS": f"http://127.0.0.1:{proxy.server_port}"},
            ):
                try:
                    sandbox = Sandbox(
                        name=name,
                        detached=True,
                        runtime=_RUNTIME,
                        cpu=1000,
                        memory=2048,
                    )
                    self.assertEqual(sandbox.id, proxy.dropped_final_id)
                    self.assertEqual(
                        sandbox.commands.run("printf connected").stdout,
                        "connected",
                    )
                    self.assertEqual(len(proxy.create_attempts), 2)
                    self.assertEqual(proxy.create_attempts[0], proxy.create_attempts[1])
                    self.assertTrue(proxy.create_attempts[0][0].startswith("create-"))
                    self.assertEqual(proxy.create_attempts[0][1], name)
                    self.assertFalse(proxy.upstream_errors)
                finally:
                    if sandbox is not None:
                        sandbox.kill()
                    Sandbox.delete(name)
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
