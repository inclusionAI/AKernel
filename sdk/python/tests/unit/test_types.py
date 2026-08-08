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
import subprocess
import sys
import unittest
from pathlib import Path

import akernel_sdk
from akernel_sdk import HttpReverseTunnel, Mount, S3Config


class PublicTypesTest(unittest.TestCase):
    def test_lightweight_imports_do_not_load_yuanrong(self):
        package_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(package_root)
        code = """
import os
import sys
os.environ.pop('YR_HTTP_CONNECTION_NUM', None)
import akernel_sdk
import akernel_sdk.cli
assert 'yr' not in sys.modules
assert 'yr_sandbox' not in sys.modules
assert 'YR_HTTP_CONNECTION_NUM' not in os.environ
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_exports_are_minimal(self):
        self.assertEqual(
            set(akernel_sdk.__all__),
            {
                "Sandbox",
                "S3Config",
                "Mount",
                "NetworkPolicy",
                "HttpReverseTunnel",
                "CommandResult",
                "CommandInfo",
                "CommandHandle",
                "EntryInfo",
                "SandboxInfo",
                "NodeInfo",
                "Pty",
                "PtySession",
                "PtyError",
                "resources",
                "get_backend",
                "InvalidBackendError",
                "BackendNotInstalledError",
                "UnsupportedBackendFeatureError",
                "BackendOperationError",
            },
        )
        for removed in (
            "SimpleSandbox",
            "PersistentBashSandbox",
            "PersistentBashInstance",
            "create_persistent",
            "PortForwarding",
            "Shells",
            "Shell",
        ):
            self.assertFalse(hasattr(akernel_sdk, removed), removed)

    def test_s3_config_serialization(self):
        config = S3Config(
            endpoint="https://s3.example.com",
            bucket="rootfs",
            object="ubuntu/rootfs.img",
            access_key="access",
            secret_key="secret",
        )
        self.assertEqual(
            config.to_dict(),
            {
                "endpoint": "https://s3.example.com",
                "bucket": "rootfs",
                "object": "ubuntu/rootfs.img",
                "accessKey": "access",
                "secretKey": "secret",
            },
        )
        self.assertNotIn("secret", repr(config))

    def test_s3_config_requires_location(self):
        with self.assertRaisesRegex(ValueError, "bucket"):
            S3Config(endpoint="https://s3.example.com", bucket="", object="rootfs")

    def test_mount_accepts_one_source(self):
        image_mount = Mount(target="/opt/tools", image_url="ubuntu:24.04")
        self.assertEqual(image_mount.to_dict()["image_url"], "ubuntu:24.04")

        s3 = S3Config("https://s3.example.com", "data", "weights.img")
        s3_mount = Mount(target="/weights", s3_config=s3, type="erofs")
        self.assertEqual(s3_mount.to_dict()["s3_config"], s3.to_dict())

        with self.assertRaisesRegex(ValueError, "exactly one"):
            Mount(target="/data")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            Mount(target="/data", image_url="image", s3_config=s3)

    def test_mount_requires_absolute_target(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            Mount(target="relative", image_url="ubuntu:24.04")

    def test_http_reverse_tunnel_supports_http_and_https(self):
        self.assertEqual(
            HttpReverseTunnel("http://127.0.0.1:8000").url,
            "http://127.0.0.1:8766",
        )
        self.assertEqual(
            HttpReverseTunnel(
                "https://example.com",
                reverse_port=9000,
                listen_port=9001,
            ).url,
            "http://127.0.0.1:9001",
        )
        self.assertEqual(HttpReverseTunnel("localhost:8000").target, "localhost:8000")

    def test_http_reverse_tunnel_validation(self):
        with self.assertRaisesRegex(ValueError, "scheme"):
            HttpReverseTunnel("ftp://example.com")
        with self.assertRaisesRegex(ValueError, "invalid port"):
            HttpReverseTunnel("http://example.com:invalid")
        with self.assertRaisesRegex(ValueError, "different"):
            HttpReverseTunnel("example.com", reverse_port=9000, listen_port=9000)
        with self.assertRaisesRegex(ValueError, "between"):
            HttpReverseTunnel("example.com", reverse_port=0)
        with self.assertRaisesRegex(ValueError, "greater than 0"):
            HttpReverseTunnel("example.com", connect_timeout=0)


if __name__ == "__main__":
    unittest.main()
