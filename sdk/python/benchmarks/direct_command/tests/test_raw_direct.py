# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from benchmarks.direct_command.raw_direct import (
    build_create_body,
    parse_final_event,
    run_phase,
    run_phase_multi,
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: Counter[str] = Counter()

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.__class__.requests[self.path] += 1
        valid = (
            self.path.startswith("/direct/sandbox-")
            and self.headers.get("Authorization") == "Bearer test-token"
            and self.headers.get("X-Auth") == "test-token"
            and body.get("action") == "process.exec"
            and body.get("args", {}).get("cmd") == "true"
            and body.get("requestId") == self.headers.get("X-ADX-Request-ID")
        )
        response = (
            {"exit_code": 0, "stdout": "", "stderr": ""}
            if valid
            else {"error": "invalid request"}
        )
        payload = json.dumps(response).encode()
        self.send_response(200 if valid else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self.__class__.requests[self.path] += 1
        valid = (
            self.path.startswith("/direct/sandbox-")
            and self.path.endswith("/healthz")
            and self.headers.get("Authorization") == "Bearer test-token"
            and self.headers.get("X-Auth") == "test-token"
            and self.headers.get("X-ADX-Request-ID") == self.headers.get("X-Request-Id")
        )
        payload = json.dumps(
            {"status": "ok"} if valid else {"error": "invalid request"}
        ).encode()
        self.send_response(200 if valid else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RawDirectTest(unittest.TestCase):
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
        _Handler.requests.clear()

    def test_create_body_pins_the_runtime_to_one_node(self) -> None:
        body = build_create_body(
            name="direct-perf-1",
            node_id="node-a",
            runtime="runsc",
            cpu=1000,
            memory=2048,
            idle_timeout=900,
        )
        self.assertEqual(body["rootfs"], {"runtime": "runsc"})
        self.assertEqual(body["cpu"], 1000)
        self.assertEqual(body["cpu_limit"], 0)
        self.assertEqual(
            body["dataPlane"],
            {"tunnelSecurityMode": "tls", "portForwardSecurityMode": "tls"},
        )
        self.assertEqual(
            body["scheduleAffinities"],
            [
                {
                    "kind": 0,
                    "affinity": 2,
                    "labelOps": [
                        {
                            "type": 0,
                            "labelKey": "NODE_ID",
                            "labelValues": ["node-a"],
                        }
                    ],
                }
            ],
        )

    def test_final_sse_event_is_required(self) -> None:
        payload = (
            b": keepalive\n\n"
            b'event: progress\ndata: {"status":"scheduling"}\n\n'
            b'event: final\ndata: {"status":"running",\n'
            b'data: "sandboxId":"sandbox-1"}\n\n'
        )
        self.assertEqual(parse_final_event(payload)["sandboxId"], "sandbox-1")
        with self.assertRaisesRegex(ValueError, "final"):
            parse_final_event(b"event: progress\ndata: {}\n\n")

    def test_raw_phase_distributes_commands_across_sandboxes(self) -> None:
        result = run_phase(
            origin=f"http://127.0.0.1:{self.server.server_port}",
            token="test-token\n",
            sandbox_ids=["sandbox-1", "sandbox-2"],
            concurrency=4,
            warmup_seconds=0.05,
            duration_seconds=0.15,
            timeout_seconds=2,
            run_id="unit",
        )
        self.assertEqual(result["failed"], 0)
        self.assertGreater(result["succeeded"], 0)
        self.assertEqual(
            set(result["per_sandbox_succeeded"]), {"sandbox-1", "sandbox-2"}
        )
        self.assertTrue(
            all(value > 0 for value in result["per_sandbox_succeeded"].values())
        )
        self.assertEqual(result["status_codes"], {"200": result["succeeded"]})
        self.assertNotIn("test-token", json.dumps(result))

    def test_raw_phase_can_measure_rrt_health_without_spawning_processes(self) -> None:
        result = run_phase(
            origin=f"http://127.0.0.1:{self.server.server_port}",
            token="test-token\n",
            sandbox_ids=["sandbox-1", "sandbox-2"],
            concurrency=4,
            warmup_seconds=0.05,
            duration_seconds=0.15,
            timeout_seconds=2,
            run_id="unit-health",
            operation="health",
        )
        self.assertEqual(result["operation"], "health")
        self.assertEqual(result["failed"], 0)
        self.assertGreater(result["succeeded"], 0)
        self.assertEqual(
            set(result["per_sandbox_succeeded"]), {"sandbox-1", "sandbox-2"}
        )
        self.assertTrue(all(path.endswith("/healthz") for path in _Handler.requests))

    def test_multiple_generators_report_aggregate_throughput(self) -> None:
        result = run_phase_multi(
            origin=f"http://127.0.0.1:{self.server.server_port}",
            token="test-token\n",
            sandbox_ids=["sandbox-1", "sandbox-2"],
            concurrency=4,
            warmup_seconds=0.05,
            duration_seconds=0.15,
            timeout_seconds=2,
            run_id="unit-multi",
            operation="health",
            generator_processes=2,
        )
        self.assertEqual(result["generator_processes"], 2)
        self.assertEqual(result["concurrency"], 4)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(result["process_results"]), 2)
        self.assertGreater(result["requests_per_second"], 0)


if __name__ == "__main__":
    unittest.main()
