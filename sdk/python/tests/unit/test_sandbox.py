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

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from akernel_sdk import HttpReverseTunnel, S3Config, Sandbox
from akernel_sdk import sandbox as sandbox_module


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.handle = SimpleNamespace(instance_id="logical-id")
        self.client = MagicMock()
        backend = sandbox_module._openyuanrong
        self.patchers = [
            patch.object(backend, "ensure_initialized"),
            patch.object(backend, "build_options", return_value="options"),
            patch.object(backend, "create_instance", return_value=self.handle),
            patch.object(backend, "real_instance_id", return_value="physical-id"),
            patch.object(backend, "terminate_instance"),
            patch.object(backend, "delete_named_instance"),
            patch.object(backend, "ping_instance", return_value=True),
            patch.object(backend, "instance_state", return_value="running"),
            patch.object(backend, "start_reverse_tunnel", return_value=self.client),
        ]
        self.mocks = [patcher.start() for patcher in self.patchers]
        self.addCleanup(self._stop_patchers)

    def _stop_patchers(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    @property
    def backend(self):
        return sandbox_module._openyuanrong

    def test_default_constructor_and_info(self):
        sandbox = Sandbox(cpu=2000, memory=8192)
        self.assertEqual(sandbox.id, "physical-id")
        self.assertIsNone(sandbox.reverse_tunnel)
        self.assertTrue(sandbox.is_running())
        self.assertEqual(sandbox.get_info().id, "physical-id")
        self.assertEqual(sandbox.get_info().cpu, 2000)
        self.assertIsNone(sandbox.get_info().xpu)
        self.assertIsNone(sandbox.get_info().storage_mb)
        self.backend.build_options.assert_called_once()
        sandbox.kill()
        self.backend.terminate_instance.assert_called_once_with(self.handle)

    def test_kill_is_idempotent(self):
        sandbox = Sandbox()
        sandbox.kill()
        sandbox.kill()
        self.backend.terminate_instance.assert_called_once_with(self.handle)

    def test_detached_sandbox_is_not_terminated_by_kill(self):
        sandbox = Sandbox(name="worker", detached=True)
        sandbox.kill()
        self.backend.terminate_instance.assert_not_called()

    def test_named_delete_hides_backend_namespace(self):
        Sandbox.delete("worker")
        self.backend.delete_named_instance.assert_called_once_with("worker")

    def test_rootfs_requires_s3_config(self):
        with self.assertRaisesRegex(TypeError, "S3Config"):
            Sandbox(rootfs={"type": "s3"})
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            Sandbox(
                image="ubuntu:24.04",
                rootfs=S3Config("https://s3.example.com", "rootfs", "rootfs.img"),
            )

    def test_supported_runtimes(self):
        Sandbox(runtime="kata")

        with self.assertRaisesRegex(ValueError, "unsupported runtime"):
            Sandbox(runtime="unknown")

    def test_xpu_request_is_normalized_and_passed_to_backend(self):
        sandbox = Sandbox(xpu=" GPU:L20:02 ")
        self.assertEqual(sandbox.get_info().xpu, "gpu:l20:2")
        self.assertEqual(
            self.backend.build_options.call_args.kwargs["xpu"],
            "gpu:l20:2",
        )
        sandbox.kill()

    def test_xpu_request_validation(self):
        invalid = (
            (1, TypeError),
            ("gpu", ValueError),
            ("gpu::1", ValueError),
            ("npu:l20:1", ValueError),
            ("gpu:l20:0", ValueError),
            ("gpu:l20:1.5", ValueError),
            ("gpu:l20/evil:1", ValueError),
        )
        for value, error_type in invalid:
            with self.subTest(value=value), self.assertRaises(error_type):
                Sandbox(xpu=value)
        with self.assertRaisesRegex(ValueError, "xpu.*runsc"):
            Sandbox(runtime="kata", xpu="gpu:l20:1")
        self.backend.ensure_initialized.assert_not_called()

    def test_storage_request_is_passed_to_backend(self):
        sandbox = Sandbox(storage_mb=256)
        self.assertEqual(sandbox.get_info().storage_mb, 256)
        self.assertEqual(
            self.backend.build_options.call_args.kwargs["storage_mb"],
            256,
        )
        sandbox.kill()

    def test_storage_request_validation(self):
        for value in (True, 0, -1, 1.5):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                Sandbox(storage_mb=value)
        with self.assertRaisesRegex(ValueError, "storage_mb.*runsc"):
            Sandbox(runtime="kata", storage_mb=256)
        self.backend.ensure_initialized.assert_not_called()

    def test_port_forwardings_are_integer_ports(self):
        with self.assertRaisesRegex(TypeError, "integer"):
            Sandbox(port_forwardings=["8080"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            Sandbox(port_forwardings=[8080, 8080])

    def test_reverse_tunnel_lifecycle(self):
        tunnel = HttpReverseTunnel(
            "https://example.com", reverse_port=9000, listen_port=9001
        )
        sandbox = Sandbox(reverse_tunnel=tunnel)
        self.assertIs(sandbox.reverse_tunnel, tunnel)
        self.assertEqual(sandbox.reverse_tunnel.url, "http://127.0.0.1:9001")
        self.backend.start_reverse_tunnel.assert_called_once_with(
            self.handle, tunnel, name=None
        )
        sandbox.kill()
        self.client.stop.assert_called_once()

    def test_reverse_tunnel_port_conflict(self):
        tunnel = HttpReverseTunnel(
            "http://127.0.0.1:8000", reverse_port=9000, listen_port=9001
        )
        with self.assertRaisesRegex(ValueError, "conflict"):
            Sandbox(port_forwardings=[9000], reverse_tunnel=tunnel)
        with self.assertRaisesRegex(ValueError, "conflict"):
            Sandbox(port_forwardings=[9001], reverse_tunnel=tunnel)

    def test_tunnel_start_failure_terminates_instance(self):
        self.backend.start_reverse_tunnel.side_effect = RuntimeError("timeout")
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            Sandbox(reverse_tunnel=HttpReverseTunnel("example.com"), detached=True)
        self.backend.terminate_instance.assert_called_once_with(self.handle)

    def test_get_port_url(self):
        with patch.dict(
            os.environ,
            {"AKERNEL_SERVER_ADDRESS": "gateway.example.com"},
            clear=True,
        ):
            sandbox = Sandbox(port_forwardings=[8080])
            self.assertEqual(
                sandbox.get_port_url(8080),
                "http://gateway.example.com/physical-id/8080",
            )
            with self.assertRaisesRegex(ValueError, "not in port_forwardings"):
                sandbox.get_port_url(9090)
            sandbox.kill()


if __name__ == "__main__":
    unittest.main()
