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

"""Creation ownership applies to every public-constructor rollback stage."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import httpx

from akernel_sdk import DockerfileLaunch, LocalDockerContext, Sandbox
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends.adx import AdxBackend
from akernel_sdk._backends.base import BackendConfig
from akernel_sdk._dockerfile import DockerfileBuildError


class CreationRollbackTest(unittest.TestCase):
    def backend(self):
        endpoint = Endpoint("fixture.invalid", 443, "https", True)
        return AdxBackend(BackendConfig(
            api_endpoint=endpoint, gateway_endpoint=endpoint, token="fixture",
        ))

    def transport(self, ownership, calls):
        def send(_client, request, **_kwargs):
            body = json.loads(request.content) if request.content else {}
            action = body.get("action")
            calls.append(action or request.method)
            if request.method == "DELETE":
                return httpx.Response(200, json={}, request=request)
            if action == "process.list":
                return httpx.Response(200, json={"processes": []}, request=request)
            if action == "process.exec":
                return httpx.Response(200, json={
                    "stdout": "", "stderr": "RUN failed", "exit_code": 1,
                }, request=request)
            if action:
                raise AssertionError("unexpected data operation " + action)
            result = {"environmentId": "default-shared", "status": "running"}
            if ownership is not None:
                result["newlyCreated"] = ownership
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content="event: final\ndata: " + json.dumps(result) + "\n\n",
                request=request,
            )
        return send

    def failed_constructor(self, stage, ownership):
        calls = []
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(
                httpx.Client, "send", autospec=True,
                side_effect=self.transport(ownership, calls),
            ))
            stack.enter_context(patch(
                "akernel_sdk.sandbox.load_backend", return_value=self.backend(),
            ))
            kwargs = dict(name="shared", detached=True, cpu=2000, memory=4096)
            expected = DockerfileBuildError
            if stage == "facade":
                expected = RuntimeError
                stack.enter_context(patch(
                    "akernel_sdk.sandbox.Pty", side_effect=RuntimeError("facade"),
                ))
            else:
                instruction = (
                    "COPY missing-input.txt /tmp/missing-input.txt"
                    if stage == "copy" else "RUN false"
                )
                kwargs["dockerfile"] = DockerfileLaunch(LocalDockerContext(
                    "FROM ubuntu:24.04\n" + instruction + "\n",
                    context_dir=directory,
                ), auto_start_cmd=False, run_timeout=1)
            with self.assertRaises(expected):
                Sandbox(**kwargs)
        if stage == "run":
            self.assertIn("process.exec", calls)
        return calls

    def test_public_failures_delete_only_confirmed_new_instances(self):
        for stage in ("facade", "copy", "run"):
            for ownership in (True, False, None, "true"):
                with self.subTest(stage=stage, ownership=ownership):
                    calls = self.failed_constructor(stage, ownership)
                    self.assertEqual(
                        calls.count("DELETE"), int(ownership is True), calls,
                    )

    def test_explicit_kill_still_deletes_a_reused_instance(self):
        calls = []
        with patch.object(
            httpx.Client, "send", autospec=True,
            side_effect=self.transport(False, calls),
        ), patch("akernel_sdk.sandbox.load_backend", return_value=self.backend()):
            sandbox = Sandbox(name="shared")
            self.assertNotIn("DELETE", calls)
            sandbox.kill()
        self.assertEqual(calls.count("DELETE"), 1)
