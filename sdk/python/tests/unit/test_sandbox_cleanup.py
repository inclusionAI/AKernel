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

import gc
import multiprocessing
import os
import unittest
from threading import Event, Lock, get_ident
from unittest.mock import MagicMock, patch

from akernel_sdk import Sandbox
from akernel_sdk import _sandbox_cleanup as cleanup
from akernel_sdk import sandbox as sandbox_module


class SandboxCleanupTest(unittest.TestCase):
    def make_sandbox(self, **kwargs):
        session = MagicMock()
        session.id = "unit-test"
        backend = MagicMock()
        backend.create.return_value = session
        with patch.object(sandbox_module, "load_backend", return_value=backend):
            sandbox = Sandbox(**kwargs)
        return sandbox, session

    def test_gc_under_http_pool_lock_does_not_reenter_it(self):
        pool_lock = Lock()
        finished = Event()
        attempts = []
        owner = get_ident()

        def terminate():
            # Bound the unpatched failure so this regression never hangs CI.
            acquired = pool_lock.acquire(timeout=0.25)
            attempts.append((get_ident(), acquired))
            if acquired:
                pool_lock.release()
            finished.set()

        sandbox, session = self.make_sandbox()
        session.terminate.side_effect = terminate
        sandbox.cycle = sandbox
        with pool_lock:
            del sandbox
            gc.collect()
        self.assertTrue(finished.wait(5))
        self.assertEqual(len(attempts), 1)
        self.assertNotEqual(attempts[0][0], owner)
        self.assertTrue(attempts[0][1])

    def test_explicit_kill_remains_synchronous_and_is_not_repeated_by_gc(self):
        sandbox, session = self.make_sandbox()
        owner = get_ident()
        calls = []
        session.terminate.side_effect = lambda: calls.append(get_ident())
        sandbox.kill()
        with patch.object(sandbox_module, "defer_cleanup") as defer:
            sandbox.__del__()
        self.assertEqual(calls, [owner])
        session.close.assert_called_once()
        defer.assert_not_called()

    def test_detached_gc_closes_client_without_terminating_sandbox(self):
        sandbox, session = self.make_sandbox(name="worker", detached=True)
        closed = Event()
        session.close.side_effect = closed.set
        del sandbox
        self.assertTrue(closed.wait(5))
        session.terminate.assert_not_called()

    def test_worker_continues_after_a_cleanup_failure(self):
        cleanup.start_cleanup_worker()
        finished = Event()

        def fail():
            raise RuntimeError("simulated cleanup failure")

        with patch.object(cleanup.logger, "warning") as warning:
            cleanup.defer_cleanup(fail)
            cleanup.defer_cleanup(finished.set)
            self.assertTrue(finished.wait(5))
            warning.assert_called_once()

    def test_constructor_validation_failure_does_not_enqueue_cleanup(self):
        # Keep this object isolated from unrelated cyclic garbage in the suite.
        sandbox = Sandbox.__new__(Sandbox)
        with patch.object(sandbox_module, "defer_cleanup") as defer:
            with self.assertRaises(TypeError):
                sandbox.__init__(cpu="invalid")
            sandbox.__del__()
            defer.assert_not_called()

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork")
    def test_new_sandbox_in_fork_child_has_a_cleanup_worker(self):
        cleanup.start_cleanup_worker()

        def child():
            sandbox, session = self.make_sandbox()
            finished = Event()
            session.terminate.side_effect = finished.set
            del sandbox
            os._exit(0 if finished.wait(5) else 1)

        process = multiprocessing.get_context("fork").Process(target=child)
        process.start()
        try:
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.kill()
                process.join()
