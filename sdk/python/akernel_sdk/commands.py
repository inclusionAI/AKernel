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

from ._backends.base import CommandsDriver
from .types import CommandInfo, CommandResult


class CommandHandle:
    """Handle for a background process running in the sandbox."""

    def __init__(self, pid: int, driver: CommandsDriver) -> None:
        self.pid = pid
        self._driver = driver

    def wait(self, timeout: int | None = None) -> CommandResult:
        """Wait for completion; the default backend returns a timeout result."""

        return self._driver.wait(self.pid, timeout)

    def kill(self) -> bool:
        """Return whether a live process was signalled."""

        return self._driver.kill(self.pid)

    def send_stdin(self, data: str, eof: bool = False) -> None:
        """Write text to the process's stdin."""

        self._driver.send_stdin(self.pid, data, eof)

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
    ) -> CommandHandle: ...

    def run(
        self,
        cmd: str,
        background: bool = False,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: int = 60,
        stdin: bool = False,
    ) -> CommandResult | CommandHandle:
        """Execute a foreground command or start a background process.

        An explicit ``cwd`` must already exist inside the sandbox. When omitted,
        commands use the default directory set by ``Sandbox(cwd=...)``.
        """

        if stdin and not background:
            raise ValueError("stdin=True requires background=True")
        if background:
            pid = self._driver.start(
                cmd,
                envs=envs,
                cwd=cwd,
                stdin=stdin,
            )
            return CommandHandle(pid, self._driver)
        return self._driver.run(cmd, envs=envs, cwd=cwd, timeout=timeout)

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
