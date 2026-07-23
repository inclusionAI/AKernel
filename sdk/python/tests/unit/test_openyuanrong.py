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
from unittest.mock import patch

from akernel_sdk import HttpReverseTunnel, Mount, S3Config, _openyuanrong


class OpenYuanRongAdapterTest(unittest.TestCase):
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
        }
        values.update(overrides)
        return _openyuanrong.build_options(**values)

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

    def test_kata_uses_the_default_local_rootfs(self):
        options = self.build_options(runtime="kata")
        self.assertEqual(
            json.loads(options.custom_extensions["rootfs"]),
            {
                "runtime": "kata",
                "type": "local",
                "readonly": False,
                "path": "/home/yuanrong/yr-runtime-rootfs.img",
            },
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

    def test_node_info_conversion(self):
        node = _openyuanrong._to_node_info(
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

    def test_resources_wrap_backend_values(self):
        with patch.object(_openyuanrong, "ensure_initialized"):
            with patch.object(
                _openyuanrong.yr,
                "resources",
                return_value=[
                    {
                        "id": "node-1",
                        "status": 0,
                        "capacity": {"CPU": 4000},
                        "allocatable": {"CPU": 3000},
                        "labels": {},
                    }
                ],
            ):
                result = _openyuanrong.resources()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "node-1")


if __name__ == "__main__":
    unittest.main()
