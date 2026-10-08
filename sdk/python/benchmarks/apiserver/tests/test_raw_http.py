# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from benchmarks.apiserver.raw_http import Case, run_phase


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/objects":
            body = json.dumps({"nodes": []}).encode()
        elif self.path == "/arrays":
            body = b"[]"
        else:
            body = b"missing"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.headers.get("Authorization") != "Bearer test-token":
            body = b"denied"
            self.send_response(401)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RawHttpBenchmarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def test_phase_reuses_raw_http_and_validates_json_object(self) -> None:
        result = run_phase(
            Case("resources", self.url("/objects"), "bearer", "json-object"),
            token="test-token\n",
            concurrency=4,
            warmup_seconds=0.05,
            duration_seconds=0.15,
            timeout_seconds=2,
        )
        self.assertEqual(result["failed"], 0)
        self.assertGreater(result["succeeded"], 0)
        self.assertGreater(result["requests_per_second"], 0)
        self.assertEqual(result["status_codes"], {"200": result["succeeded"]})
        self.assertGreaterEqual(result["latency_ms"]["max"], 0)

    def test_invalid_response_is_reported_without_token(self) -> None:
        result = run_phase(
            Case("instances", self.url("/arrays"), "none", "json-array"),
            token="test-token",
            concurrency=1,
            warmup_seconds=0,
            duration_seconds=0.05,
            timeout_seconds=2,
        )
        self.assertEqual(result["succeeded"], 0)
        self.assertGreater(result["failed"], 0)
        self.assertIn("401", result["status_codes"])
        self.assertNotIn("test-token", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
