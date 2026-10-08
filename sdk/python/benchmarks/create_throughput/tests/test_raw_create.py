# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from benchmarks.create_throughput.raw_create import build_body, run_create_phase


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    created: set[str] = set()
    create_client_ports: set[int] = set()

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        if self.path != "/api/sandbox/v1/sandboxes":
            self._send(404, b"{}", "application/json")
            return
        sandbox_id = "default-" + body["name"]
        with self.lock:
            self.created.add(sandbox_id)
            self.create_client_ports.add(self.client_address[1])
        payload = (
            'event: progress\ndata: {"status":"scheduling"}\n\n'
            "event: final\ndata: "
            + json.dumps(
                {"status": "running", "sandboxId": sandbox_id, "nodeId": "node-a"}
            )
            + "\n\n"
        ).encode()
        self._send(200, payload, "text/event-stream")

    def do_GET(self) -> None:
        if self.path == "/api/instances":
            with self.lock:
                items = [{"id": value} for value in sorted(self.created)]
            self._send(200, json.dumps(items).encode(), "application/json")
            return
        if self.path.startswith("/direct/") and self.path.endswith("/healthz"):
            sandbox_id = self.path[len("/direct/") : -len("/healthz")]
            with self.lock:
                exists = sandbox_id in self.created
            self._send(
                200 if exists else 404,
                json.dumps(
                    {"status": "ok"} if exists else {"status": "missing"}
                ).encode(),
                "application/json",
            )
            return
        self._send(404, b"{}", "application/json")

    def do_DELETE(self) -> None:
        prefix = "/api/sandbox/v1/sandboxes/"
        if not self.path.startswith(prefix):
            self._send(404, b"{}", "application/json")
            return
        sandbox_id = self.path[len(prefix) :]
        with self.lock:
            self.created.discard(sandbox_id)
        self._send(204, b"", "application/json")


class RawCreateTest(unittest.TestCase):
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

    def setUp(self) -> None:
        with _Handler.lock:
            _Handler.created.clear()
            _Handler.create_client_ports.clear()

    def test_body_can_pin_or_defer_placement(self) -> None:
        pinned = build_body(
            name="create-1",
            target_node="node-a",
            runtime="runsc",
            cpu=100,
            memory=128,
            cpu_limit=1000,
            memory_limit=2048,
            idle_timeout=900,
        )
        automatic = build_body(
            name="create-2",
            target_node="",
            runtime="runsc",
            cpu=100,
            memory=128,
            cpu_limit=0,
            memory_limit=0,
            idle_timeout=900,
        )
        self.assertEqual(
            pinned["scheduleAffinities"][0]["labelOps"][0]["labelValues"],
            ["node-a"],
        )
        self.assertEqual(pinned["cpu_limit"], 1000)
        self.assertEqual(pinned["mem_limit"], 2048)
        self.assertEqual(automatic["cpu_limit"], 0)
        self.assertEqual(automatic["mem_limit"], 0)
        self.assertNotIn("scheduleAffinities", automatic)

    def test_phase_measures_create_then_checks_route_and_cleans_up(self) -> None:
        result = run_create_phase(
            origin=f"http://127.0.0.1:{self.server.server_port}",
            token="test-token",
            run_id="unit",
            count=6,
            concurrency=3,
            target_node="node-a",
            runtime="runsc",
            cpu=100,
            memory=128,
            cpu_limit=1000,
            memory_limit=2048,
            idle_timeout=900,
            timeout_seconds=5,
            cleanup_concurrency=3,
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["attempted"], 6)
        self.assertEqual(result["succeeded"], 6)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["route_ready"], 6)
        self.assertEqual(result["cleanup_succeeded"], 6)
        self.assertGreater(result["creates_per_second"], 0)
        self.assertEqual(result["node_counts"], {"node-a": 6})
        self.assertEqual(result["http_workers"], 3)
        self.assertLessEqual(result["http_connections_opened"], 3)
        with _Handler.lock:
            self.assertEqual(_Handler.created, set())
            self.assertLessEqual(len(_Handler.create_client_ports), 3)


if __name__ == "__main__":
    unittest.main()
