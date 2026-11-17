"""Restore rollback respects the server's authoritative create ownership."""

import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import httpx

from akernel_sdk import BackendOperationError, HttpReverseTunnel, Sandbox
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends.adx import AdxBackend
from akernel_sdk._backends.base import BackendConfig


class SnapshotOwnershipTest(unittest.TestCase):
    def exercise(self, stage, ownership):
        deletes = []
        endpoint = Endpoint("fixture.invalid", 443, "https", True)
        backend = AdxBackend(BackendConfig(endpoint, endpoint, "fixture"))

        def send(_client, request, **_kwargs):
            if request.method == "DELETE":
                deletes.append(request.url.path)
                return httpx.Response(200, json={}, request=request)
            body = json.loads(request.content) if request.content else {}
            if body.get("action") == "process.list":
                if stage == "route":
                    return httpx.Response(403, json={"message": "denied"},
                                          request=request)
                return httpx.Response(200, json={"processes": []}, request=request)
            if request.method == "GET":
                return httpx.Response(200, json={
                    "id": "default-shared", "status": "running",
                    "required_cpu": 1000, "required_mem": 1024,
                }, request=request)
            result = {"environmentId": "default-shared", "status": "running"}
            if ownership is not None:
                result["newlyCreated"] = ownership
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content="event: final\ndata: " + json.dumps(result) + "\n\n",
                request=request,
            )

        with ExitStack() as stack:
            stack.enter_context(patch.object(httpx.Client, "send", autospec=True,
                                             side_effect=send))
            stack.enter_context(patch("akernel_sdk.sandbox.load_backend",
                                      return_value=backend))
            options = dict(name="shared")
            expected = BackendOperationError
            if stage in {"tunnel", "interrupt"}:
                options["reverse_tunnel"] = HttpReverseTunnel(
                    target="http://127.0.0.1:9000"
                )
                tunnel = stack.enter_context(patch(
                    "adx_environment.tunnel_client.TunnelClient"
                ))
                tunnel.return_value.start.return_value = False
                if stage == "interrupt":
                    expected = KeyboardInterrupt
                    tunnel.return_value.start.side_effect = KeyboardInterrupt()
            elif stage in {"session", "facade"}:
                expected = RuntimeError
                target = ("akernel_sdk._backends.adx._Session" if stage == "session"
                          else "akernel_sdk.sandbox.Pty")
                stack.enter_context(patch(target, side_effect=RuntimeError(stage)))
            with self.assertRaises(expected):
                Sandbox.from_snapshot("saved", **options)
        return deletes

    def test_restore_failures_delete_only_confirmed_new_instances(self):
        for stage in ("tunnel", "interrupt", "route", "session", "facade"):
            for ownership in (True, False, None, "true"):
                with self.subTest(stage=stage, ownership=ownership):
                    deletes = self.exercise(stage, ownership)
                    self.assertEqual(len(deletes), int(ownership is True), deletes)
