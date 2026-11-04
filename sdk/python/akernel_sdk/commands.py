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

"""Backend-neutral command facade."""

from __future__ import annotations

from typing import Literal, overload

from ._backends.base import CommandReference, CommandsDriver
from ._backends.errors import CommandExitException, TimeoutException
from .types import CommandInfo, CommandResult


class CommandHandle:
    """Handle for a background process running in the sandbox."""

    def __init__(self, pid: int | CommandReference, driver: CommandsDriver) -> None:
        if isinstance(pid, CommandReference):
            self.pid = pid.pid
            self._command_id = pid.command_id
        else:
            # Preserve explicit construction with an older PID-only driver.
            self.pid = pid
            identity = driver.command_id(pid)
            self._command_id = identity if isinstance(identity, str) else None
        self._command: str | int = self._command_id or self.pid
        self._driver = driver

    @property
    def command_id(self) -> str | None:
        """Stable command ID fixed at attachment, independent of later PID reuse."""
        return self._command_id

    def wait(
        self, timeout: int | None = None, *, check: bool = False
    ) -> CommandResult:
        """Wait for completion; check=True raises on nonzero or unfinished exit.

        The default preserves CommandResult behavior. An unfinished wait only
        ends local observation; the remote process may continue running.
        """
        _validate_check(check)
        return _checked_result(self._driver.wait(self._command, timeout), check)

    def kill(self) -> bool:
        """Return whether a live process was signalled."""
        return self._driver.kill(self._command)

    def send_stdin(self, data: str, eof: bool = False) -> None:
        """Write text to the process's stdin."""
        self._driver.send_stdin(self._command, data, eof)

    def close_stdin(self) -> None:
        """Close the process's stdin so it observes EOF."""
        self.send_stdin("", eof=True)


class Commands:
    """Client-side wrapper for command execution on the remote sandbox."""

    def __init__(self, driver: CommandsDriver) -> None:
        self._driver = driver

    @overload
    def run(
        self,
        cmd: str,
        background: Literal[False] = False,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: int = 60,
        stdin: Literal[False] = False,
        *,
        check: bool = False,
    ) -> CommandResult: ...

    @overload
    def run(
        self,
        cmd: str,
        background: Literal[True],
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: int = 60,
        stdin: bool = False,
        *,
        check: bool = False,
    ) -> CommandHandle: ...

    def run(
        self,
        cmd: str,
        background: bool = False,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: int = 60,
        stdin: bool = False,
        *,
        check: bool = False,
    ) -> CommandResult | CommandHandle:
        """Execute a foreground command or start a background process.

        An explicit cwd must already exist. Otherwise the sandbox's configured
        command directory is used. check=True opts into typed exit errors;
        background handles apply this option when wait(check=True) is called.
        """
        _validate_check(check)
        if stdin and not background:
            raise ValueError("stdin=True requires background=True")
        if background:
            return CommandHandle(
                self._driver.start(cmd, envs=envs, cwd=cwd, stdin=stdin),
                self._driver,
            )
        return _checked_result(
            self._driver.run(cmd, envs=envs, cwd=cwd, timeout=timeout), check
        )

    def connect(self, command: str | int) -> CommandHandle:
        """Reconnect by stable command ID or PID without submitting it again."""
        if isinstance(command, bool) or not isinstance(command, (str, int)):
            raise TypeError("command must be a stable ID or positive PID")
        if (isinstance(command, str) and not command.strip()) or (
            isinstance(command, int) and command <= 0
        ):
            raise ValueError("command must be a non-empty ID or positive PID")
        return CommandHandle(self._driver.connect(command), self._driver)

    def list(self) -> list[CommandInfo]:
        """Return snapshots of processes tracked by this sandbox."""
        return self._driver.list()

    def kill(self, pid: int) -> bool:
        """Return whether a live process was signalled."""
        return self._driver.kill(pid)

    def send_stdin(self, pid: int, data: str, eof: bool = False) -> None:
        """Write text to the stdin of a tracked process."""
        self._driver.send_stdin(pid, data, eof)

    def close_stdin(self, pid: int) -> None:
        """Close the stdin of a tracked process."""
        self.send_stdin(pid, "", eof=True)


def _validate_check(check: bool) -> None:
    if not isinstance(check, bool):
        raise TypeError("check must be a boolean")


def _checked_result(result: CommandResult, check: bool) -> CommandResult:
    if check:
        if result.exit_code is None:
            raise TimeoutException(
                result.error_message
                or "command wait timed out; the command may still run",
                code=result.error_code or "WAIT_TIMEOUT",
            )
        if result.exit_code != 0:
            raise CommandExitException(result.stdout, result.stderr, result.exit_code)
    return result
