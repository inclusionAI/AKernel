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
import os
import subprocess
import sys
import unittest
import weakref
from unittest.mock import MagicMock, patch

import yr_sandbox
from yr_sandbox._http_pool import _SHARED_HTTP_CLIENT_REGISTRY
from yr_sandbox._transport import SandboxClient

from akernel_sdk import Sandbox
from akernel_sdk import sandbox as sandbox_module
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends import openyuanrong_sandbox as adapter
from akernel_sdk._backends.base import BackendConfig
from akernel_sdk._backends.errors import BackendOperationError


def _gc_probe():
    gc.disable()
    endpoint = Endpoint(host="127.0.0.1", port=1, scheme="http", explicit_port=True)
    with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}, clear=True):
        backend = adapter.OpenYuanRongSandboxBackend(
            BackendConfig(
                api_endpoint=endpoint, gateway_endpoint=endpoint, token="test"
            )
        )
        for cyclic in (False, True):
            # Keep the actual AKernel -> Session -> native Sandbox -> HTTPX
            # object chain; only the server's create response is stubbed.
            with (
                patch.object(sandbox_module, "load_backend", return_value=backend),
                patch.object(
                    SandboxClient, "create_info",
                    return_value={"sandboxId": "gc-test", "status": "running"},
                ),
            ):
                sandbox = Sandbox()
            if cyclic:
                sandbox.cycle = sandbox
            reference = weakref.ref(sandbox)
            native_reference = weakref.ref(sandbox._session._sandbox)
            http = sandbox._session._sandbox._client._http._current_client()
            with http._transport._pool._optional_thread_lock:
                del sandbox
                gc.collect()
            if reference() is not None or native_reference() is not None:
                raise AssertionError("sandbox object chain was not collected")
    _SHARED_HTTP_CLIENT_REGISTRY.close_all()


class SandboxCleanupTests(unittest.TestCase):
    def make_sandbox(self):
        native = MagicMock()
        native.id = "cleanup-test"
        backend = MagicMock()
        backend.create.side_effect = lambda spec: adapter._Session(native, spec)
        with patch.object(sandbox_module, "load_backend", return_value=backend):
            sandbox = Sandbox()
        return sandbox, native

    def test_gc_with_real_backend_and_http_pool_does_not_deadlock(self):
        result = subprocess.run(
            [sys.executable, __file__, "--gc-probe"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_delete_failure_closes_local_handle_and_remains_retryable(self):
        sandbox, native = self.make_sandbox()
        with patch.object(
            yr_sandbox.Sandbox, "delete",
            side_effect=[RuntimeError("remote delete failed"), None],
        ) as delete:
            with self.assertRaisesRegex(BackendOperationError, "remote delete failed"):
                sandbox.kill()
            self.assertTrue(sandbox._closed)
            self.assertFalse(sandbox._terminated)
            sandbox.kill()
            sandbox.kill()
        self.assertTrue(sandbox._terminated)
        self.assertEqual(delete.call_count, 2)
        native.close.assert_called_once_with()

    def test_local_close_failure_still_deletes_and_does_not_repeat_delete(self):
        sandbox, native = self.make_sandbox()
        native.close.side_effect = RuntimeError("local close failed")
        with patch.object(yr_sandbox.Sandbox, "delete") as delete:
            with self.assertRaisesRegex(BackendOperationError, "local close failed"):
                sandbox.kill()
            self.assertTrue(sandbox._session._terminated)
            sandbox.kill()
        delete.assert_called_once_with("cleanup-test")
        native.close.assert_called_once_with()

    def test_delete_error_takes_precedence_over_native_close_error(self):
        sandbox, native = self.make_sandbox()
        native.close.side_effect = RuntimeError("local close failed")
        with (
            patch.object(
                yr_sandbox.Sandbox, "delete",
                side_effect=RuntimeError("remote delete failed"),
            ) as delete,
            self.assertLogs(adapter.logger, level="WARNING"),
            self.assertRaisesRegex(BackendOperationError, "remote delete failed"),
        ):
            sandbox.kill()
        delete.assert_called_once_with("cleanup-test")
        self.assertFalse(sandbox._terminated)

    def test_context_cleanup_error_preserves_workload_error(self):
        sandbox, native = self.make_sandbox()
        workload_error = ValueError("workload failed")
        with (
            patch.object(
                yr_sandbox.Sandbox, "delete",
                side_effect=[RuntimeError("remote delete failed"), None],
            ) as delete,
            self.assertLogs(sandbox_module.logger, level="WARNING") as logs,
        ):
            with self.assertRaises(ValueError) as raised, sandbox:
                raise workload_error
            self.assertIs(raised.exception, workload_error)
            sandbox.kill()
        self.assertIn("cleanup-test", logs.output[0])
        self.assertEqual(delete.call_count, 2)
        native.close.assert_called_once_with()

    def test_successful_block_propagates_cleanup_error(self):
        sandbox, _native = self.make_sandbox()
        with (
            patch.object(
                yr_sandbox.Sandbox, "delete",
                side_effect=RuntimeError("remote delete failed"),
            ),
            self.assertRaisesRegex(BackendOperationError, "remote delete failed"),
            sandbox,
        ):
            pass

    def test_named_delete_failure_is_visible_and_retryable(self):
        backend = MagicMock()
        backend.delete_named.side_effect = [RuntimeError("delete failed"), None]
        with patch.object(sandbox_module, "load_backend", return_value=backend):
            with self.assertRaisesRegex(RuntimeError, "delete failed"):
                Sandbox.delete("worker")
            Sandbox.delete("worker")
        self.assertEqual(backend.delete_named.call_count, 2)


if __name__ == "__main__":
    if sys.argv[1:] == ["--gc-probe"]:
        _gc_probe()
    else:
        unittest.main()
