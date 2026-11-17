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

"""Actionable scheduling failures retain authoritative retry/outcome semantics."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from adx_environment._transport import EnvironmentHTTPError

from akernel_sdk import (
    BackendOperationError,
    InsufficientResourcesError,
    SchedulingError,
    TimeoutException,
)
from akernel_sdk._addresses import Endpoint
from akernel_sdk._backends import adx
from akernel_sdk._backends.base import BackendConfig
from tests.unit.test_adx_backend import _spec


def diagnosis(**overrides):
    result = {
        "nodeCount": 15,
        "eligibleNodes": 0,
        "requested": {
            "cpu_millis": 2000,
            "memory_bytes": 8 * 1024**3,
            "disk_bytes": 20 * 1024**3,
        },
        "maxCapacity": {"memory_bytes": 32 * 1024**3},
        "maxAvailable": {"memory_bytes": 32 * 1024**3},
        "rejections": {"insufficient_memory": 14, "runtime_fit": 1},
    }
    result.update(overrides)
    return result


def native_error(details, **overrides):
    context = {
        "code": "DEADLINE_EXCEEDED",
        "retry": "never",
        "outcome": "not_started",
        "request_id": "create-test",
        "environment_id": "default-test",
        "scheduling_failure": details,
    }
    context.update(overrides)
    return EnvironmentHTTPError(504, {}, "native scheduling diagnostics", **context)


class SchedulingErrorTest(unittest.TestCase):
    def convert(self, details, **overrides):
        return adx._convert_error("create sandbox", native_error(details, **overrides))

    def test_mixed_memory_runtime_failure_explains_all_blockers(self):
        details = diagnosis()
        error = self.convert(details)
        self.assertIsInstance(error, InsufficientResourcesError)
        self.assertIsInstance(error, SchedulingError)
        self.assertIsInstance(error, BackendOperationError)
        self.assertEqual(error.resource_shortages, ("memory",))
        self.assertEqual(error.reasons, ("insufficient_memory", "runtime_fit"))
        text = str(error)
        for phrase in (
            "insufficient resources",
            "2 CPU",
            "8 GiB memory",
            "20 GiB disk",
            "insufficient memory (14)",
            "unsupported runtime (1)",
            "release idle sandboxes",
            "requested runtime",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("32 GiB", text)
        self.assertNotIn("native scheduling diagnostics", text)
        self.assertIs(error.scheduling_failure, details)

    def test_each_scalar_shortage_is_actionable_and_preserves_units(self):
        for resource in ("cpu", "memory", "disk"):
            with self.subTest(resource=resource):
                error = self.convert(
                    diagnosis(
                        rejections={f"insufficient_{resource}": 15},
                        requested={
                            "cpu_millis": 1500,
                            "memory_bytes": 512 * 1024**2,
                            "disk_bytes": 1536 * 1024**2,
                        },
                    )
                )
                self.assertIsInstance(error, InsufficientResourcesError)
                self.assertEqual(error.resource_shortages, (resource,))
                self.assertIn("1.5 CPU, 512 MiB memory, 1.5 GiB disk", str(error))
                self.assertTrue(
                    any(resource in hint.lower() for hint in error.suggestions)
                )

    def test_nonresource_blockers_have_specific_guidance(self):
        for reason, advice in (
            ("runtime_fit", "runtime"),
            ("device_fit", "accelerator"),
            ("node_available", "health"),
            ("node_affinity", "placement"),
            ("placement_groups", "placement"),
            ("environment_affinity", "placement"),
            ("topology_spread", "placement"),
            ("waiting_for_volume", "volume"),
            ("volume_topology", "storage"),
        ):
            with self.subTest(reason=reason):
                error = self.convert(diagnosis(rejections={reason: 15}))
                self.assertIsInstance(error, SchedulingError)
                self.assertNotIsInstance(error, InsufficientResourcesError)
                self.assertEqual(error.reasons, (reason,))
                self.assertEqual(error.resource_shortages, ())
                self.assertTrue(any(advice in hint for hint in error.suggestions))
                self.assertNotIn("insufficient resources", str(error))

    def test_no_workers_is_distinct_from_insufficient_capacity(self):
        for reasons in ({}, {"no_workers": 1}):
            with self.subTest(rejections=reasons):
                error = self.convert(diagnosis(nodeCount=0, rejections=reasons))
                self.assertIsInstance(error, SchedulingError)
                self.assertNotIsInstance(error, InsufficientResourcesError)
                self.assertEqual(error.reasons, ("no_workers",))
                self.assertIn("no workers", str(error))
                self.assertIn("register", " ".join(error.suggestions))

    def test_eligible_workers_do_not_imply_cluster_shortage(self):
        error = self.convert(diagnosis(eligibleNodes=1))
        self.assertIsInstance(error, SchedulingError)
        self.assertNotIsInstance(error, InsufficientResourcesError)
        self.assertEqual(error.reasons, ("queue_timeout",))
        self.assertEqual(error.resource_shortages, ())
        self.assertIn("1 eligible worker", str(error))
        self.assertIn("schedule_timeout", " ".join(error.suggestions))
        self.assertNotIn("insufficient memory", str(error))

    def test_zero_eligible_workers_without_rejections_remains_unexplained(self):
        details = diagnosis(nodeCount=15, eligibleNodes=0, rejections={})
        error = self.convert(details)
        self.assertIsInstance(error, SchedulingError)
        self.assertNotIsInstance(error, InsufficientResourcesError)
        self.assertEqual(error.reasons, ("unknown",))
        self.assertEqual(error.resource_shortages, ())
        self.assertIs(error.scheduling_failure, details)
        self.assertIn("without a usable placement diagnosis", str(error))
        self.assertIn("cluster operator", " ".join(error.suggestions))
        self.assertNotIn("no workers", str(error))
        self.assertEqual((error.retry, error.outcome), ("never", "not_started"))

    def test_incomplete_or_malformed_diagnoses_do_not_invent_shortages(self):
        for details in (
            {},
            {"rejections": {"insufficient_memory": 3}},
            diagnosis(eligibleNodes=True),
            diagnosis(eligibleNodes=-1),
            diagnosis(eligibleNodes="0"),
            diagnosis(eligibleNodes=16),
            diagnosis(rejections=None),
            diagnosis(rejections=["insufficient_memory"]),
            diagnosis(rejections={"insufficient_memory": True}),
            diagnosis(rejections={"insufficient_memory": -1}),
            diagnosis(rejections={"insufficient_memory": 0}),
            diagnosis(rejections={"insufficient_memory": "14"}),
            diagnosis(rejections={"insufficient_memory": 16}),
            diagnosis(rejections={"no_workers": 1}),
        ):
            with self.subTest(details=details):
                error = self.convert(details)
                self.assertIsInstance(error, SchedulingError)
                self.assertNotIsInstance(error, InsufficientResourcesError)
                self.assertEqual(error.resource_shortages, ())
                self.assertEqual(error.reasons, ("unknown",))
                self.assertNotIn("insufficient memory", str(error))
                self.assertIs(error.scheduling_failure, details)

    def test_future_reasons_remain_inspectable_without_fabricated_advice(self):
        error = self.convert(diagnosis(rejections={"future_filter": 15}))
        self.assertIsInstance(error, SchedulingError)
        self.assertNotIsInstance(error, InsufficientResourcesError)
        self.assertEqual(error.reasons, ("future_filter",))
        self.assertIn("other placement constraint", str(error))
        self.assertIn("cluster operator", " ".join(error.suggestions))

    def test_bad_requested_resources_do_not_mask_a_valid_rejection(self):
        for resources in (None, [], {"cpu_millis": True}, {"memory_bytes": -1}):
            with self.subTest(resources=resources):
                error = self.convert(diagnosis(requested=resources))
                self.assertIsInstance(error, InsufficientResourcesError)
                self.assertIn("insufficient memory", str(error))
                self.assertNotIn("Requested:", str(error))

    def test_only_authoritative_cancelled_scheduling_errors_are_classified(self):
        for overrides in (
            {"retry": "same_operation", "outcome": "unknown"},
            {"retry": "after_backoff"},
            {"outcome": "terminal"},
            {"outcome": None},
            {"code": "RESOURCE_EXHAUSTED"},
            {"code": None},
        ):
            with self.subTest(overrides=overrides):
                error = self.convert(diagnosis(), **overrides)
                self.assertNotIsInstance(error, SchedulingError)
                self.assertIn("native scheduling diagnostics", str(error))
                for key, value in overrides.items():
                    self.assertEqual(getattr(error, key), value)
        for details in (None, [], "insufficient_memory"):
            with self.subTest(details=details):
                error = self.convert(details)
                self.assertNotIsInstance(error, SchedulingError)

    def test_plain_timeouts_and_backend_limits_keep_existing_classification(self):
        for native in (
            TimeoutError("transport timeout"),
            EnvironmentHTTPError(504, {}, "initialization timeout"),
            EnvironmentHTTPError(429, {}, "registry limit", code="RESOURCE_EXHAUSTED"),
        ):
            with self.subTest(native=native):
                error = adx._convert_error("create sandbox", native)
                self.assertNotIsInstance(error, SchedulingError)
                self.assertIn(str(native), str(error))
        error = adx._convert_error("create sandbox", TimeoutError("transport timeout"))
        self.assertIsInstance(error, TimeoutException)

    def test_public_scheduling_error_properties_are_safe_without_diagnostics(self):
        error = SchedulingError("caller-supplied message")
        self.assertEqual(error.reasons, ("unknown",))
        self.assertEqual(error.resource_shortages, ())
        self.assertEqual(str(error), "caller-supplied message")


class SchedulingTransportTest(unittest.TestCase):
    def setUp(self):
        self.config = BackendConfig(
            api_endpoint=Endpoint("api.example", 443, "https", True),
            gateway_endpoint=Endpoint("gateway.example", 8443, "https", True),
            token="test-token",
        )

    def create_error(self, sse, **overrides):
        calls = []
        context = {
            "code": "DEADLINE_EXCEEDED",
            "retry": "never",
            "outcome": "not_started",
            "requestId": "create-test",
            "operationId": "operation-test",
            "environmentId": "default-test",
            "schedulingFailure": diagnosis(),
        }
        context.update(overrides)

        def reject(_client, request, **_kwargs):
            calls.append(request)
            payload = {"message": "native scheduling diagnostics", "error": context}
            if sse:
                payload.update(status="timeout", errorCode=504)
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content="event: final\ndata: " + json.dumps(payload) + "\n\n",
                    request=request,
                )
            return httpx.Response(504, json=payload, request=request)

        # Exercise the native constructor, encoder and JSON/SSE parser;
        # replace only network I/O and real backoff waiting.
        with (
            patch.object(httpx.Client, "send", autospec=True, side_effect=reject),
            patch("adx_environment._transport.time.sleep") as sleep,
            self.assertRaises(BackendOperationError) as caught,
        ):
            adx.AdxBackend(self.config).create(_spec())
        return caught.exception, calls, sleep

    def test_pinned_client_json_and_sse_keep_single_attempt_and_full_context(self):
        for sse in (False, True):
            with self.subTest(sse=sse):
                error, calls, sleep = self.create_error(sse)
                self.assertIsInstance(error, InsufficientResourcesError)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].method, "POST")
                sleep.assert_not_called()
                self.assertIsInstance(error.__cause__, EnvironmentHTTPError)
                self.assertEqual(error.scheduling_failure, diagnosis())
                self.assertEqual(error.status_code, 504)
                self.assertEqual(error.code, "DEADLINE_EXCEEDED")
                self.assertEqual(error.retry, "never")
                self.assertEqual(error.outcome, "not_started")
                self.assertEqual(error.request_id, "create-test")
                self.assertEqual(error.operation_id, "operation-test")
                self.assertEqual(error.environment_id, "default-test")
                self.assertIn("native scheduling diagnostics", str(error.__cause__))

    def test_uncertain_json_and_sse_keep_native_retries_and_request_identity(self):
        for sse in (False, True):
            with self.subTest(sse=sse):
                error, calls, sleep = self.create_error(
                    sse, retry="same_operation", outcome="unknown"
                )
                self.assertNotIsInstance(error, SchedulingError)
                self.assertEqual(error.retry, "same_operation")
                self.assertEqual(error.outcome, "unknown")
                self.assertEqual(error.scheduling_failure, diagnosis())
                self.assertEqual(len(calls), 3)
                self.assertEqual(sleep.call_count, 2)
                for request in calls:
                    self.assertEqual(request.method, "POST")
                    self.assertEqual(request.content, calls[0].content)
                    self.assertEqual(
                        request.headers["x-request-id"],
                        calls[0].headers["x-request-id"],
                    )

    def test_terminal_initialization_and_older_services_keep_native_errors(self):
        for sse in (False, True):
            for overrides in ({"outcome": "terminal"}, {"schedulingFailure": None}):
                with self.subTest(sse=sse, overrides=overrides):
                    error, calls, sleep = self.create_error(sse, **overrides)
                    self.assertNotIsInstance(error, SchedulingError)
                    self.assertEqual(len(calls), 1)
                    sleep.assert_not_called()
                    self.assertIn("native scheduling diagnostics", str(error))

    def test_wrapping_retains_secondary_context_and_scheduling_class(self):
        native = native_error(diagnosis())
        native.cleanup_errors = (RuntimeError("cleanup failed"),)
        native.first_error = RuntimeError("initial attempt")
        native.failure = {"reason": "failure"}
        native.probe_failure = {"kind": "probe"}
        native.request_forwarded = False
        native.command_id = "command-test"
        native.command_result = SimpleNamespace(stdout="output")
        with (
            patch.object(adx, "_OwnedSandbox", side_effect=native),
            self.assertRaises(InsufficientResourcesError) as caught,
        ):
            adx.AdxBackend(self.config).create(_spec())
        public = caught.exception
        self.assertIs(public.__cause__, native)
        wrapped = adx._convert_error("restore snapshot", public)
        self.assertIsInstance(wrapped, InsufficientResourcesError)
        self.assertTrue(str(wrapped).startswith("restore snapshot failed:"))
        for key in (
            "code",
            "retry",
            "outcome",
            "request_id",
            "operation_id",
            "environment_id",
            "status_code",
            "scheduling_failure",
            "cleanup_errors",
            "first_error",
            "failure",
            "probe_failure",
            "request_forwarded",
            "command_id",
            "command_result",
        ):
            self.assertEqual(getattr(wrapped, key), getattr(native, key, None), key)


if __name__ == "__main__":
    unittest.main()
