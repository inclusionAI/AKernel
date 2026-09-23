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

"""Interactive pseudo-terminal support for AKernel sandboxes."""

from __future__ import annotations

import os
import shlex
import ssl
import threading
from collections.abc import Callable, Sequence
from typing import Any

from ._addresses import exec_endpoint_from_env
from ._pty_transport import (
    _build_pty_uri,
    _PtyConnection,
    _PtyTransportError,
)


class PtyError(RuntimeError):
    """Raised when a PTY cannot start or terminates at the transport layer."""


def _validate_size(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _normalize_command(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        try:
            arguments = shlex.split(command)
        except ValueError as error:
            raise ValueError(f"invalid PTY command: {error}") from error
    elif isinstance(command, Sequence) and not isinstance(command, (bytes, bytearray)):
        arguments = list(command)
    else:
        raise TypeError("command must be a string or a sequence of strings")
    if not arguments or not all(
        isinstance(argument, str) and argument for argument in arguments
    ):
        raise ValueError("command must contain at least one non-empty argument")
    return arguments


def _ssl_context(endpoint_tls: bool) -> ssl.SSLContext | None:
    if not endpoint_tls:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class PtySession:
    """A connection-scoped interactive process running in a sandbox."""

    def __init__(
        self,
        connection: Any,
        *,
        remove: Callable[[PtySession], None],
    ) -> None:
        self._connection = connection
        self._remove = remove

    @property
    def session_id(self) -> str:
        """Stable server-generated identifier for diagnostics."""

        try:
            return self._connection.session_id
        except Exception as error:
            raise PtyError(str(error)) from error

    @property
    def exit_code(self) -> int | None:
        """Remote exit code after completion, otherwise ``None``."""

        return self._connection.exit_code

    @property
    def done(self) -> bool:
        """Whether the remote process exited or the connection ended."""

        return self._connection.done

    def send_stdin(self, data: bytes) -> None:
        """Write raw bytes to the PTY input stream."""

        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        try:
            self._connection.send_stdin(data)
        except Exception as error:
            raise PtyError(str(error)) from error

    def close_stdin(self) -> None:
        """Signal end-of-input without closing the PTY output stream."""

        try:
            self._connection.close_stdin()
        except Exception as error:
            raise PtyError(str(error)) from error

    def resize(self, *, rows: int, cols: int) -> None:
        """Change the remote terminal size."""

        _validate_size("rows", rows)
        _validate_size("cols", cols)
        try:
            self._connection.resize(rows=rows, cols=cols)
        except Exception as error:
            raise PtyError(str(error)) from error

    def wait(self, timeout: float | None = None) -> int:
        """Block until the remote process exits and return its exit code."""

        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        try:
            return self._connection.wait(timeout)
        except Exception as error:
            if isinstance(error, TimeoutError):
                raise
            raise PtyError(str(error)) from error

    def close(self) -> None:
        """Close the connection and terminate the remote PTY process."""

        try:
            self._connection.close()
        except Exception as error:
            raise PtyError(str(error)) from error
        finally:
            self._remove(self)

    def __enter__(self) -> PtySession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class Pty:
    """Factory for interactive PTY sessions in one sandbox."""

    def __init__(self, instance_id: str, *, driver: Any | None = None) -> None:
        self._instance_id = instance_id
        self._driver = driver
        self._sessions: set[PtySession] = set()
        self._lock = threading.Lock()

    def create(
        self,
        command: str | Sequence[str] = "/bin/bash",
        *,
        rows: int = 24,
        cols: int = 80,
        on_data: Callable[[bytes], None] | None = None,
        timeout: float = 60,
    ) -> PtySession:
        """Start an interactive process and wait for its PTY to become ready.

        Args:
            command: Executable and arguments. Strings are parsed with
                :func:`shlex.split`; pass an argument sequence to preserve
                exact boundaries.
            rows: Initial terminal row count.
            cols: Initial terminal column count.
            on_data: Callback invoked with each raw output chunk. The callback
                runs on the PTY reader thread and must not call session methods.
            timeout: Seconds to establish the WebSocket and start the process.

        Raises:
            TypeError: An argument has an invalid type.
            ValueError: Command, size, or timeout is invalid.
            TimeoutError: The remote PTY does not start before ``timeout``.
            PtyError: The frontend or remote runtime reports an error.
        """

        arguments = _normalize_command(command)
        _validate_size("rows", rows)
        _validate_size("cols", cols)
        if on_data is not None and not callable(on_data):
            raise TypeError("on_data must be callable")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        if self._driver is not None:
            try:
                connection = self._driver.create(
                    arguments,
                    rows=rows,
                    cols=cols,
                    on_data=on_data,
                    timeout=timeout,
                )
            except TimeoutError:
                raise
            except Exception as error:
                raise PtyError(str(error)) from error
            session = PtySession(connection, remove=self._remove)
            with self._lock:
                self._sessions.add(session)
            return session

        token = os.environ.get("AKERNEL_TOKEN", "").strip()
        if not token:
            raise RuntimeError("AKERNEL_TOKEN is not set")
        endpoint = exec_endpoint_from_env()
        uri = _build_pty_uri(
            endpoint,
            instance_id=self._instance_id,
            token=token,
            command=arguments,
            rows=rows,
            cols=cols,
        )

        session_ref: list[PtySession] = []

        def on_done() -> None:
            if session_ref:
                self._remove(session_ref[0])

        connection = _PtyConnection(
            uri,
            ssl_context=_ssl_context(endpoint.use_tls),
            rows=rows,
            cols=cols,
            on_data=on_data,
            on_done=on_done,
        )
        session = PtySession(connection, remove=self._remove)
        session_ref.append(session)
        with self._lock:
            self._sessions.add(session)
        try:
            connection.start(float(timeout))
        except (TimeoutError, _PtyTransportError) as error:
            connection.close()
            self._remove(session)
            if isinstance(error, TimeoutError):
                raise
            raise PtyError(str(error)) from error
        return session

    def _remove(self, session: PtySession) -> None:
        with self._lock:
            self._sessions.discard(session)

    def _close(self) -> None:
        with self._lock:
            sessions = list(self._sessions)
        for session in sessions:
            session.close()
