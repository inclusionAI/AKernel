"""Exercise the packaged service helper with real Linux processes and signals."""

import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from akernel_sdk import BackendOperationError, ServiceProcessHandle, TimeoutException
from akernel_sdk.types import CommandResult


class LocalService:
    name = "worker"
    project = SimpleNamespace(descriptor={"project_name": "sdk-test"})

    def __init__(self):
        self.current = {"container_id": "c" * 64, "started_at": "original"}
        self.lose_start_reply = False
        self.tokens = set()
        self.identities = {}

    def identity(self):
        return dict(self.current)

    def exec(self, command, **kwargs):
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=kwargs.get("cwd"),
            env={**os.environ, **(kwargs.get("envs") or {})},
            text=True,
            capture_output=True,
            timeout=15,
        )
        # Capture only this test's exclusive process directories for cleanup.
        import re

        self.tokens.update(re.findall(r"\b[0-9a-f]{32}\b", command))
        for token in self.tokens:
            file = Path("/tmp/akernel-service-process-" + token) / "identity.json"
            if file.exists() and token not in self.identities:
                self.identities[token] = json.loads(file.read_text())
        if self.lose_start_reply:
            self.lose_start_reply = False
            raise TimeoutException("response lost", outcome="unknown")
        return CommandResult(
            stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode
        )


@unittest.skipUnless(
    sys.platform.startswith("linux"), "Requires Linux /proc and subreaper"
)
class ServiceProcessTest(unittest.TestCase):
    def setUp(self):
        self.libc = ctypes.CDLL(None)
        previous = ctypes.c_int()
        self.libc.prctl(37, ctypes.byref(previous), 0, 0, 0)
        self.previous_subreaper = previous.value
        self.libc.prctl(36, 1, 0, 0, 0)
        self.temp = tempfile.TemporaryDirectory()
        self.service = LocalService()

    def tearDown(self):
        for token in self.service.tokens:
            root = Path("/tmp/akernel-service-process-" + token)
            if not (root / "identity.json").exists():
                continue
            identity = self.service.identities[token]
            try:
                os.kill(identity["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            # The helper monitor is an adopted child, not an unrelated process.
            try:
                os.waitpid(identity["monitor_pid"], 0)
            except ChildProcessError:
                pass
            shutil.rmtree(root)
        self.temp.cleanup()
        self.libc.prctl(36, self.previous_subreaper, 0, 0, 0)

    def start(self):
        command = "exec " + sys.executable + " -c 'import time;time.sleep(60)'"
        return ServiceProcessHandle.start(self.service, command, cwd=self.temp.name)

    def test_freeze_attach_and_continue_original_process(self):
        handle = self.start()
        pid = handle.pid
        stopped = handle.signal("stop")
        self.assertTrue(stopped)
        self.assertTrue(all(p["state"] in ("T", "t") for p in stopped))
        attached = ServiceProcessHandle.attach(self.service, handle.descriptor)
        self.assertEqual(attached.pid, pid)
        self.assertEqual(attached.process_id, handle.process_id)
        attached.signal("continue")
        self.assertNotEqual(attached.inspect()[0]["state"], "T")

    def test_container_restart_refuses_to_signal_old_pid(self):
        handle = self.start()
        self.service.current["started_at"] = "restarted"
        with self.assertRaisesRegex(BackendOperationError, "replaced or restarted"):
            handle.signal("stop")

    def test_modified_identity_record_refuses_to_signal(self):
        handle = self.start()
        root = Path("/tmp/akernel-service-process-" + handle.process_id)
        record = json.loads((root / "identity.json").read_text())
        record["start_time"] = "0"
        (root / "identity.json").write_text(json.dumps(record))
        with self.assertRaises(BackendOperationError):
            handle.signal("stop")

    def test_unknown_start_recovers_by_token_without_relaunch(self):
        counter = Path(self.temp.name) / "counter"
        self.service.lose_start_reply = True
        command = f"echo launched >> {counter}; exec sleep 60"
        with self.assertRaises(TimeoutException) as raised:
            ServiceProcessHandle.start(self.service, command)
        attached = ServiceProcessHandle.attach(
            self.service, raised.exception.process_descriptor
        )
        self.assertGreater(attached.pid, 1)
        self.assertEqual(counter.read_text(), "launched\n")
        attached.signal("kill")
        self.assertEqual(attached.wait(timeout=3), 137)

    def test_wait_reports_exit_code_and_times_out_without_killing(self):
        handle = self.start()
        with self.assertRaises(TimeoutException):
            handle.wait(timeout=0.05)
        self.assertTrue(handle.inspect())
        handle.signal("kill")
        self.assertEqual(handle.wait(timeout=3), 137)

    def test_thaw_children_before_bash_waiter_does_not_report_sigstop_as_exit(self):
        import time
        from unittest.mock import patch

        from akernel_sdk import _service_process_guest as guest

        root = Path(self.temp.name)
        ready, result = root / "ready", root / "wait-result"
        command = (
            "set -m; sleep 1 & worker=$!; "
            f'echo "$worker" > {ready}; wait "$worker"; echo "$?" > {result}'
        )
        handle = ServiceProcessHandle.start(self.service, command)
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        child = int(ready.read_text())
        child_identity = guest.stat(child)
        process_root = Path("/tmp/akernel-service-process-" + handle.process_id)
        kill = os.kill

        def scheduled_kill(pid, sig):
            kill(pid, sig)
            if sig == signal.SIGCONT and pid == handle.pid:
                # Reproduce a scheduler switch to the waiting Bash immediately
                # after its thaw. A still-stopped child must not yield status 147.
                time.sleep(0.08)

        try:
            handle.signal("stop")
            with patch.object(guest.os, "kill", side_effect=scheduled_kill):
                guest.operate(process_root, "continue")
            self.assertEqual(handle.wait(timeout=3), 0)
            self.assertEqual(result.read_text().strip(), "0")
        finally:
            current = guest.stat(child)
            if (
                current
                and child_identity
                and current["start_time"] == child_identity["start_time"]
            ):
                try:
                    kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass
