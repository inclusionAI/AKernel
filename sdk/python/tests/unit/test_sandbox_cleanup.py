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
import unittest
import weakref
from threading import Lock, get_ident
from unittest.mock import MagicMock, patch

from akernel_sdk import Sandbox
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

    def test_gc_under_http_pool_lock_does_not_delete_or_close(self):
        pool_lock = Lock()

        def terminate():
            # Bound the old finalizer's failure instead of hanging the suite.
            if pool_lock.acquire(timeout=0.25):
                pool_lock.release()

        sandbox, session = self.make_sandbox()
        session.terminate.side_effect = terminate
        reference = weakref.ref(sandbox)
        sandbox.cycle = sandbox
        with pool_lock:
            del sandbox
            gc.collect()
        self.assertIsNone(reference())
        session.terminate.assert_not_called()
        session.close.assert_not_called()
        self.assertNotIn("__del__", Sandbox.__dict__)

    def test_context_exit_deletes_synchronously(self):
        sandbox, session = self.make_sandbox()
        calls = []
        session.terminate.side_effect = lambda: calls.append(get_ident())
        with sandbox:
            session.terminate.assert_not_called()
        self.assertEqual(calls, [get_ident()])
        session.close.assert_called_once()
        self.assertTrue(sandbox._terminated)
        self.assertTrue(sandbox._closed)

    def test_delete_error_does_not_mask_body_exception_or_retry_after_gc(self):
        sandbox, session = self.make_sandbox()
        session.terminate.side_effect = RuntimeError("remote delete failed")
        with self.assertLogs(sandbox_module.logger, level="WARNING"):
            with self.assertRaisesRegex(ValueError, "body failed"):
                with sandbox:
                    raise ValueError("body failed")
        sandbox.kill()
        del sandbox
        gc.collect()
        session.terminate.assert_called_once()
        session.close.assert_called_once()

    def test_context_exit_succeeds_when_delete_and_local_cleanup_fail(self):
        sandbox, session = self.make_sandbox()
        session.terminate.side_effect = RuntimeError("delete failed")
        session.close.side_effect = RuntimeError("close failed")
        with self.assertLogs(sandbox_module.logger, level="WARNING"):
            with sandbox:
                pass
        self.assertTrue(sandbox._terminated)
        self.assertTrue(sandbox._closed)

    def test_named_delete_backend_error_is_logged_not_raised(self):
        backend = MagicMock()
        backend.delete_named.side_effect = RuntimeError("frontend not owning proxy")
        with patch.object(sandbox_module, "load_backend", return_value=backend):
            with self.assertLogs(sandbox_module.logger, level="WARNING"):
                self.assertIsNone(Sandbox.delete("worker"))
        backend.delete_named.assert_called_once_with("worker")

    def test_named_delete_still_validates_input(self):
        with patch.object(sandbox_module, "load_backend") as load:
            with self.assertRaises(ValueError):
                Sandbox.delete("")
        load.assert_not_called()
