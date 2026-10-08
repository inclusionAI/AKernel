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

"""Opt-in worker-loss acceptance for shared and local-only checkpoints."""

import json
import os
import shlex
import time
import unittest
import uuid

from akernel_sdk import BackendOperationError, Sandbox, resources
from tests.e2e.fault.test_node_loss import _hook, _run_hook
from tests.e2e.full.test_redis_lifecycle import _Redis

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and os.environ.get("AKERNEL_TEST_DESTRUCTIVE_FAULTS") == "1"
    and os.environ.get("AKERNEL_TEST_CHECKPOINT_STORAGE") in {"shared", "local"}
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
    and bool(os.environ.get("AKERNEL_REDIS_HOST"))
)


def require_checkpoint_storage(record, expected):
    point = ((record or {}).get("result") or {}).get("checkpoint") or {}
    artifact = point.get("artifact") or {}
    assert point.get("id") and artifact.get("location"), "checkpoint was not published"
    assert artifact.get("storage") == expected, (
        f"checkpoint storage {artifact.get('storage')!r} does not match {expected!r}"
    )
    return point["id"]


def restored_on_healthy(record, healthy_node, previous_generation):
    assignment = (record or {}).get("assignment") or {}
    result = (record or {}).get("result") or {}
    return (
        result.get("state") == "Running"
        and result.get("resources_held") is True
        and assignment.get("node_id") == healthy_node
        and assignment.get("generation", 0) > previous_generation
    )


def shared_failover_pair(record, candidate_a, candidate_b):
    """Choose the actual source and peer without persisting a source-node pin."""
    groups = (((record or {}).get("spec") or {}).get("scheduling") or {}).get(
        "placement_groups", []
    )
    assert not any(
        group.get("target") == "node" and group.get("required") for group in groups
    ), "shared failover cannot cross a hard node affinity"
    source = ((record or {}).get("assignment") or {}).get("node_id")
    assert source in {candidate_a, candidate_b}, "owner is not a fault candidate"
    return source, candidate_b if source == candidate_a else candidate_a


def local_only_failed(record, lost_node, previous_generation):
    assignment = (record or {}).get("assignment") or {}
    result = (record or {}).get("result") or {}
    return (
        result.get("state") == "Failed"
        and result.get("resources_held") is False
        and assignment.get("node_id") == lost_node
        and assignment.get("generation") == previous_generation
    )


