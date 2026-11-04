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

"""Named ownership, retry convergence, and idle lifecycle contracts."""

import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from akernel_sdk import (
    BackendOperationError,
    DockerfileLaunch,
    LocalDockerContext,
    Sandbox,
)
from akernel_sdk._backends import adx
from akernel_sdk._dockerfile import DockerfileBuildError

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class LifecycleFeaturesIntegrationTest(unittest.TestCase):
    def test_detached_name_remains_reserved_after_client_close(self):
        name = f"e2e-detached-{uuid.uuid4().hex[:12]}"
        first = None
        reopened = None
        try:
            first = Sandbox(
                name=name, detached=True, runtime=_RUNTIME, cpu=1000, memory=2048
            )
            self.assertTrue(first.is_running())
            first_id = first.id
            first.kill()
            reopened = Sandbox(
                name=name, detached=True, runtime=_RUNTIME, cpu=1000, memory=2048
            )
            self.assertEqual(reopened.id, first_id)
            self.assertEqual(
                reopened.commands.run("printf reopened").stdout, "reopened"
            )
        finally:
            if reopened is not None:
                reopened.kill()
            if first is not None:
                first.kill()
                Sandbox.delete(name)

    def test_same_name_reuses_successful_result_and_original_resources(self):
        name = f"e2e-reuse-{uuid.uuid4().hex[:12]}"
        first = None
        reopened = None
        try:
            first = Sandbox(
                name=name, detached=True, runtime=_RUNTIME, cpu=1000, memory=2048
            )
            reopened = Sandbox(
                name=name,
                detached=True,
                runtime=_RUNTIME,
                cpu=2000,
                memory=4096,
            )
            self.assertEqual(reopened.id, first.id)
            info = reopened.get_info()
            self.assertEqual(info.cpu, 1000)
            self.assertEqual(info.memory, 2048)
            self.assertEqual(reopened.commands.run("printf reused").stdout, "reused")
            self.assertEqual(first.commands.run("printf owner").stdout, "owner")
        finally:
            if reopened is not None:
                reopened.kill()
            if first is not None:
                first.kill()
                Sandbox.delete(name)

    def test_failed_named_reuse_keeps_the_original_workload(self):
        name = f"e2e-rollback-{uuid.uuid4().hex[:12]}"
        first = None
        try:
            first = Sandbox(
                name=name, detached=True, runtime=_RUNTIME, cpu=1000, memory=2048
            )
            path = "/tmp/akernel-cwd-is-a-file"
            first.files.write(path, "preserved original workload")
            with self.assertRaises(BackendOperationError):
                Sandbox(
                    name=name, detached=True, runtime=_RUNTIME,
                    cpu=2000, memory=4096, cwd=path,
                )
            self.assertEqual(first.commands.run("printf after-cwd").stdout, "after-cwd")
            # Inject a client-side readiness failure after the real create/reuse
            # RPC. The original runtime must survive this separate rollback path.
            with patch.object(
                adx._CommandsDriver, "list",
                side_effect=BackendOperationError("injected route readiness failure"),
            ), self.assertRaises(BackendOperationError):
                Sandbox(
                    name=name, detached=True, runtime=_RUNTIME,
                    cpu=2000, memory=4096,
                )
            self.assertEqual(
                first.commands.run("printf after-route").stdout, "after-route"
            )
            self.assertEqual(first.files.read(path), "preserved original workload")
            self.assertEqual(first.get_info().cpu, 1000)
            self.assertEqual(first.get_info().memory, 2048)
        finally:
            if first is not None:
                first.kill()
                Sandbox.delete(name)

    def test_failed_new_initialization_releases_the_named_workload(self):
        name = f"e2e-new-rollback-{uuid.uuid4().hex[:12]}"
        replacement = None
        try:
            with self.assertRaises(BackendOperationError):
                Sandbox(
                    name=name, detached=True, runtime=_RUNTIME,
                    cpu=1000, memory=2048, cwd="/proc/akernel-review",
                )
            replacement = Sandbox(
                name=name, detached=True, runtime=_RUNTIME,
                cpu=2000, memory=4096,
            )
            self.assertEqual(replacement.get_info().cpu, 2000)
            self.assertEqual(replacement.get_info().memory, 4096)
            self.assertEqual(replacement.commands.run("printf new").stdout, "new")
        finally:
            if replacement is not None:
                replacement.kill()
            Sandbox.delete(name)

    def test_failed_dockerfile_reuse_preserves_the_original_workload(self):
        name = f"e2e-dockerfile-rollback-{uuid.uuid4().hex[:12]}"
        first = None
        try:
            first = Sandbox(
                name=name, detached=True, runtime=_RUNTIME, cpu=1000, memory=2048
            )
            canary = "/tmp/public-rollback-canary"
            first.files.write(canary, "original workload")
            with tempfile.TemporaryDirectory() as directory:
                for instruction in (
                    "COPY missing-input /tmp/missing-input", "RUN false",
                ):
                    with self.subTest(instruction=instruction):
                        with self.assertRaises(DockerfileBuildError):
                            Sandbox(
                                name=name, detached=True, runtime=_RUNTIME,
                                cpu=2000, memory=4096,
                                dockerfile=DockerfileLaunch(LocalDockerContext(
                                    "FROM ubuntu:24.04\n" + instruction + "\n",
                                    context_dir=directory,
                                ), auto_start_cmd=False, run_timeout=5),
                            )
                        self.assertEqual(
                            first.files.read(canary), "original workload"
                        )
                        self.assertEqual(first.get_info().cpu, 1000)
                        self.assertEqual(
                            first.commands.run("printf survived").stdout, "survived"
                        )
        finally:
            if first is not None:
                first.kill()
                Sandbox.delete(name)

    def test_failed_public_facade_new_create_releases_the_name(self):
        name = f"e2e-facade-rollback-{uuid.uuid4().hex[:12]}"
        replacement = None
        try:
            with patch(
                "akernel_sdk.sandbox.Pty", side_effect=RuntimeError("facade fault"),
            ), self.assertRaisesRegex(RuntimeError, "facade fault"):
                Sandbox(
                    name=name, detached=True, runtime=_RUNTIME,
                    cpu=1000, memory=2048,
                )
            replacement = Sandbox(
                name=name, detached=True, runtime=_RUNTIME, cpu=2000, memory=4096,
            )
            self.assertEqual(replacement.get_info().cpu, 2000)
            self.assertEqual(replacement.get_info().memory, 4096)
            self.assertEqual(replacement.commands.run("printf ready").stdout, "ready")
        finally:
            if replacement is not None:
                replacement.kill()
            Sandbox.delete(name)

    def test_reload_without_checkpoint_does_not_create_a_new_runtime(self):
        with Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048) as sandbox:
            sandbox_id = sandbox.id
            self.assertFalse(sandbox.reload())
            self.assertEqual(sandbox.id, sandbox_id)
            self.assertEqual(sandbox.commands.run("printf alive").stdout, "alive")


if __name__ == "__main__":
    unittest.main()
