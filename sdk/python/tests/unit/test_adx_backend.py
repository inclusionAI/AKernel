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

from adx_sandbox._transport import SandboxHTTPError

from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends import adx
from akernel_sdk._backends.base import BackendConfig, SandboxSpec
from akernel_sdk.types import HttpReverseTunnel, Mount, NetworkPolicy, S3Config


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
        "failover": False,
        "inherit_entrypoint": False,
        "node_id": None,
        "xpu": None,
        "storage_mb": None,
        "network_policy": None,
        "extra_config": MappingProxyType({}),
    }
    values.update(overrides)
    return SandboxSpec(**values)


class AdxBackendTest(unittest.TestCase):
    def setUp(self):
        self.config = BackendConfig(
            api_endpoint=Endpoint("api.example", 443, "https", True),
            gateway_endpoint=Endpoint("gateway.example", 8443, "https", True),
            token="secret",
        )

    def test_reload_waits_for_new_data_route_without_repeating_reload(self):
        backend = adx.AdxBackend(self.config)
        native = MagicMock(id="default-worker")
        with patch.object(adx, "_OwnedSandbox", return_value=native):
            session = backend.create(_spec())
        native.commands.list.reset_mock()
        native.commands.list.side_effect = [
            SandboxHTTPError(409, {}, "stale route"), []
        ]
        native.reload.return_value = True
        self.assertTrue(session.reload())
        native.reload.assert_called_once_with()
        self.assertEqual(native.commands.list.call_count, 2)

    def test_reload_route_conflict_wait_is_bounded(self):
        backend = adx.AdxBackend(self.config)
        native = MagicMock(id="default-worker")
        with patch.object(adx, "_OwnedSandbox", return_value=native):
            session = backend.create(_spec())
        native.commands.list.reset_mock()
        native.commands.list.side_effect = SandboxHTTPError(409, {}, "stale route")
        native.reload.return_value = True
        with patch.object(adx.time, "monotonic", side_effect=[0, 10]):
            self.assertFalse(session.reload())
        native.reload.assert_called_once_with()
        native.commands.list.assert_called_once_with()

    def test_reload_does_not_retry_terminal_route_errors(self):
        backend = adx.AdxBackend(self.config)
        native = MagicMock(id="default-worker")
        with patch.object(adx, "_OwnedSandbox", return_value=native):
            session = backend.create(_spec())
        native.commands.list.reset_mock()
        native.commands.list.side_effect = SandboxHTTPError(403, {}, "forbidden")
        native.reload.return_value = True
        self.assertFalse(session.reload())
        native.reload.assert_called_once_with()
        native.commands.list.assert_called_once_with()

    def test_connection_is_explicit_and_does_not_mutate_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            backend = adx.AdxBackend(self.config)

        connection = backend._connection
        self.assertEqual(connection.server_address, "api.example:443")
        self.assertEqual(connection.gateway_address, "gateway.example:8443")
        self.assertEqual(connection.token, "secret")
        self.assertTrue(connection.use_tls)
        self.assertTrue(connection.gateway_use_tls)
        self.assertNotIn("ADX_SERVER_ADDRESS", os.environ)
        self.assertNotIn("ADX_TOKEN", os.environ)

    def test_create_maps_the_complete_akernel_spec(self):
        backend = adx.AdxBackend(self.config)
        native = MagicMock()
        native.id = "default-worker"
        native.commands = MagicMock()
        native.files = MagicMock()
        rootfs = S3Config("https://s3.example", "rootfs", "rootfs.img")
        mount = Mount(target="/tools", image_url="tools:v1")
        tunnel = HttpReverseTunnel("https://service.example")
        network = NetworkPolicy.deny_dns("github.com")

        with patch.object(adx, "_OwnedSandbox", return_value=native) as sandbox:
            session = backend.create(
                _spec(
                    rootfs=rootfs,
                    runtime="firecracker",
                    cpu=2000,
                    memory=8192,
                    cpu_limit=2500,
                    mem_limit=9216,
                    idle_timeout=600,
                    schedule_timeout=45,
                    env=MappingProxyType({"USER_VALUE": "preserved"}),
                    name="worker",
                    command_cwd="/workspace",
                    port_forwardings=(8080,),
                    mounts=(mount,),
                    reverse_tunnel=tunnel,
                    detached=True,
                    failover=True,
                    node_id="node-1",
                    xpu="gpu:A100:1",
                    storage_mb=10240,
                    network_policy=network,
                    extra_config=MappingProxyType({"featureFlag": True}),
                )
            )

        kwargs = sandbox.call_args.kwargs
        self.assertIsInstance(kwargs["rootfs"], adx.adx_sandbox.S3Config)
        self.assertEqual(kwargs["runtime"], "firecracker")
        self.assertEqual(kwargs["cpu"], 2000)
        self.assertEqual(kwargs["memory"], 8192)
        self.assertEqual(kwargs["cpu_limit"], 2500)
        self.assertEqual(kwargs["mem_limit"], 9216)
        self.assertEqual(kwargs["idle_timeout"], 600)
        self.assertEqual(kwargs["schedule_timeout"], 45)
        self.assertEqual(kwargs["create_timeout"], 105)
        self.assertEqual(kwargs["env"], {"USER_VALUE": "preserved"})
        self.assertEqual(kwargs["name"], "worker")
        self.assertEqual(kwargs["cwd"], "/workspace")
        self.assertEqual(kwargs["port_forwardings"], [8080])
        self.assertEqual(kwargs["upstream"], "https://service.example")
        self.assertEqual(kwargs["proxy_port"], tunnel.listen_port)
        self.assertEqual(kwargs["node_id"], "node-1")
        self.assertEqual(kwargs["xpu"], "gpu:A100:1")
        self.assertEqual(kwargs["storage_mb"], 10240)
        self.assertEqual(kwargs["extra_config"], {"featureFlag": True})
        self.assertTrue(kwargs["failover"])
        self.assertEqual(kwargs["data_plane_security"].tunnel_mode, "tls")
        self.assertEqual(kwargs["data_plane_security"].port_forward_mode, "tls")
        self.assertIs(kwargs["connection"], backend._connection)
        self.assertIsInstance(kwargs["mounts"][0], adx.adx_sandbox.Mount)
        self.assertIsInstance(kwargs["network"], adx.adx_sandbox.NetworkPolicy)
        self.assertEqual(session.id, "default-worker")
        native.commands.list.assert_called_once_with()

    def test_session_pty_uses_the_authenticated_tls_entrypoint(self):
        backend = adx.AdxBackend(self.config)
        native = MagicMock()
        native.id = "default-worker"
        native.commands = MagicMock()
        native.files = MagicMock()

        with (
            patch.object(adx, "_OwnedSandbox", return_value=native),
            patch.object(adx.adx_sandbox, "Pty") as pty,
        ):
            session = backend.create(_spec())

        connection = pty.call_args.kwargs["connection"]
        self.assertEqual(pty.call_args.args, ("default-worker",))
        self.assertEqual(connection.server_address, "api.example:443")
        self.assertIsNone(connection.gateway_address)
        self.assertTrue(connection.use_tls)
        self.assertIs(session.pty, pty.return_value)

    def test_session_uses_stable_id_delete_with_the_same_connection(self):
        backend = adx.AdxBackend(self.config)
        native = MagicMock()
        native.id = "default-worker"
        native.commands = MagicMock()
        native.files = MagicMock()
        with (
            patch.object(adx, "_OwnedSandbox", return_value=native),
            patch.object(adx.adx_sandbox.Sandbox, "delete") as delete,
        ):
            session = backend.create(_spec())
            session.terminate()

        native.close.assert_called_once_with()
        delete.assert_called_once_with(
            "default-worker",
            connection=backend._connection,
        )

    def test_get_info_keeps_akernel_resource_extensions(self):
        backend = adx.AdxBackend(self.config)
        native = MagicMock()
        native.id = "default-worker"
        native.commands = MagicMock()
        native.files = MagicMock()
        native.get_info.return_value = SimpleNamespace(
            id="default-worker",
            state="running",
            cpu=2000,
            memory=8192,
            image="worker:v1",
        )
        with patch.object(adx, "_OwnedSandbox", return_value=native):
            session = backend.create(
                _spec(xpu="gpu:A100:1", storage_mb=10240)
            )

        info = session.get_info()
        self.assertEqual(info.xpu, "gpu:A100:1")
        self.assertEqual(info.storage_mb, 10240)

    def test_get_info_normalizes_an_empty_backend_image(self):
        backend = adx.AdxBackend(self.config)
        native = MagicMock()
        native.id = "default-worker"
        native.commands = MagicMock()
        native.files = MagicMock()
        native.get_info.return_value = SimpleNamespace(
            id="default-worker",
            state="running",
            cpu=1000,
            memory=4096,
            image="",
        )
        with patch.object(adx, "_OwnedSandbox", return_value=native):
            session = backend.create(_spec())

        self.assertIsNone(session.get_info().image)

    def test_delete_named_uses_default_namespace_and_explicit_connection(self):
        backend = adx.AdxBackend(self.config)
        with patch.object(adx.adx_sandbox.Sandbox, "delete") as delete:
            backend.delete_named("worker")

        delete.assert_called_once_with(
            "default-worker",
            connection=backend._connection,
        )


if __name__ == "__main__":
    unittest.main()
