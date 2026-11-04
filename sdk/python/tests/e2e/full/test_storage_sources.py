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

"""Full-deployment image, S3 rootfs, mount, and device contracts."""

import os
import unittest

from akernel_sdk import Mount, S3Config, Sandbox

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


def _s3_config(object_name):
    return S3Config(
        endpoint=os.environ["AKERNEL_S3_ENDPOINT"],
        bucket=os.environ["AKERNEL_S3_BUCKET"],
        object=object_name,
        access_key=os.environ.get("AKERNEL_S3_ACCESS_KEY"),
        secret_key=os.environ.get("AKERNEL_S3_SECRET_KEY"),
    )


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class StorageSourcesIntegrationTest(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("AKERNEL_TEST_ENTRYPOINT_IMAGE"),
        "set AKERNEL_TEST_ENTRYPOINT_IMAGE to a short-lived OCI entrypoint",
    )
    def test_inherited_oci_entrypoint_reports_exit(self):
        with Sandbox(
            image=os.environ["AKERNEL_TEST_ENTRYPOINT_IMAGE"],
            inherit_entrypoint=True,
            runtime=_RUNTIME,
            cpu=1000,
            memory=2048,
        ) as sandbox:
            self.assertIsNone(sandbox.startup_command)
            self.assertEqual(sandbox.wait_entrypoint(), 0)
            self.assertIsNotNone(sandbox.entrypoint_exit_info)
            self.assertTrue(sandbox.is_running())

    @unittest.skipUnless(
        os.environ.get("AKERNEL_TEST_IMAGE"), "set AKERNEL_TEST_IMAGE to an OCI image"
    )
    def test_oci_root_has_private_writes(self):
        image = os.environ["AKERNEL_TEST_IMAGE"]
        with Sandbox(image=image, runtime=_RUNTIME, cpu=1000, memory=2048) as first:
            original = first.files.read("/etc/os-release")
            first.files.write("/etc/os-release", "E2E_PRIVATE_ROOT\n")
            with Sandbox(
                image=image, runtime=_RUNTIME, cpu=1000, memory=2048
            ) as second:
                self.assertEqual(second.files.read("/etc/os-release"), original)
                self.assertEqual(
                    first.files.read("/etc/os-release"), "E2E_PRIVATE_ROOT\n"
                )

    @unittest.skipUnless(
        os.environ.get("AKERNEL_TEST_MOUNT_IMAGE"),
        "set AKERNEL_TEST_MOUNT_IMAGE to a resolvable OCI image",
    )
    def test_oci_mount_is_read_only(self):
        mount = Mount(
            target="/mnt/e2e-image",
            image_url=os.environ["AKERNEL_TEST_MOUNT_IMAGE"],
        )
        with Sandbox(
            runtime=_RUNTIME, mounts=[mount], cpu=1000, memory=2048
        ) as sandbox:
            found = sandbox.commands.run("test -e /mnt/e2e-image/etc/os-release")
            self.assertEqual(found.exit_code, 0, found.stderr)
            write = sandbox.commands.run("touch /mnt/e2e-image/e2e-write")
            self.assertNotEqual(write.exit_code, 0)

    @unittest.skipUnless(
        all(
            os.environ.get(key)
            for key in (
                "AKERNEL_S3_ENDPOINT",
                "AKERNEL_S3_BUCKET",
                "AKERNEL_S3_ROOTFS_OBJECT",
            )
        ),
        "set AKERNEL_S3_ENDPOINT/BUCKET/ROOTFS_OBJECT",
    )
    def test_s3_rootfs_starts_with_expected_files(self):
        rootfs = _s3_config(os.environ["AKERNEL_S3_ROOTFS_OBJECT"])
        with Sandbox(rootfs=rootfs, runtime=_RUNTIME, cpu=1000, memory=2048) as sandbox:
            result = sandbox.commands.run("test -s /etc/os-release")
            self.assertEqual(result.exit_code, 0, result.stderr)

    @unittest.skipUnless(
        all(
            os.environ.get(key)
            for key in (
                "AKERNEL_S3_ENDPOINT",
                "AKERNEL_S3_BUCKET",
                "AKERNEL_S3_MOUNT_OBJECT",
            )
        ),
        "set AKERNEL_S3_ENDPOINT/BUCKET/MOUNT_OBJECT",
    )
    def test_s3_mount_is_read_only(self):
        mount = Mount(
            target="/mnt/e2e-s3",
            type="erofs",
            s3_config=_s3_config(os.environ["AKERNEL_S3_MOUNT_OBJECT"]),
        )
        with Sandbox(
            runtime=_RUNTIME, mounts=[mount], cpu=1000, memory=2048
        ) as sandbox:
            found = sandbox.commands.run("test -d /mnt/e2e-s3")
            self.assertEqual(found.exit_code, 0, found.stderr)
            write = sandbox.commands.run("touch /mnt/e2e-s3/e2e-write")
            self.assertNotEqual(write.exit_code, 0)

    @unittest.skipUnless(
        os.environ.get("AKERNEL_TEST_GPU_XPU"),
        "requires a GPU worker and AKERNEL_TEST_GPU_XPU",
    )
    def test_whole_gpu_is_visible(self):
        with Sandbox(
            runtime=_RUNTIME,
            xpu=os.environ["AKERNEL_TEST_GPU_XPU"],
            cpu=1000,
            memory=2048,
        ) as sandbox:
            result = sandbox.commands.run("nvidia-smi -L")
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertIn("GPU", result.stdout)


if __name__ == "__main__":
    unittest.main()
