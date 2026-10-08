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

"""Validate physical ownership and released resources against deployed Redis.

This suite intentionally requires direct test-only Redis access. It does not
print credentials, keys, or environment IDs. A released environment is removed
from the persistent catalog; a terminal record that still holds resources is a
leak.
"""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import uuid

from akernel_sdk import Sandbox, resources

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
    and bool(os.environ.get("AKERNEL_REDIS_HOST"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


class _Redis:
    def __init__(self):
        host = os.environ["AKERNEL_REDIS_HOST"]
        port = int(os.environ.get("AKERNEL_REDIS_PORT", "6379"))
        self.socket = socket.create_connection((host, port), timeout=5)
        self.socket.settimeout(5)
        self.reader = self.socket.makefile("rb")
        password = os.environ.get("AKERNEL_REDIS_PASSWORD")
        if password:
            self.call("AUTH", password)

    def close(self):
        self.reader.close()
        self.socket.close()

    def call(self, *args):
        payload = [f"*{len(args)}\r\n".encode()]
        for value in args:
            encoded = str(value).encode()
            payload.extend((f"${len(encoded)}\r\n".encode(), encoded, b"\r\n"))
        self.socket.sendall(b"".join(payload))
        return self._read()

    def _read(self):
        line = self.reader.readline()
        kind, value = line[:1], line[1:-2]
        if kind == b"+":
            return value
        if kind == b"-":
            raise RuntimeError("Redis command failed")
        if kind == b":":
            return int(value)
        if kind == b"$":
            size = int(value)
            return None if size < 0 else self.reader.read(size + 2)[:-2]
        if kind == b"*":
            return [self._read() for _ in range(int(value))]
        raise RuntimeError("Invalid Redis response")


@unittest.skipUnless(
    _ENABLED, "requires SDK credentials and AKERNEL_REDIS_HOST test access"
)
class RedisLifecycleIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.redis = _Redis()
        namespace = os.environ.get("AKERNEL_REDIS_NAMESPACE", "akernel")
        cls.control_key = f"adx:{{{namespace}}}:control:v1"

    @classmethod
    def tearDownClass(cls):
        cls.redis.close()

    def _record(self, sandbox_id):
        raw = self.redis.call("HGET", self.control_key, f"environment:{sandbox_id}")
        return json.loads(raw) if raw else None

    def _await_state(self, sandbox_id, state, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self._record(sandbox_id)
            if (
                record is not None
                and (record.get("result") or {}).get("state") == state
            ):
                return record
            time.sleep(0.25)
        self.fail(f"environment record did not reach {state} within {timeout}s")

    def _await_absent(self, sandbox_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._record(sandbox_id) is None:
                return
            time.sleep(0.25)
        self.fail(f"environment record was not removed within {timeout}s")

    def test_node_pin_and_delete_release_persistent_allocation(self):
        nodes = [
            item.id
            for item in resources()
            if item.allocatable.get("CPU", 0) >= 1000
            and item.allocatable.get("Memory", 0) >= 2048
        ]
        self.assertTrue(nodes, "no schedulable node")
        sandbox = None
        try:
            sandbox = Sandbox(
                runtime=_RUNTIME,
                node_id=nodes[0],
                cpu=1000,
                memory=2048,
                cpu_limit=1500,
                mem_limit=3072,
            )
            running = self._await_state(sandbox.id, "Running")
            self.assertEqual(running["assignment"]["node_id"], nodes[0])
            self.assertTrue(running["result"]["resources_held"])
            limits = running["spec"]["sandbox"]["limits"]
            self.assertEqual(limits["cpu_millis"], 1500)
            self.assertEqual(limits["memory_bytes"], 3072 * 1048576)
            self.assertEqual(
                sandbox.commands.run("printf redis-e2e").stdout, "redis-e2e"
            )
        finally:
            if sandbox is not None:
                sandbox.kill()
        if sandbox is not None:
            self._await_absent(sandbox.id)

    def test_idle_reclamation_releases_persistent_allocation(self):
        name = f"e2e-idle-{uuid.uuid4().hex[:12]}"
        sandbox = None
        try:
            sandbox = Sandbox(
                name=name,
                detached=True,
                runtime=_RUNTIME,
                cpu=1000,
                memory=2048,
                idle_timeout=2,
            )
            sandbox_id = sandbox.id
            self._await_state(sandbox_id, "Running")
            self._await_absent(sandbox_id, timeout=60)
        finally:
            if sandbox is not None:
                sandbox.kill()
                Sandbox.delete(name)

    def test_activity_extends_idle_lifetime_then_reclaims_after_quiet(self):
        name = f"e2e-idle-active-{uuid.uuid4().hex[:12]}"
        sandbox = None
        try:
            sandbox = Sandbox(
                name=name,
                detached=True,
                runtime=_RUNTIME,
                cpu=1000,
                memory=2048,
                idle_timeout=5,
            )
            sandbox_id = sandbox.id
            self._await_state(sandbox_id, "Running")
            for sequence in range(8):
                result = sandbox.commands.run(f"printf active-{sequence}")
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(result.stdout, f"active-{sequence}")
                self.assertEqual(self._record(sandbox_id)["result"]["state"], "Running")
                time.sleep(1)

            self._await_absent(sandbox_id, timeout=60)
        finally:
            if sandbox is not None:
                sandbox.kill()
                Sandbox.delete(name)

    def test_client_exit_reclaims_idle_sandbox_with_unfinished_command(self):
        name = f"e2e-idle-exit-{uuid.uuid4().hex[:12]}"
        child = """
import json
import os
import sys
from akernel_sdk import Sandbox

sandbox = Sandbox(name=sys.argv[1], detached=True, runtime=sys.argv[2],
                  cpu=1000, memory=2048, idle_timeout=3)
handle = sandbox.commands.run('sleep 300', background=True)
assert any(item.pid == handle.pid and item.running
           for item in sandbox.commands.list()), 'background command did not start'
print(json.dumps({'id': sandbox.id, 'pid': handle.pid}), flush=True)
os._exit(0)
"""
        sandbox_id = None
        try:
            result = subprocess.run(
                [sys.executable, "-c", child, name, _RUNTIME],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            started = json.loads(result.stdout)
            sandbox_id = started["id"]
            self.assertGreater(started["pid"], 0)
            running = self._record(sandbox_id)
            self.assertEqual(running["result"]["state"], "Running")
            self._await_absent(sandbox_id, timeout=45)
        finally:
            if sandbox_id is not None:
                Sandbox.delete(name)


if __name__ == "__main__":
    unittest.main()
