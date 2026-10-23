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

"""Opt-in real snapshot/fork isolation and cleanup acceptance.

Set AKERNEL_RUN_INTEGRATION=1, AKERNEL_* connection settings and
AKERNEL_TEST_IMAGE to an image containing python3. Failure injection changes
only this client's calls; it does not alter the cluster or other workloads.
"""

import json
import os
import time
import unittest
import uuid
from unittest.mock import patch

from akernel_sdk import BackendOperationError, ForkCleanupError, Sandbox
from akernel_sdk._backends import adx

_ENABLED = os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
_IMAGE = os.environ.get("AKERNEL_TEST_IMAGE")
_WORKER = """
import json, os, secrets, time
from pathlib import Path
root = Path('/tmp/fork-acceptance')
fd = (root / 'data').open('r')
fd.read(7)
nonce = secrets.token_hex(16)
value = 0
previous = None
while True:
    try:
        request = json.loads((root / 'request').read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        time.sleep(0.02)
        continue
    if request['id'] != previous:
        if request['action'] == 'advance':
            value += 1
            fd.read(4)
        response = dict(id=request['id'], pid=os.getpid(), nonce=nonce,
                        value=value, offset=fd.tell())
        temporary = root / 'reply.tmp'
        temporary.write_text(json.dumps(response))
        temporary.replace(root / 'reply')
        previous = request['id']
    time.sleep(0.02)
"""


class ForkIntegrationFixture:
    def setUp(self):
        self.handles = []
        self.snapshot_ids = []
        self.addCleanup(self.cleanup_resources)
        options = dict(
            image=_IMAGE,
            runtime=os.environ.get("AKERNEL_TEST_RUNTIME", "runsc"),
            cpu=1000,
            memory=1024,
            storage_mb=256,
            idle_timeout=600,
            schedule_timeout=180,
            cwd="/tmp",
        )
        options.update(getattr(self, "sandbox_options", {}))
        source = Sandbox(**options)
        self.source = source
        self.handles.append(source)
        probe = source.commands.run("python3 --version")
        self.assertEqual(probe.exit_code, 0, probe.stderr)
        self.assertIn("Python 3.", probe.stdout)
        original = source._session.create_snapshot

        def capture(name, timeout):
            snapshot = original(name, timeout)
            self.snapshot_ids.append(snapshot.snapshot_id)
            print("owned snapshot:", snapshot.snapshot_id, flush=True)
            return snapshot

        patcher = patch.object(source._session, "create_snapshot", side_effect=capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        print("owned source:", source.id, flush=True)

    def remember(self, results):
        for result in results:
            if isinstance(result, Sandbox):
                self.handles.append(result)
                print("owned child:", result.id, flush=True)
        return results

    def cleanup_resources(self):
        errors = []
        for handle in reversed(self.handles):
            try:
                handle.kill()
            except Exception as error:
                errors.append(error)
        for identity in self.snapshot_ids:
            try:
                Sandbox.delete_snapshot(identity)
                deadline = time.monotonic() + 40
                while True:
                    try:
                        Sandbox.get_snapshot(identity)
                    except BackendOperationError as error:
                        if error.status_code == 404:
                            break
                        raise
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"snapshot not collected: {identity}")
                    time.sleep(0.5)
            except Exception as error:
                errors.append(error)
        if errors:
            raise RuntimeError("owned fork resources did not clean up") from errors[0]
        print("owned handles and snapshots cleaned", flush=True)


