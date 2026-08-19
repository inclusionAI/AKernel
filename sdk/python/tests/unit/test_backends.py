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
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, patch

from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends import (
    openyuanrong_sandbox,
    openyuanrong_sdk,
    registry,
)
from akernel_sdk._backends.base import BackendConfig, SandboxSpec
from akernel_sdk._backends.errors import (
    BackendNotInstalledError,
    BackendOperationError,
    InvalidBackendError,
    UnsupportedBackendFeatureError,
)
from akernel_sdk.types import (
    CommandInfo,
    CommandResult,
    EntryInfo,
    HttpReverseTunnel,
    Mount,
    NetworkPolicy,
    S3Config,
)


def _spec(**overrides):
    values = {
        "image": None,
        "rootfs": None,
        "runtime": "runsc",
        "cpu": 1000,
        "memory": 4096,
        "cpu_limit": 0,
        "mem_limit": 0,
        "idle_timeout": 300,
        "schedule_timeout": 30,
        "env": MappingProxyType({}),
        "name": None,
        "command_cwd": None,
        "port_forwardings": (),
        "mounts": (),
        "reverse_tunnel": None,
        "detached": False,
        "node_id": None,
        "xpu": None,
        "storage_mb": None,
        "network_policy": None,
    }
    values.update(overrides)
    return SandboxSpec(**values)


class RegistryTest(unittest.TestCase):
    def test_explicit_selection_wins_without_importing_backend(self):
        with (
            patch.dict(
                os.environ,
                {"AKERNEL_BACKEND": "openyuanrong-sdk"},
                clear=True,
            ),
            patch.object(registry, "_is_installed") as installed,
        ):
            self.assertEqual(registry._select_backend(), "openyuanrong-sdk")
        installed.assert_not_called()

    def test_sandbox_has_auto_detection_priority(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(registry, "_is_installed", return_value=True) as installed,
        ):
            self.assertEqual(registry._select_backend(), "openyuanrong-sandbox")
        installed.assert_called_once_with("openyuanrong-sandbox")

    def test_invalid_explicit_backend_fails_during_selection(self):
        with (
            patch.dict(os.environ, {"AKERNEL_BACKEND": "sandbox"}, clear=True),
            self.assertRaisesRegex(InvalidBackendError, "openyuanrong-sandbox"),
        ):
            registry._select_backend()

    def test_missing_default_backend_recommends_plain_install(self):
        error = registry._not_installed_error("openyuanrong-sandbox")
        self.assertIsInstance(error, BackendNotInstalledError)
        self.assertIn("pip install akernel-sdk", str(error))
        self.assertNotIn("[openyuanrong-sandbox]", str(error))

    def test_missing_actor_backend_recommends_named_extra(self):
        error = registry._not_installed_error("openyuanrong-sdk")
        self.assertIn("akernel-sdk[openyuanrong-sdk]", str(error))

    def test_loaded_backend_close_is_registered_for_process_exit(self):
        backend = MagicMock()
        backend_module = SimpleNamespace(
            create_backend=MagicMock(return_value=backend),
        )
        with (
            patch.object(registry, "_loaded_backend", None),
            patch.object(registry, "_selected_backend", "openyuanrong-sdk"),
            patch.object(registry, "_is_installed", return_value=True),
            patch.object(
                registry.importlib,
                "import_module",
                return_value=backend_module,
            ),
            patch.object(registry, "_config_from_env", return_value=MagicMock()),
            patch.object(registry.atexit, "register") as register,
        ):
            self.assertIs(registry.load_backend(), backend)

        register.assert_called_once_with(backend.close)


