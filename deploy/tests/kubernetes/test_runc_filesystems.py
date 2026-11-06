"""Runc acceptance must distinguish scheduling refusal from later failures."""
import copy
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("runc_filesystems", Path(__file__).with_name("runc_filesystems.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class BackendError(RuntimeError):
    def __init__(self, code="DEADLINE_EXCEEDED", status_code=504, scheduling_failure=None,
                 cleanup_errors=()):
        super().__init__("test backend failure")
        self.code, self.status_code = code, status_code
        self.scheduling_failure = scheduling_failure
        self.cleanup_errors = cleanup_errors


EMPTY_SCHEDULING = {
    "eligibleNodes": 0, "nodeCount": 0, "rejections": {"no_workers": 1},
    "maxCapacity": {"cpu_millis": 0, "memory_bytes": 0, "disk_bytes": 0},
    "maxAvailable": {"cpu_millis": 0, "memory_bytes": 0, "disk_bytes": 0},
}


class Pages:
    def __init__(self, pages):
        self.pages = list(pages)
        self.index = 0

    @property
    def has_next(self):
        return self.index < len(self.pages)

    @property
    def next_token(self):
        return None if self.index == 0 else str(self.index)

    def next_items(self):
        page = self.pages[self.index]
        self.index += 1
        if isinstance(page, Exception):
            raise page
        return page


class ProbeTest(unittest.TestCase):
    def sdk(self, *, creation=None, nodes=()):
        factory = Mock(side_effect=creation)
        factory.list.side_effect = lambda **kwargs: Pages([[]])
        return SimpleNamespace(Sandbox=factory, resources=Mock(return_value=list(nodes)),
                               BackendOperationError=BackendError)

    def negative(self, error=None, sdk=None):
        error = error or BackendError(scheduling_failure=copy.deepcopy(EMPTY_SCHEDULING))
        return probe.Probe("negative", "worker-a", sdk=sdk or self.sdk(creation=error))

    def sandbox(self, output="runc-ready", *, command_error=None, cleanup_error=None):
        sandbox = Mock(id="owned-instance")
        sandbox.__enter__ = Mock(return_value=sandbox)
        sandbox.__exit__ = Mock(return_value=False, side_effect=cleanup_error)
        sandbox.commands.run.side_effect = command_error
        sandbox.commands.run.return_value = SimpleNamespace(exit_code=0, stdout=output)
        return sandbox

    def test_structured_scheduling_rejection_passes_and_pins_target(self):
        acceptance = self.negative()
        report = acceptance.run()
        self.assertTrue(report["passed"])
        self.assertEqual(report["phase"], "complete")
        self.assertEqual(report["expected_create_failure"]["scheduling_failure"], EMPTY_SCHEDULING)
        acceptance.sdk.Sandbox.assert_called_once_with(
            runtime="runc", node_id="worker-a", cpu=1000, memory=256, schedule_timeout=8)
        self.assertEqual(acceptance.sdk.resources.call_count, 2)

    def test_transport_authentication_and_unstructured_errors_fail(self):
        for code, status, failure in (
            ("UNAVAILABLE", 503, EMPTY_SCHEDULING),
            ("UNAUTHENTICATED", 401, EMPTY_SCHEDULING),
            ("PERMISSION_DENIED", 403, EMPTY_SCHEDULING),
            ("DEADLINE_EXCEEDED", 504, None),
        ):
            with self.subTest(code=code, failure=failure):
                acceptance = self.negative(BackendError(code, status, failure))
                with self.assertRaises(AssertionError):
                    acceptance.run()
                self.assertFalse(acceptance.report["passed"])
        with self.assertRaisesRegex(RuntimeError, "raw transport"):
            self.negative(sdk=self.sdk(creation=RuntimeError("raw transport"))).run()

    def test_missing_nonzero_or_malformed_scheduling_fields_fail(self):
        variants = []
        for key in EMPTY_SCHEDULING:
            value = copy.deepcopy(EMPTY_SCHEDULING)
            del value[key]
            variants.append(value)
        for path, replacement in (
            (("eligibleNodes",), 1), (("nodeCount",), 1), (("eligibleNodes",), False),
            (("rejections", "no_workers"), 0), (("maxCapacity", "cpu_millis"), 1000),
            (("maxAvailable", "memory_bytes"), 256), (("maxAvailable", "disk_bytes"), None),
        ):
            value = copy.deepcopy(EMPTY_SCHEDULING)
            parent = value
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = replacement
            variants.append(value)
        for failure in variants:
            with self.subTest(failure=failure):
                acceptance = self.negative(BackendError(scheduling_failure=failure))
                with self.assertRaises(AssertionError):
                    acceptance.run()
                self.assertFalse(acceptance.report["passed"])

    def test_creation_rollback_error_does_not_pass(self):
        acceptance = self.negative(BackendError(scheduling_failure=EMPTY_SCHEDULING,
                                               cleanup_errors=(RuntimeError("rollback"),)))
        with self.assertRaisesRegex(AssertionError, "rollback"):
            acceptance.run()

    def test_successful_negative_creation_is_cleaned_and_never_runs_commands(self):
        sandbox = self.sandbox(command_error=BackendError(), cleanup_error=BackendError())
        sdk = self.sdk()
        sdk.Sandbox.return_value = sandbox
        acceptance = self.negative(sdk=sdk)
        with self.assertRaisesRegex(AssertionError, "creation succeeded"):
            acceptance.run()
        sandbox.kill.assert_called_once_with()
        sandbox.commands.run.assert_not_called()
        sandbox.__enter__.assert_not_called()
        self.assertNotIn("expected_create_failure", acceptance.report)
        self.assertFalse(acceptance.report["passed"])

    def test_unexpected_create_cleanup_error_propagates(self):
        sandbox = self.sandbox()
        sandbox.kill.side_effect = BackendError("UNAVAILABLE", 503)
        sdk = self.sdk()
        sdk.Sandbox.return_value = sandbox
        acceptance = self.negative(sdk=sdk)
        with self.assertRaises(BackendError):
            acceptance.run()
        self.assertEqual(acceptance.report["phase"], "unexpected_create_cleanup")
        self.assertFalse(acceptance.report["passed"])

    def test_negative_resource_view_must_be_empty_before_and_after(self):
        node = SimpleNamespace(id="worker-a", status=0, capacity={}, allocatable={})
        for resources in (([node],), ([], [node])):
            with self.subTest(resources=resources):
                acceptance = self.negative()
                acceptance.sdk.resources.side_effect = resources
                with self.assertRaisesRegex(AssertionError, "empty resource"):
                    acceptance.run()
                self.assertFalse(acceptance.report["passed"])

    def test_inventory_fetches_empty_pages_and_rejects_later_paused_instance(self):
        sdk = self.sdk()
        pages = Pages([[], [], [SimpleNamespace(id="paused-id", state="paused")]])
        sdk.Sandbox.list.return_value = pages
        sdk.Sandbox.list.side_effect = None
        acceptance = probe.Probe("inventory", "worker-a", sdk=sdk)
        with self.assertRaisesRegex(AssertionError, "running or paused"):
            acceptance.run()
        self.assertEqual(pages.index, 3)
        self.assertEqual(acceptance.report["inventory_before"]["instances"][0]["id"], "paused-id")

    def test_inventory_page_error_and_stuck_token_never_mean_empty(self):
        for pages in (Pages([[], RuntimeError("page failed")]), Pages([[], []])):
            if len(pages.pages) == 2 and not isinstance(pages.pages[1], Exception):
                type_for_stuck = type("StuckPages", (Pages,), {"next_token": property(lambda self: None)})
                pages = type_for_stuck([[], []])
            sdk = self.sdk()
            sdk.Sandbox.list.side_effect = None
            sdk.Sandbox.list.return_value = pages
            acceptance = probe.Probe("inventory", "worker-a", sdk=sdk)
            with self.assertRaises((RuntimeError, AssertionError)):
                acceptance.run()
            self.assertFalse(acceptance.report["passed"])

    def test_positive_runs_two_targeted_sandboxes_and_checks_cleanup(self):
        node = SimpleNamespace(id="worker-a", status=0, capacity={}, allocatable={})
        first, second = self.sandbox(), self.sandbox("private-write-isolated")
        sdk = self.sdk(creation=[first, second], nodes=[node])
        report = probe.Probe("positive", "worker-a", sdk=sdk).run()
        self.assertTrue(report["passed"])
        self.assertEqual(sdk.Sandbox.call_count, 2)
        for call in sdk.Sandbox.call_args_list:
            self.assertEqual(call.kwargs["node_id"], "worker-a")
            self.assertEqual(call.kwargs["runtime"], "runc")
        first.__exit__.assert_called_once()
        second.__exit__.assert_called_once()

    def test_positive_command_and_context_cleanup_errors_fail(self):
        node = SimpleNamespace(id="worker-a", status=0, capacity={}, allocatable={})
        for failure in ("command", "cleanup"):
            with self.subTest(failure=failure):
                sandbox = self.sandbox(**{failure + "_error": BackendError("UNAVAILABLE", 503)})
                acceptance = probe.Probe("positive", "worker-a",
                                         sdk=self.sdk(creation=[sandbox], nodes=[node]))
                with self.assertRaises(BackendError):
                    acceptance.run()
                self.assertFalse(acceptance.report["passed"])
                self.assertNotIn("expected_create_failure", acceptance.report)

    def test_cli_records_failure_and_redacts_escaped_credentials(self):
        secret = 'test-secret"with\\escapes\nand-newline'
        failure = copy.deepcopy(EMPTY_SCHEDULING)
        failure["detail"] = secret
        # The structured rejection is correct, but rollback failed. CLI must
        # retain the failed phase and exit nonzero without printing credentials.
        sdk = self.sdk(creation=BackendError(scheduling_failure=failure,
                                            cleanup_errors=(RuntimeError("cleanup"),)))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            arguments = ["probe", "--mode", "negative", "--node-id", "worker-a",
                         "--output", str(output)]
            captured = io.StringIO()
            with patch.object(probe, "load_sdk", return_value=sdk), \
                    patch("sys.argv", arguments), patch.dict(os.environ, AKERNEL_TOKEN=secret), \
                    contextlib.redirect_stdout(captured):
                status = probe.main()
            report = json.loads(output.read_text())
            self.assertEqual(status, 1)
            self.assertFalse(report["passed"])
            self.assertEqual(report["phase"], "negative_create")
            self.assertEqual(report["expected_create_failure"]["scheduling_failure"]["detail"], "<redacted>")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(json.dumps(secret)[1:-1], captured.getvalue())


if __name__ == "__main__":
    unittest.main()