@unittest.skipUnless(_ENABLED and _IMAGE, "enable integration and set a python3 image")
class ForkIntegrationTest(ForkIntegrationFixture, unittest.TestCase):
    def witness(self, sandbox, action="inspect"):
        request = {"id": uuid.uuid4().hex, "action": action}
        sandbox.files.write("/tmp/fork-acceptance/request", json.dumps(request))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if sandbox.files.exists("/tmp/fork-acceptance/reply"):
                response = json.loads(sandbox.files.read("/tmp/fork-acceptance/reply"))
                if response["id"] == request["id"]:
                    return {
                        key: value for key, value in response.items() if key != "id"
                    }
            time.sleep(0.05)
        self.fail("worker did not respond")

    def test_memory_process_open_fd_and_private_writes(self):
        source = self.source
        setup = source.commands.run("mkdir -p /tmp/fork-acceptance")
        self.assertEqual(setup.exit_code, 0, setup.stderr)
        source.files.write("/tmp/fork-acceptance/data", "0123456789abcdef")
        source.files.write("/tmp/fork-acceptance/marker", "source")
        source.files.write("/tmp/fork-acceptance/worker.py", _WORKER)
        source.commands.run("python3 /tmp/fork-acceptance/worker.py", background=True)
        before = self.witness(source)
        results = self.remember(source.fork(count=2))
        self.assertTrue(all(isinstance(result, Sandbox) for result in results), results)
        first, second = results
        self.assertEqual(len({source.id, first.id, second.id}), 3)
        self.assertEqual(self.witness(first), before)
        self.assertEqual(self.witness(second), before)
        changed = self.witness(first, "advance")
        self.assertEqual(changed["value"], before["value"] + 1)
        self.assertEqual(changed["offset"], before["offset"] + 4)
        self.assertEqual(self.witness(source), before)
        self.assertEqual(self.witness(second), before)
        first.files.write("/tmp/fork-acceptance/marker", "first")
        second.files.write("/tmp/fork-acceptance/marker", "second")
        self.assertEqual(source.files.read("/tmp/fork-acceptance/marker"), "source")
        self.assertEqual(first.files.read("/tmp/fork-acceptance/marker"), "first")
        self.assertEqual(second.files.read("/tmp/fork-acceptance/marker"), "second")
        source.kill()
        self.assertEqual(self.witness(first), changed)
        first.kill()
        self.assertEqual(self.witness(second), before)
        self.assertEqual(len(self.snapshot_ids), 1)
        print(
            "proof:",
            json.dumps(
                {
                    "case": "isolation",
                    "snapshot_count": 1,
                    "children": 2,
                    "memory_pid_fd_before": before,
                    "first_after_mutation": changed,
                    "sibling_unchanged": True,
                    "source_unchanged": True,
                    "children_survive_source_deletion": True,
                    "sibling_survives_child_deletion": True,
                }
            ),
            flush=True,
        )

    def test_partial_failure_keeps_real_successful_children(self):
        self.source.files.write("/tmp/fork-marker", "captured")
        original = adx.AdxBackend._restore_snapshot
        calls = 0

        def restore(spec, connection):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise BackendOperationError(
                    "test-injected child failure before create",
                    code="TEST_INJECTED",
                    outcome="not_started",
                )
            return original(spec, connection)

        with patch.object(adx.AdxBackend, "_restore_snapshot", side_effect=restore):
            results = self.remember(self.source.fork(count=3))
        self.assertEqual(calls, 3)
        self.assertIsInstance(results[1], BackendOperationError)
        self.assertEqual(results[1].code, "TEST_INJECTED")
        for index in (0, 2):
            self.assertIsInstance(results[index], Sandbox)
            self.assertEqual(results[index].files.read("/tmp/fork-marker"), "captured")
        self.assertEqual(self.source.commands.run("printf running").stdout, "running")
        self.assertEqual(len(self.snapshot_ids), 1)
        print(
            "proof:",
            json.dumps(
                {
                    "case": "partial_failure",
                    "snapshot_count": 1,
                    "slots": 3,
                    "real_successful_children": 2,
                    "injected_pre_create_failures": 1,
                    "source_continues": True,
                }
            ),
            flush=True,
        )

    def test_cleanup_failure_retains_handles_and_can_be_retried(self):
        with patch.object(
            adx.AdxBackend,
            "_delete_snapshot",
            side_effect=BackendOperationError(
                "test-injected delete failure", outcome="not_started"
            ),
        ):
            with self.assertRaises(ForkCleanupError) as raised:
                self.source.fork(count=1)
        error = raised.exception
        results = self.remember(error.results)
        self.assertEqual(error.snapshot_id, self.snapshot_ids[0])
        self.assertEqual(results[0].commands.run("printf alive").stdout, "alive")
        Sandbox.delete_snapshot(error.snapshot_id)
        self.assertEqual(
            results[0].commands.run("printf after-delete").stdout, "after-delete"
        )
        print(
            "proof:",
            json.dumps(
                {
                    "case": "cleanup_retry",
                    "snapshot_count": 1,
                    "child_handle_preserved": True,
                    "explicit_delete_retry": "passed",
                    "child_survives_snapshot_deletion": True,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