class OpenYuanRongSandboxBackendTest(unittest.TestCase):
    def setUp(self):
        self.config = BackendConfig(
            api_endpoint=Endpoint("api.example", 443, "https", True),
            gateway_endpoint=Endpoint("gateway.example", 80, "http", True),
            token="secret",
        )
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.backend = openyuanrong_sandbox.OpenYuanRongSandboxBackend(self.config)

    def test_connection_config_maps_to_yr_environment(self):
        self.assertEqual(os.environ["YR_SERVER_ADDRESS"], "api.example:443")
        self.assertEqual(os.environ["YR_TLS"], "1")
        self.assertEqual(os.environ["YR_GATEWAY_ADDRESS"], "gateway.example:80")
        self.assertEqual(os.environ["YR_GATEWAY_TLS"], "0")
        self.assertEqual(os.environ["YR_TOKEN"], "secret")

    def test_runtime_identifier_without_explicit_rootfs_is_forwarded(self):
        native = MagicMock()
        native.id = "default-gvisor-next"
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            self.backend.create(_spec(runtime="gvisor-next"))

        # YuanRong applies this runtime as a configuration override to the
        # deployed default rootfs; the adapter does not build a filesystem
        # overlay.
        self.assertEqual(sandbox_type.call_args.kwargs["runtime"], "gvisor-next")
        self.assertIsNone(sandbox_type.call_args.kwargs["rootfs"])

    def test_runsc_without_explicit_rootfs_passes_runtime_config_override(self):
        native = MagicMock()
        native.id = "default-runsc"
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            self.backend.create(_spec())

        self.assertEqual(sandbox_type.call_args.kwargs["runtime"], "runsc")
        self.assertIsNone(sandbox_type.call_args.kwargs["rootfs"])

    def test_explicit_kata_image_is_forwarded_to_native_sdk(self):
        native = MagicMock()
        native.id = "default-kata-image"
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            self.backend.create(_spec(runtime="kata", image="ubuntu:24.04"))

        self.assertEqual(sandbox_type.call_args.kwargs["runtime"], "kata")
        self.assertEqual(sandbox_type.call_args.kwargs["image"], "ubuntu:24.04")

    def test_create_converts_inputs_and_preserves_akernel_outputs(self):
        native = MagicMock()
        native.id = "default-worker"
        native_info = SimpleNamespace(
            id="default-worker",
            state="running",
            cpu=2000,
            memory=8192,
            image=None,
        )
        native.get_info.return_value = native_info
        native.commands.run.return_value = SimpleNamespace(
            stdout="ok\n",
            stderr="",
            exit_code=0,
        )
        native.commands.list.return_value = [
            SimpleNamespace(pid=7, command="sleep 1", running=True)
        ]
        native.files.get_info.return_value = SimpleNamespace(
            name="a.txt",
            path="/tmp/a.txt",
            type="file",
            size=3,
            permissions="rw-r--r--",
            modified_time=1.0,
        )
        rootfs = S3Config("https://s3.example", "rootfs", "rootfs.img")
        mount = Mount(target="/tools", image_url="tools:v1")
        tunnel = HttpReverseTunnel("https://service.example")
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            session = self.backend.create(
                _spec(
                    rootfs=rootfs,
                    runtime="kata",
                    cpu=2000,
                    memory=8192,
                    name="worker",
                    command_cwd="/workspace",
                    port_forwardings=(8080,),
                    mounts=(mount,),
                    reverse_tunnel=tunnel,
                    detached=True,
                    node_id="node-1",
                )
            )

        kwargs = sandbox_type.call_args.kwargs
        self.assertIsInstance(
            kwargs["rootfs"],
            openyuanrong_sandbox.yr_sandbox.S3Config,
        )
        self.assertEqual(kwargs["runtime"], "kata")
        self.assertEqual(kwargs["cwd"], "/workspace")
        self.assertEqual(kwargs["port_forwardings"], [8080])
        self.assertEqual(kwargs["upstream"], "https://service.example")
        self.assertEqual(kwargs["create_timeout"], 60)
        self.assertEqual(kwargs["node_id"], "node-1")

        self.assertEqual(
            session.commands.run("echo ok", envs=None, cwd=None, timeout=60),
            CommandResult("ok\n", "", 0),
        )
        self.assertEqual(
            session.commands.list(),
            [CommandInfo(pid=7, command="sleep 1", running=True)],
        )
        self.assertEqual(
            session.files.get_info("/tmp/a.txt"),
            EntryInfo(
                name="a.txt",
                path="/tmp/a.txt",
                type="file",
                size=3,
                permissions="rw-r--r--",
                modified_time=1.0,
            ),
        )
        self.assertEqual(session.get_info().id, "default-worker")
        session.close()
        native.close.assert_called_once_with()
        native.kill.assert_not_called()

    def test_create_converts_network_policy_to_native_sdk_type(self):
        native = MagicMock()
        native.id = "default-worker"
        native.commands = MagicMock()
        native.files = MagicMock()
        policy = NetworkPolicy.deny_dns("github.com", "*.github.com")
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            session = self.backend.create(_spec(network_policy=policy))

        network = sandbox_type.call_args.kwargs["network"]
        self.assertIsInstance(network, openyuanrong_sandbox.yr_sandbox.NetworkPolicy)
        self.assertFalse(network.block_network)
        self.assertEqual(network.dns_blacklist, ("github.com", "*.github.com"))
        session.close()

    def test_terminate_forces_deletion_of_detached_native_sandbox(self):
        native = MagicMock()
        native.id = "default-worker"
        native.commands = MagicMock()
        native.files = MagicMock()
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            session = self.backend.create(_spec(detached=True))
            session.terminate()
            session.close()

        sandbox_type.delete.assert_called_once_with("default-worker")
        native.close.assert_called_once_with()
        native.kill.assert_not_called()

    def test_terminate_closes_native_resources_before_remote_delete(self):
        lifecycle = []
        native = MagicMock()
        native.id = "default-worker"
        native.commands = MagicMock()
        native.files = MagicMock()
        native.close.side_effect = lambda: lifecycle.append("close")
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            sandbox_type.delete.side_effect = lambda _sandbox_id: lifecycle.append(
                "delete"
            )
            session = self.backend.create(_spec())
            session.terminate()

        self.assertEqual(lifecycle, ["close", "delete"])

    def test_detached_delete_failure_still_allows_local_cleanup(self):
        native = MagicMock()
        native.id = "default-worker"
        native.commands = MagicMock()
        native.files = MagicMock()
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            sandbox_type.delete.side_effect = [
                RuntimeError("remote delete failed"),
                None,
            ]
            session = self.backend.create(_spec(detached=True))
            with self.assertRaisesRegex(
                BackendOperationError, "remote delete failed"
            ):
                try:
                    session.terminate()
                finally:
                    session.close()
            session.terminate()
            session.terminate()

        self.assertEqual(sandbox_type.delete.call_count, 2)
        sandbox_type.delete.assert_called_with("default-worker")
        native.close.assert_called_once_with()
        native.kill.assert_not_called()

    def test_terminate_then_close_does_not_issue_second_native_delete(self):
        native = MagicMock()
        native.id = "default-anonymous"
        native.commands = MagicMock()
        native.files = MagicMock()
        native.kill.side_effect = RuntimeError("redundant DELETE failed")
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            session = self.backend.create(_spec())
            session.terminate()
            session.close()

        sandbox_type.delete.assert_called_once_with("default-anonymous")
        native.close.assert_called_once_with()
        native.kill.assert_not_called()

    def test_non_detached_termination_uses_retryable_id_delete(self):
        native = MagicMock()
        native.id = "default-anonymous"
        native.commands = MagicMock()
        native.files = MagicMock()
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            sandbox_type.delete.side_effect = [RuntimeError("temporary failure"), None]
            session = self.backend.create(_spec())
            with self.assertRaisesRegex(BackendOperationError, "temporary failure"):
                session.terminate()
            session.terminate()
            session.terminate()

        self.assertEqual(sandbox_type.delete.call_count, 2)
        sandbox_type.delete.assert_called_with("default-anonymous")
        native.kill.assert_not_called()

    def test_custom_reverse_tunnel_ports_are_forwarded(self):
        native = MagicMock()
        native.id = "default-anonymous"
        native.commands = MagicMock()
        native.files = MagicMock()
        tunnel = HttpReverseTunnel(
            "https://service.example",
            reverse_port=9000,
            listen_port=9001,
        )
        with patch.object(
            openyuanrong_sandbox.yr_sandbox,
            "Sandbox",
            return_value=native,
        ) as sandbox_type:
            session = self.backend.create(_spec(reverse_tunnel=tunnel))
            session.close()

        self.assertEqual(sandbox_type.call_args.kwargs["proxy_port"], 9001)

    def test_non_consecutive_reverse_tunnel_ports_are_rejected(self):
        tunnel = HttpReverseTunnel(
            "https://service.example",
            reverse_port=9000,
            listen_port=9002,
        )
        with self.assertRaisesRegex(
            UnsupportedBackendFeatureError,
            "listen_port - 1",
        ):
            self.backend.create(_spec(reverse_tunnel=tunnel))

    def test_named_delete_uses_deterministic_sid(self):
        with patch.object(
            openyuanrong_sandbox.yr_sandbox.Sandbox,
            "delete",
        ) as delete:
            self.backend.delete_named("worker")
        delete.assert_called_once_with("default-worker")


