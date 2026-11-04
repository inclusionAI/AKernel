"""Checked-command errors and reconnect never replay a command submission."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from adx_environment.commands import CommandNotFound

from akernel_sdk import CommandExitException, NotFoundException, TimeoutException
from akernel_sdk._backends.adx import _CommandsDriver, _convert_error
from akernel_sdk.commands import Commands
from akernel_sdk.types import CommandResult


class CommandCompatibilityTest(unittest.TestCase):



    def test_nonzero_exit_remains_compatible_unless_explicitly_checked(self):
        driver = MagicMock()
        driver.run.return_value = CommandResult("out", "err", 7)
        commands = Commands(driver)
        self.assertEqual(commands.run("false").exit_code, 7)
        with self.assertRaises(CommandExitException) as raised:
            commands.run("false", check=True)
        self.assertEqual(
            (
                raised.exception.stdout,
                raised.exception.stderr,
                raised.exception.exit_code,
            ),
            ("out", "err", 7),
        )

    def test_wait_timeout_is_an_observation_not_a_second_submission(self):
        native = MagicMock()
        native.list.return_value = [SimpleNamespace(pid=42, id="cmd-existing")]
        native.get.return_value.pid = 42
        native.get.return_value.id = "cmd-existing"
        native.get.return_value.wait.return_value = SimpleNamespace(
            stdout="",
            stderr="",
            exit_code=None,
            status="RUNNING",
            error_code="WAIT_TIMEOUT",
            error_message="still running",
        )
        commands = Commands(_CommandsDriver(native))
        handle = commands.connect(42)
        self.assertEqual(handle.command_id, "cmd-existing")
        self.assertIsNone(handle.wait(timeout=1).exit_code)
        with self.assertRaises(TimeoutException):
            handle.wait(timeout=1, check=True)
        native.run.assert_not_called()
        native.get.assert_called_once_with("cmd-existing")

    def test_reconnect_by_stable_id_and_missing_pid(self):
        native = MagicMock()
        native.get.return_value.pid = 55
        native.get.return_value.id = "cmd-stable"
        commands = Commands(_CommandsDriver(native))
        self.assertEqual(commands.connect("cmd-stable").pid, 55)
        native.get.assert_called_once_with("cmd-stable")
        native.list.return_value = []
        with self.assertRaises(NotFoundException):
            commands.connect(99)
        native.run.assert_not_called()

    def test_missing_stable_command_is_a_typed_not_found(self):
        native = MagicMock()
        native.get.side_effect = CommandNotFound("missing", command_id="cmd-gone")
        with self.assertRaises(NotFoundException) as raised:
            Commands(_CommandsDriver(native)).connect("cmd-gone")
        self.assertEqual(raised.exception.command_id, "cmd-gone")
        native.run.assert_not_called()

    def test_error_classification_preserves_service_context(self):
        error = RuntimeError("denied")
        error.status_code = 403
        error.request_id = "req"
        error.outcome = "not_started"
        converted = _convert_error("query", error)
        self.assertEqual(type(converted).__name__, "PermissionDeniedException")
        self.assertEqual(converted.request_id, "req")
        self.assertEqual(converted.outcome, "not_started")


if __name__ == "__main__":
    unittest.main()