@unittest.skipUnless(
    _ENABLED,
    "requires isolated two-worker checkpoint deployment and explicit fault opt-in",
)
class CheckpointFailoverIntegrationTest(unittest.TestCase):
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
        return json.loads(raw) if raw is not None else None

    def _await_record(self, sandbox_id, condition, description, timeout=180):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self._record(sandbox_id)
            if condition(last):
                return last
            time.sleep(2)
        result = (last or {}).get("result") or {}
        assignment = (last or {}).get("assignment") or {}
        self.fail(
            f"{description} did not converge within {timeout}s; "
            f"state={result.get('state')}, held={result.get('resources_held')}, "
            f"node={assignment.get('node_id')}, "
            f"generation={assignment.get('generation')}"
        )

    def _fixture(self):
        lost_node = os.environ.get("AKERNEL_TEST_FAULT_NODE_ID", "")
        healthy_node = os.environ.get("AKERNEL_TEST_HEALTHY_NODE_ID", "")
        self.assertTrue(lost_node and healthy_node and lost_node != healthy_node)
        stop_hook = _hook("AKERNEL_TEST_STOP_WORKER")
        recover_hook = _hook("AKERNEL_TEST_RECOVER_WORKER")
        return lost_node, healthy_node, stop_hook, recover_hook

    def _checkpoint(self, sandbox, storage):
        socket = os.environ.get(
            "AKERNEL_TEST_CHECKPOINT_SOCKET", "/run/akernel/execd.sock"
        )
        if os.environ.get("AKERNEL_TEST_CHECKPOINT_CLIENT") == "python":
            script = (
                "import socket,sys\n"
                "connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
                "connection.settimeout(300)\n"
                f"connection.connect({socket!r})\n"
                "connection.sendall(\n"
                " b'POST /checkpoint HTTP/1.1\\r\\n'\n"
                " b'Host: localhost\\r\\n'\n"
                " b'Content-Length: 0\\r\\n'\n"
                " b'Connection: close\\r\\n\\r\\n'\n"
                ")\n"
                "response=b''.join(iter(lambda:connection.recv(65536),b''))\n"
                "print(response.decode(errors='replace'))\n"
                "status=int(response.split(b' ',2)[1])\n"
                "sys.exit(0 if 200<=status<300 else 1)\n"
            )
            command = f"python3 -c {shlex.quote(script)}"
        else:
            command = (
                "curl --fail-with-body --silent --show-error "
                f"--unix-socket {shlex.quote(socket)} "
                "--request POST http://localhost/checkpoint"
            )
        response = sandbox.commands.run(command, timeout=300)
        self.assertEqual(response.exit_code, 0, response.stderr)
        self.assertIn('"status":"completed"', response.stdout)
        record = self._await_record(
            sandbox.id,
            lambda item: bool(((item or {}).get("result") or {}).get("checkpoint")),
            "checkpoint publication",
            timeout=30,
        )
        require_checkpoint_storage(record, storage)
        return record

    def _await_rejoin(self, node_id):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                node = next((item for item in resources() if item.id == node_id), None)
            except BackendOperationError:
                node = None
            if node is not None and node.allocatable.get("CPU", 0) >= 1000:
                return
            time.sleep(2)
        self.fail(f"{node_id} did not rejoin after the recovery hook")

    def test_shared_checkpoint_restores_same_id_on_healthy_worker(self):
        if os.environ["AKERNEL_TEST_CHECKPOINT_STORAGE"] != "shared":
            self.skipTest("requires the shared checkpoint deployment profile")
        storage_alias = os.environ.get("AKERNEL_TEST_SHARED_CHECKPOINT_ALIAS", "")
        self.assertTrue(storage_alias and storage_alias != "local")
        candidate_a, candidate_b, stop_hook, recover_hook = self._fixture()
        marker = f"checkpoint-{uuid.uuid4().hex}"
        with Sandbox(
            image=os.environ.get("AKERNEL_TEST_CHECKPOINT_IMAGE") or None,
            runtime=os.environ.get("AKERNEL_TEST_RUNTIME", "runsc"),
            cpu=1000,
            memory=2048,
            failover=True,
        ) as sandbox:
            sandbox_id = sandbox.id
            sandbox.files.write("/tmp/akernel-failover-marker", marker)
            before = self._checkpoint(sandbox, storage_alias)
            generation = before["assignment"]["generation"]
            lost_node, healthy_node = shared_failover_pair(
                before, candidate_a, candidate_b
            )
            print(
                f"shared checkpoint source={lost_node} target={healthy_node} "
                f"generation={generation}",
                flush=True,
            )
            sandbox.files.write(
                "/tmp/akernel-failover-marker", "source-after-checkpoint"
            )
            try:
                _run_hook(stop_hook, lost_node)
                self._await_record(
                    sandbox_id,
                    lambda item: restored_on_healthy(item, healthy_node, generation),
                    "same-ID shared-checkpoint takeover",
                )
            finally:
                _run_hook(recover_hook, lost_node)
            self._await_rejoin(lost_node)
            self.assertEqual(sandbox.id, sandbox_id)
            self.assertTrue(
                restored_on_healthy(self._record(sandbox_id), healthy_node, generation)
            )
            self.assertEqual(sandbox.files.read("/tmp/akernel-failover-marker"), marker)
            self.assertEqual(
                sandbox.commands.run("printf recovered").stdout, "recovered"
            )

    def test_local_only_checkpoint_fails_without_cold_start(self):
        if os.environ["AKERNEL_TEST_CHECKPOINT_STORAGE"] != "local":
            self.skipTest("requires the local-only checkpoint deployment profile")
        lost_node, healthy_node, stop_hook, recover_hook = self._fixture()
        self.assertNotEqual(lost_node, healthy_node)
        with Sandbox(
            image=os.environ.get("AKERNEL_TEST_CHECKPOINT_IMAGE") or None,
            runtime=os.environ.get("AKERNEL_TEST_RUNTIME", "runsc"),
            node_id=lost_node,
            cpu=1000,
            memory=2048,
            failover=True,
        ) as sandbox:
            sandbox_id = sandbox.id
            before = self._checkpoint(sandbox, "local")
            generation = before["assignment"]["generation"]
            try:
                _run_hook(stop_hook, lost_node)
                self._await_record(
                    sandbox_id,
                    lambda item: local_only_failed(item, lost_node, generation),
                    "local-only recovery failure",
                )
            finally:
                _run_hook(recover_hook, lost_node)
            self._await_rejoin(lost_node)
            self.assertTrue(
                local_only_failed(self._record(sandbox_id), lost_node, generation)
            )
            self.assertFalse(sandbox.is_running())


if __name__ == "__main__":
    unittest.main()