class OpenYuanRongSdkBackendTest(unittest.TestCase):
    def setUp(self):
        self.config = BackendConfig(
            api_endpoint=Endpoint("api.example", 443, "https", True),
            gateway_endpoint=Endpoint("gateway.example", 80, "http", True),
            token="secret",
        )
        initialized = patch.object(openyuanrong_sdk._impl, "ensure_initialized")
        initialized.start()
        self.addCleanup(initialized.stop)
        self.backend = openyuanrong_sdk.OpenYuanRongSdkBackend(self.config)

    def test_physical_id_failure_rolls_back_created_actor(self):
        instance = MagicMock()
        physical_id_error = RuntimeError("physical ID unavailable")
        with (
            patch.object(
                openyuanrong_sdk._impl,
                "build_options",
                return_value=MagicMock(),
            ),
            patch.object(
                openyuanrong_sdk._impl,
                "create_instance",
                return_value=instance,
            ),
            patch.object(
                openyuanrong_sdk._impl,
                "real_instance_id",
                side_effect=physical_id_error,
            ),
            patch.object(
                openyuanrong_sdk._impl,
                "terminate_instance",
            ) as terminate,
            self.assertRaisesRegex(
                BackendOperationError,
                "physical ID unavailable",
            ) as raised,
        ):
            self.backend.create(_spec())

        self.assertIs(raised.exception.__cause__, physical_id_error)
        terminate.assert_called_once_with(instance)

    def test_rollback_failure_does_not_replace_physical_id_error(self):
        instance = MagicMock()
        physical_id_error = RuntimeError("physical ID unavailable")
        with (
            patch.object(
                openyuanrong_sdk._impl,
                "build_options",
                return_value=MagicMock(),
            ),
            patch.object(
                openyuanrong_sdk._impl,
                "create_instance",
                return_value=instance,
            ),
            patch.object(
                openyuanrong_sdk._impl,
                "real_instance_id",
                side_effect=physical_id_error,
            ),
            patch.object(
                openyuanrong_sdk._impl,
                "terminate_instance",
                side_effect=RuntimeError("rollback failed"),
            ) as terminate,
            self.assertLogs(openyuanrong_sdk.logger, level="WARNING"),
            self.assertRaisesRegex(
                BackendOperationError,
                "physical ID unavailable",
            ) as raised,
        ):
            self.backend.create(_spec())

        self.assertIs(raised.exception.__cause__, physical_id_error)
        terminate.assert_called_once_with(instance)

    def test_termination_failure_still_closes_reverse_tunnel(self):
        instance = MagicMock()
        tunnel_client = MagicMock()
        terminate_error = RuntimeError("remote delete failed")
        with (
            patch.object(
                openyuanrong_sdk._impl,
                "build_options",
                return_value=MagicMock(),
            ),
            patch.object(
                openyuanrong_sdk._impl,
                "create_instance",
                return_value=instance,
            ),
            patch.object(
                openyuanrong_sdk._impl,
                "real_instance_id",
                return_value="physical-id",
            ),
            patch.object(
                openyuanrong_sdk._impl,
                "start_reverse_tunnel",
                return_value=tunnel_client,
            ),
            patch.object(
                openyuanrong_sdk._impl,
                "terminate_instance",
                side_effect=[terminate_error, None],
            ) as terminate,
        ):
            session = self.backend.create(
                _spec(reverse_tunnel=HttpReverseTunnel("http://127.0.0.1:9000"))
            )
            with self.assertRaisesRegex(
                BackendOperationError,
                "remote delete failed",
            ) as raised:
                session.terminate()
            session.close()
            session.terminate()
            session.terminate()

        self.assertIs(raised.exception.__cause__, terminate_error)
        self.assertEqual(terminate.call_count, 2)
        tunnel_client.stop.assert_called_once_with()

    def test_close_finalizes_actor_sdk(self):
        with patch.object(openyuanrong_sdk._impl, "finalize") as finalize:
            self.backend.close()

        finalize.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
