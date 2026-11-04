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

from akernel_sdk import (
    BackendOperationError,
    CommandExitException,
    ForkCleanupError,
    Sandbox,
    SandboxNotFoundException,
    SandboxQuery,
    SandboxState,
)
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
        # Older deployed APIs omit newlyCreated. Explicitly acknowledge this
        # fixture's intended policy through the supported public API instead
        # of bypassing the SDK's fail-closed ownership/policy guard.
        if not source._session._network_policy_known:
            source.update_network_policy(options.get("network_policy"))
            print(
                "legacy create response: source policy explicitly reapplied", flush=True
            )
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
    def test_readiness_and_rollback_interruptions_keep_real_child_reconcilable(self):
        create = adx._OwnedSandbox.create
        rollback = adx._Session.rollback_creation
        primary = KeyboardInterrupt("client-injected restore readiness interruption")
        secondary = KeyboardInterrupt("client-injected rollback interruption")
        children = []
        rollback_attempts = []

        def interrupt_readiness(*args, **kwargs):
            native = create(*args, **kwargs)
            children.append(native)
            # A separately recorded, test-owned handle guarantees teardown even
            # if reconciliation assertions fail. It makes no new remote calls.
            self.remember(
                [
                    Sandbox._session_handle(
                        adx._Session._cleanup_handle(
                            native, kwargs["connection"], closed=False
                        ),
                        reverse_tunnel=None,
                        ports=[],
                        detached=False,
                    )
                ]
            )
            patcher = patch.object(native.commands, "list", side_effect=primary)
            patcher.start()
            self.addCleanup(patcher.stop)
            return native

        def interrupt_rollback(child):
            if children and child.id == children[0].id:
                rollback_attempts.append(child.id)
                child.close()
                # Inject at the rollback boundary without fabricating a True
                # ownership response on older deployments that omit the bit.
                raise secondary
            return rollback(child)

        with (
            patch.object(adx._OwnedSandbox, "create", side_effect=interrupt_readiness),
            patch.object(adx._Session, "rollback_creation", new=interrupt_rollback),
            self.assertRaises(BaseException) as raised,
        ):
            self.source.fork()
        error = raised.exception
        self.assertIs(error, primary)
        self.assertEqual(error.environment_id, children[0].id)
        self.assertEqual(error.created_environment_ids, (children[0].id,))
        self.assertEqual(error.pending_environment_ids, (children[0].id,))
        self.assertEqual(error.snapshot_id, self.snapshot_ids[0])
        self.assertIn(secondary, error.cleanup_errors)
        self.assertEqual(rollback_attempts, [children[0].id])
        self.assertEqual(Sandbox.get_info(children[0].id).state, "running")
        pending = self.remember(error.pending_sandboxes)[0]
        pending.kill()
        with self.assertRaises(SandboxNotFoundException):
            Sandbox.get_info(children[0].id)
        Sandbox.delete_snapshot(error.snapshot_id)
        self.assertEqual(
            self.source.commands.run("printf source-alive").stdout, "source-alive"
        )
        print(
            "proof:",
            json.dumps(
                {
                    "case": "readiness_then_rollback_interruption",
                    "fault_injection": "client-only",
                    "original_interruption_preserved": True,
                    "known_child_before_result_append": True,
                    "secondary_cleanup_interruption_retained": True,
                    "automatic_rollback_attempted_once": True,
                    "pending_handle_explicit_kill_retry": True,
                    "source_untouched": True,
                }
            ),
            flush=True,
        )

    def test_interrupted_fork_returns_retryable_real_child_cleanup_handle(self):
        self.source.files.write("/tmp/cancel-marker", "saved")
        restore = adx.AdxBackend._restore_snapshot
        interruption = KeyboardInterrupt("client-injected restore interruption")
        deletion = RuntimeError("client-injected child cleanup failure")
        attempts = 0

        def stop_second(spec, connection):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise interruption
            return restore(spec, connection)

        with (
            patch.object(adx.AdxBackend, "_restore_snapshot", side_effect=stop_second),
            patch.object(adx._Session, "rollback_creation", side_effect=deletion),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.source.fork(count=2)
        error = raised.exception
        self.assertIs(error, interruption)
        self.assertEqual(error.snapshot_id, self.snapshot_ids[0])
        self.assertEqual(len(error.pending_sandboxes), 1)
        self.assertIn(deletion, error.cleanup_errors)
        self.assertTrue(
            any(
                getattr(e, "code", None) == "SNAPSHOT_IN_USE"
                for e in error.cleanup_errors
            )
        )
        pending = self.remember(error.pending_sandboxes)[0]
        self.assertEqual(error.pending_environment_ids, (pending.id,))
        self.assertEqual(Sandbox.get_info(pending.id).state, "running")
        pending.kill()
        with self.assertRaises(SandboxNotFoundException):
            Sandbox.get_info(pending.id)
        Sandbox.delete_snapshot(error.snapshot_id)
        self.assertEqual(
            self.source.commands.run("printf source-alive").stdout, "source-alive"
        )
        print(
            "proof:",
            json.dumps(
                {
                    "case": "interrupted_fork_cleanup",
                    "fault_injection": "client-only",
                    "original_interruption_preserved": True,
                    "pending_child_handle_retry": True,
                    "snapshot_retry": True,
                    "source_untouched": True,
                }
            ),
            flush=True,
        )

    def test_final_snapshot_delete_interruption_cleans_real_children(self):
        delete = adx.AdxBackend._delete_snapshot
        restore = adx.AdxBackend._restore_snapshot
        interruption = KeyboardInterrupt("client-injected final delete interruption")
        attempts = 0
        children = []

        def remember_child(spec, connection):
            child = restore(spec, connection)
            children.append(child)
            self.remember(
                [
                    Sandbox._session_handle(
                        child,
                        reverse_tunnel=None,
                        ports=[],
                        detached=False,
                    )
                ]
            )
            return child

        def stop_first(identity, connection):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise interruption
            return delete(identity, connection)

        with (
            patch.object(adx.AdxBackend, "_delete_snapshot", side_effect=stop_first),
            patch.object(
                adx.AdxBackend, "_restore_snapshot", side_effect=remember_child
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.source.fork()
        error = raised.exception
        self.assertIs(error, interruption)
        self.assertEqual(error.snapshot_id, self.snapshot_ids[0])
        self.assertEqual(len(error.created_environment_ids), 1)
        confirmed = children[0]._sandbox.newly_created is True
        if confirmed:
            self.assertEqual(error.pending_environment_ids, ())
            self.assertEqual(error.pending_sandboxes, ())
        else:
            self.assertEqual(error.pending_environment_ids, (children[0].id,))
            self.assertEqual(Sandbox.get_info(children[0].id).state, "running")
            pending = self.remember(error.pending_sandboxes)[0]
            pending.kill()
            Sandbox.delete_snapshot(error.snapshot_id)
        with self.assertRaises(SandboxNotFoundException):
            Sandbox.get_info(error.created_environment_ids[0])
        self.assertEqual(
            self.source.commands.run("printf source-alive").stdout, "source-alive"
        )
        print(
            "proof:",
            json.dumps(
                {
                    "case": "final_delete_interruption",
                    "fault_injection": "client-only",
                    "created_child_deleted": True,
                    "automatic_delete_ownership_confirmed": confirmed,
                    "unconfirmed_child_retained_for_explicit_cleanup": not confirmed,
                    "snapshot_cleanup_retried": True,
                    "original_interruption_preserved": True,
                    "source_untouched": True,
                }
            ),
            flush=True,
        )

    def test_later_creation_error_survives_earlier_binding_interruption(self):
        restore = adx.AdxBackend._restore_snapshot
        interruption = KeyboardInterrupt("client-injected first binding interruption")
        rollback = RuntimeError("client-injected later rollback failure")
        later = None
        children = []

        def restore_then_fail(spec, connection):
            nonlocal later
            child = restore(spec, connection)
            children.append(child)
            # Test-owned handles guarantee teardown even if an assertion fails;
            # their existence does not give the SDK ownership of error IDs.
            self.remember(
                [
                    Sandbox._session_handle(
                        child,
                        reverse_tunnel=None,
                        ports=[],
                        detached=False,
                    )
                ]
            )
            if len(children) == 1:
                patcher = patch.object(child, "get_info", side_effect=interruption)
                patcher.start()
                self.addCleanup(patcher.stop)
                return child
            later = BackendOperationError(
                "client-injected uncertain later create",
                environment_id=child.id,
                request_id="injected-request",
                operation_id="injected-operation",
                outcome="unknown",
                cleanup_errors=(rollback,),
            )
            raise later

        with (
            patch.object(
                adx.AdxBackend, "_restore_snapshot", side_effect=restore_then_fail
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.source.fork(count=2)
        error = raised.exception
        self.assertIs(error, interruption)
        self.assertEqual(error.creation_errors, (later,))
        retained = error.creation_errors[0]
        self.assertEqual(retained.environment_id, children[1].id)
        self.assertEqual(retained.request_id, "injected-request")
        self.assertEqual(retained.operation_id, "injected-operation")
        self.assertEqual(retained.outcome, "unknown")
        self.assertIs(retained.cleanup_errors[0], rollback)
        self.assertEqual(Sandbox.get_info(children[1].id).state, "running")
        self.remember(error.pending_sandboxes)
        # This test observed the actual returned sessions before injecting the
        # uncertain error; explicit cleanup uses those test-owned identities.
        for child in reversed(children):
            child.terminate()
        Sandbox.delete_snapshot(error.snapshot_id)
        self.assertEqual(
            self.source.commands.run("printf source-alive").stdout, "source-alive"
        )
        print(
            "proof:",
            json.dumps(
                {
                    "case": "later_creation_error_during_binding_interrupt",
                    "fault_injection": "client-only",
                    "complete_creation_errors": True,
                    "nested_request_operation_rollback_preserved": True,
                    "uncertain_child_not_automatically_deleted": True,
                    "explicit_owned_cleanup": True,
                    "source_untouched": True,
                }
            ),
            flush=True,
        )

    def test_local_only_binding_rollback_keeps_real_child_retryable(self):
        restore = adx.AdxBackend._restore_snapshot
        interruption = KeyboardInterrupt("client-injected binding interruption")
        children = []
        delete_calls = []

        def unconfirmed_child(spec, connection):
            child = restore(spec, connection)
            children.append(child)
            self.remember(
                [
                    Sandbox._session_handle(
                        child,
                        reverse_tunnel=None,
                        ports=[],
                        detached=False,
                    )
                ]
            )
            # Exercise conservative behavior even on servers that return True.
            child._sandbox._newly_created = False
            original_delete = child.terminate

            def explicit_delete():
                delete_calls.append(child.id)
                return original_delete()

            for name, replacement in (
                ("get_info", interruption),
                ("terminate", explicit_delete),
            ):
                patcher = patch.object(child, name, side_effect=replacement)
                patcher.start()
                self.addCleanup(patcher.stop)
            return child

        with (
            patch.object(
                adx.AdxBackend, "_restore_snapshot", side_effect=unconfirmed_child
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.source.fork()
        error = raised.exception
        self.assertIs(error, interruption)
        self.assertEqual(error.pending_environment_ids, (children[0].id,))
        self.assertEqual(len(error.pending_sandboxes), 1)
        self.assertEqual(delete_calls, [])
        self.assertTrue(children[0]._closed)
        self.assertEqual(Sandbox.get_info(children[0].id).state, "running")
        pending = self.remember(error.pending_sandboxes)[0]
        pending.kill()
        self.assertEqual(delete_calls, [children[0].id])
        with self.assertRaises(SandboxNotFoundException):
            Sandbox.get_info(children[0].id)
        Sandbox.delete_snapshot(error.snapshot_id)
        self.assertEqual(
            self.source.commands.run("printf source-alive").stdout, "source-alive"
        )
        print(
            "proof:",
            json.dumps(
                {
                    "case": "local_only_binding_rollback",
                    "fault_injection": "client-only",
                    "local_client_closed_remote_still_running": True,
                    "no_automatic_delete_without_ownership": True,
                    "pending_handle_explicit_kill_retry": True,
                    "source_untouched": True,
                }
            ),
            flush=True,
        )

    def test_snapshot_catalog_and_independent_verifier(self):
        source = self.source
        name = "sdk-current-" + uuid.uuid4().hex
        source.files.write("/tmp/catalog-marker", "captured-state")
        snapshot = source.create_snapshot(name=name)
        self.assertEqual(
            Sandbox.get_snapshot(snapshot.snapshot_id).snapshot_id, snapshot.snapshot_id
        )
        pages = Sandbox.list_snapshots(name=name, limit=1)
        found = []
        while pages.has_next:
            found.extend(item.snapshot_id for item in pages.next_items())
        self.assertIn(snapshot.snapshot_id, found)
        source.kill()
        restored = Sandbox.create(snapshot)
        self.remember([restored])
        self.assertEqual(restored.get_info().memory, 1024)
        self.assertEqual(restored.get_info().cpu, 1000)
        artifact = restored.files.read("/tmp/catalog-marker")
        self.assertEqual(artifact, "captured-state")
        # A fresh independent sandbox verifies exported task output; it shares
        # neither the restored runtime nor the checkpoint's writable layer.
        verifier = Sandbox.create(image=_IMAGE, memory=1024, idle_timeout=600)
        self.remember([verifier])
        self.assertNotEqual(verifier.id, restored.id)
        self.assertFalse(verifier.files.exists("/tmp/catalog-marker"))
        check = (
            'test "$(cat /tmp/submitted-artifact)" = captured-state && '
            "printf verifier-passed"
        )
        verifier.files.write("/tmp/submitted-artifact", "invalid-output")
        with self.assertRaises(CommandExitException):
            verifier.commands.run(check, check=True)
        verifier.files.write("/tmp/submitted-artifact", artifact)
        result = verifier.commands.run(check, check=True)
        self.assertEqual(result.stdout, "verifier-passed")
        restored.kill()
        Sandbox.delete_snapshot(snapshot.snapshot_id)
        Sandbox.delete_snapshot(snapshot.snapshot_id)
        print(
            "proof:",
            json.dumps(
                {
                    "case": "snapshot_catalog_and_independent_verifier",
                    "create_get_filtered_list_delete": True,
                    "source_deleted_before_restore": True,
                    "captured_resources_retained": True,
                    "independent_verifier": True,
                    "verifier_rejects_invalid_output": True,
                }
            ),
            flush=True,
        )

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

    def test_pause_connect_preserves_memory_and_close_ownership(self):
        source = self.source
        source.commands.run("mkdir -p /tmp/fork-acceptance")
        source.files.write("/tmp/fork-acceptance/data", "0123456789abcdef")
        source.files.write("/tmp/fork-acceptance/worker.py", _WORKER)
        source.commands.run("python3 /tmp/fork-acceptance/worker.py", background=True)
        before = self.witness(source)
        self.assertTrue(source.pause())
        self.assertFalse(source.beta_pause())
        source.close()
        self.assertEqual(Sandbox.get_info(source.id).state, "paused")
        pages = Sandbox.list(query=SandboxQuery(state=[SandboxState.PAUSED]), limit=1)
        visible = []
        while pages.has_next:
            visible.extend(pages.next_items())
        self.assertIn(source.id, [row.sandbox_id for row in visible])
        self.assertTrue(all(row.state == "paused" for row in visible))
        connected = Sandbox.connect(source.id, cwd="/tmp")
        self.remember([connected])
        self.assertEqual(connected.sandbox_id, source.id)
        self.assertEqual(self.witness(connected), before)
        connected.close()
        self.assertEqual(Sandbox.get_info(source.id).state, "running")
        again = Sandbox.connect(source.id)
        self.remember([again])
        self.assertEqual(self.witness(again), before)
        self.assertTrue(Sandbox.kill(source.id))
        self.assertFalse(Sandbox.kill(source.id))
        print(
            "proof:",
            json.dumps(
                {
                    "case": "pause_connect_lifecycle",
                    "same_id": True,
                    "memory_pid_fd": before,
                    "paused_query": True,
                    "close_keeps_remote": True,
                    "running_connect": True,
                    "idempotent_by_id_delete": True,
                }
            ),
            flush=True,
        )

    def test_create_and_from_snapshot_restore_the_same_saved_state(self):
        source = self.source
        setup = source.commands.run("mkdir -p /tmp/fork-acceptance")
        self.assertEqual(setup.exit_code, 0, setup.stderr)
        source.files.write("/tmp/fork-acceptance/data", "0123456789abcdef")
        source.files.write("/tmp/fork-acceptance/marker", "captured")
        source.files.write("/tmp/fork-acceptance/worker.py", _WORKER)
        source.commands.run("python3 /tmp/fork-acceptance/worker.py", background=True)
        before = self.witness(source)
        snapshot = source.create_snapshot()
        self.assertEqual(self.witness(source, "advance")["value"], 1)
        source.files.write("/tmp/fork-acceptance/marker", "after-capture")
        source.kill()

        restored = Sandbox.create(snapshot.snapshot_id, cwd="/tmp")
        self.remember([restored])
        compatible = Sandbox.from_snapshot(snapshot, cwd="/tmp")
        self.remember([compatible])
        self.assertEqual(len({source.id, restored.id, compatible.id}), 3)
        for child in (restored, compatible):
            self.assertEqual(self.witness(child), before)
            self.assertEqual(
                child.files.read("/tmp/fork-acceptance/marker"), "captured"
            )
        changed = self.witness(restored, "advance")
        self.assertEqual(changed["value"], before["value"] + 1)
        self.assertEqual(changed["offset"], before["offset"] + 4)
        restored.files.write("/tmp/fork-acceptance/marker", "created")
        self.assertEqual(self.witness(compatible), before)
        self.assertEqual(
            compatible.files.read("/tmp/fork-acceptance/marker"), "captured"
        )
        restored.kill()
        self.assertEqual(self.witness(compatible), before)
        self.assertEqual(len(self.snapshot_ids), 1)
        print(
            "proof:",
            json.dumps(
                {
                    "case": "create_from_snapshot_compatibility",
                    "snapshot_count": 1,
                    "restored_instances": 2,
                    "memory_pid_fd_before": before,
                    "create_after_mutation": changed,
                    "from_snapshot_unchanged": True,
                    "source_deleted_before_restore": True,
                    "compatibility_child_survives_other_deletion": True,
                }
            ),
            flush=True,
        )

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
        # Current ADX rejects deletion while a running clone references the
        # snapshot. The cleanup error retains both IDs until explicit release.
        results[0].kill()
        Sandbox.delete_snapshot(error.snapshot_id)
        Sandbox.delete_snapshot(error.snapshot_id)
        self.assertEqual(
            self.source.commands.run("printf after-delete").stdout, "after-delete"
        )
        print(
            "proof:",
            json.dumps(
                {
                    "case": "cleanup_retry",
                    "snapshot_count": 1,
                    "child_handle_preserved": True,
                    "explicit_delete_retry": "passed",
                    "child_released_before_snapshot_deletion": True,
                    "source_survives_child_and_snapshot_deletion": True,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
