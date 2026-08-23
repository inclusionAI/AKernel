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

import json
import unittest
from unittest.mock import MagicMock, patch

from akernel_sdk import (
    HttpReverseTunnel,
    Mount,
    NetworkPolicy,
    NetworkRule,
    PortRange,
    S3Config,
)
from akernel_sdk._backends import openyuanrong_sdk_impl as _impl


class OpenYuanRongSdkImplTest(unittest.TestCase):
    def build_options(self, **overrides):
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
            "env": None,
            "name": None,
            "port_forwardings": [],
            "mounts": [],
            "reverse_tunnel": None,
            "detached": False,
            "node_id": None,
            "xpu": None,
            "storage_mb": None,
            "network_policy": None,
            "extra_config": {},
        }
        values.update(overrides)
        return _impl.build_options(**values)

    def test_oci_rootfs_wire_format(self):
        options = self.build_options(image="ubuntu:24.04")
        self.assertEqual(
            json.loads(options.custom_extensions["rootfs"]),
            {
                "runtime": "runsc",
                "type": "image",
                "readonly": False,
                "imageurl": "ubuntu:24.04",
            },
        )

    def test_s3_rootfs_wire_format(self):
        config = S3Config("https://s3.example.com", "rootfs", "ubuntu.img")
        options = self.build_options(rootfs=config)
        self.assertEqual(
            json.loads(options.custom_extensions["rootfs"]),
            {
                "runtime": "runsc",
                "type": "s3",
                "readonly": False,
                "storageInfo": config.to_dict(),
            },
        )

    def test_kata_passes_runtime_config_override_for_default_rootfs(self):
        options = self.build_options(runtime="kata")
        self.assertEqual(
            json.loads(options.custom_extensions["rootfs"]),
            {"runtime": "kata"},
        )

    def test_runsc_passes_runtime_config_override_for_default_rootfs(self):
        options = self.build_options(runtime="runsc")
        self.assertEqual(
            json.loads(options.custom_extensions["rootfs"]),
            {"runtime": "runsc"},
        )

    def test_mount_and_tunnel_translation(self):
        mount = Mount(target="/tools", image_url="ubuntu:24.04")
        tunnel = HttpReverseTunnel(
            "https://example.com", reverse_port=9100, listen_port=9101
        )
        options = self.build_options(
            port_forwardings=[8080], mounts=[mount], reverse_tunnel=tunnel
        )
        self.assertEqual(
            json.loads(options.custom_extensions["mounts"]), [mount.to_dict()]
        )
        self.assertEqual(
            [forwarding.port for forwarding in options.port_forwardings],
            [8080, 9100],
        )

    def test_resource_limit_validation(self):
        with self.assertRaisesRegex(ValueError, "cpu_limit"):
            self.build_options(cpu=2000, cpu_limit=1000)
        with self.assertRaisesRegex(ValueError, "mem_limit"):
            self.build_options(memory=4096, mem_limit=2048)
        for value in (0, -1):
            with (
                self.subTest(schedule_timeout=value),
                self.assertRaisesRegex(ValueError, "schedule_timeout"),
            ):
                self.build_options(schedule_timeout=value)

    def test_xpu_and_storage_translation_is_runtime_agnostic(self):
        options = self.build_options(
            runtime="gvisor-next", xpu="GPU:L20:2", storage_mb=256
        )
        self.assertEqual(
            options.custom_resources,
            {
                "GPU/l20/count": 2.0,
                "storage": float(256 * 1024 * 1024),
            },
        )
        self.assertEqual(
            json.loads(options.custom_extensions["rootfs"]),
            {"runtime": "gvisor-next"},
        )

    def test_network_policy_uses_custom_extension_wire_format(self):
        options = self.build_options(
            network_policy=NetworkPolicy.deny_dns("github.com", "*.github.com")
        )

        self.assertEqual(
            json.loads(options.custom_extensions["network_policy"]),
            {"dnsBlacklist": ["github.com", "*.github.com"]},
        )

    def test_acl_v2_uses_custom_extension_wire_format(self):
        policy = NetworkPolicy.allowlist(
            [
                NetworkRule(
                    domain="api.github.com",
                    protocol="tcp",
                    port_range=PortRange(80, 443),
                )
            ]
        )

        options = self.build_options(network_policy=policy)

        self.assertEqual(
            json.loads(options.custom_extensions["network_policy"]),
            policy.to_dict(),
        )

    def test_extra_config_uses_custom_extension_wire_format(self):
        options = self.build_options(
            runtime="custom-runtime",
            extra_config={"featureFlag": True, "labels": ["one", "two"]},
        )

        self.assertEqual(
            json.loads(options.custom_extensions["extra_config"]),
            {"featureFlag": True, "labels": ["one", "two"]},
        )

    def test_node_info_conversion(self):
        node = _impl._to_node_info(
            {
                "id": "node-1",
                "status": 0,
                "capacity": {"CPU": 8000, "Memory": {"value": 16384}},
                "allocatable": {"CPU": {"scalar": {"value": 6000}}},
                "labels": {"NODE_ID": "node-1"},
            }
        )
        self.assertEqual(node.id, "node-1")
        self.assertEqual(node.capacity["CPU"], 8000.0)
        self.assertEqual(node.capacity["Memory"], 16384.0)
        self.assertEqual(node.allocatable["CPU"], 6000.0)

    def test_resources_parse_vector_allocatable_counts(self):
        response = {
            "resource": {
                "fragment": {
                    "node-1": {
                        "id": "node-1",
                        "status": 0,
                        "capacity": {
                            "resources": {
                                "CPU": {"scalar": {"value": 4000}},
                                "GPU/l20": {
                                    "vectors": {
                                        "values": {
                                            "count": {
                                                "vectors": {
                                                    "node-1": {"values": [1, 1]}
                                                }
                                            }
                                        }
                                    }
                                },
                            }
                        },
                        "allocatable": {
                            "resources": {
                                "CPU": {"scalar": {"value": 3000}},
                                "GPU/l20": {
                                    "vectors": {
                                        "values": {
                                            "count": {
                                                "vectors": {
                                                    "node-1": {"values": [0, 1]}
                                                }
                                            }
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            }
        }
        with (
            patch.object(_impl, "ensure_initialized"),
            patch.object(_impl, "query_resource_view", return_value=response),
        ):
            result = _impl.resources()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "node-1")
        self.assertEqual(result[0].capacity["GPU/l20"], 2.0)
        self.assertEqual(result[0].allocatable["GPU/l20"], 1.0)

    def test_readiness_failure_rolls_back_created_actor(self):
        instance = MagicMock()
        invoke = MagicMock(return_value=instance)
        instance_type = MagicMock()
        instance_type.options.return_value.invoke = invoke
        readiness_error = RuntimeError("readiness failed")
        with (
            patch.object(_impl, "_SandboxInstance", instance_type),
            patch.object(
                _impl.yr,
                "get",
                side_effect=readiness_error,
            ),
            patch.object(_impl, "terminate_instance") as terminate,
            self.assertRaisesRegex(RuntimeError, "readiness failed") as raised,
        ):
            _impl.create_instance(MagicMock(), cwd="/workspace")

        self.assertIs(raised.exception, readiness_error)
        terminate.assert_called_once_with(instance)

    def test_readiness_rollback_failure_preserves_original_error(self):
        instance = MagicMock()
        instance_type = MagicMock()
        instance_type.options.return_value.invoke.return_value = instance
        readiness_error = RuntimeError("readiness failed")
        with (
            patch.object(_impl, "_SandboxInstance", instance_type),
            patch.object(
                _impl.yr,
                "get",
                side_effect=readiness_error,
            ),
            patch.object(
                _impl,
                "terminate_instance",
                side_effect=RuntimeError("rollback failed"),
            ) as terminate,
            self.assertLogs(_impl.logger, level="WARNING"),
            self.assertRaisesRegex(RuntimeError, "readiness failed") as raised,
        ):
            _impl.create_instance(MagicMock(), cwd=None)

        self.assertIs(raised.exception, readiness_error)
        terminate.assert_called_once_with(instance)


if __name__ == "__main__":
    unittest.main()
