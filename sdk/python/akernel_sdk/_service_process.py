"""Recoverable identity of an actual process inside a Compose service."""

from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ._backends.errors import BackendOperationError, TimeoutException

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ._compose import ServiceHandle


class ServiceProcessHandle:
    """A service process, independent of its short-lived docker-exec submission.

    The in-service monitor and identity record are included in whole-sandbox
    snapshots. attach() never relaunches a command. Container identity/start time
    and Linux process start time are checked before signalling. Python 3 is
    required inside this service; ordinary ServiceHandle.exec needs no Python.
    """

    def __init__(self, service: ServiceHandle, descriptor: Mapping[str, Any]) -> None:
        self.service = service
        self._descriptor = json.loads(json.dumps(descriptor, allow_nan=False))
        if (
            self._descriptor.get("schema_version") != 1
            or re.fullmatch(r"[0-9a-f]{32}", self._descriptor.get("process_id", ""))
            is None
            or self._descriptor.get("service") != service.name
            or self._descriptor.get("project_name")
            != service.project.descriptor["project_name"]
        ):
            raise ValueError("Invalid service process descriptor")

    @property
    def descriptor(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._descriptor))

    @property
    def process_id(self) -> str:
        return str(self._descriptor["process_id"])

    @property
    def pid(self) -> int:
        return int(self._descriptor["pid"])

    def _verify_container(self) -> None:
        identity = self.service.identity()
        if any(
            identity[k] != self._descriptor[k] for k in ("container_id", "started_at")
        ):
            raise BackendOperationError(
                "Service container was replaced or restarted",
                code="SERVICE_PROCESS_STALE",
                outcome="not_started",
            )

    def _invoke(
        self,
        operation: str,
        argument: str,
        *,
        user: str | int | None = "root",
        cwd: str | None = None,
        envs: Mapping[str, str] | None = None,
    ) -> Any:
        script = Path(__file__).with_name("_service_process_guest.py").read_text()
        command = (
            "for executable in python3 python /usr/local/bin/python3 "
            "/usr/bin/python3; do "
            'process_python=$(command -v "$executable") || continue; '
            '"$process_python" -c "import sys;sys.exit(sys.version_info[0]!=3)" '
            "> /dev/null 2>&1 || continue; "
            'exec "$process_python" -c '
            + shlex.quote(script)
            + " "
            + shlex.join([operation, self.process_id, argument])
            + '; done; echo "Service process handles need Python 3" >&2; exit 127'
        )
        result = self.service.exec(command, cwd=cwd, envs=envs, user=user, timeout=30)
        if result.exit_code != 0:
            failure = BackendOperationError(
                "Service process operation failed: " + result.stderr[-500:],
                code="SERVICE_PROCESS_CONFIGURATION"
                if result.exit_code == 127
                else "SERVICE_PROCESS_OBSERVATION",
                outcome="unknown" if operation == "start" else None,
            )
            failure.process_descriptor = self.descriptor  # type: ignore[attr-defined]
            raise failure
        return json.loads(result.stdout)

    @classmethod
    def start(
        cls,
        service: ServiceHandle,
        command: str,
        *,
        cwd: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | int | None = None,
        shell: str = "/bin/bash",
    ) -> ServiceProcessHandle:
        if (
            not isinstance(command, str)
            or not command.strip()
            or not shell.startswith("/")
        ):
            raise ValueError("Process command and absolute shell are required")
        identity = service.identity()
        descriptor = {
            "schema_version": 1,
            "process_id": uuid4().hex,
            "project_name": service.project.descriptor["project_name"],
            "service": service.name,
            **identity,
        }
        result = cls(service, descriptor)
        try:
            row = result._invoke(
                "start",
                json.dumps({"command": command, "shell": shell}),
                cwd=cwd,
                envs=envs,
                user=user,
            )
        except BaseException as failure:
            # The remote process may already exist. Retain its recovery token;
            # never repeat a side-effecting start after an unknown response.
            failure.process_descriptor = result.descriptor  # type: ignore[attr-defined]
            raise
        result._descriptor.update(pid=row["pid"], start_time=row["start_time"])
        return result

    @classmethod
    def attach(
        cls, service: ServiceHandle, descriptor: Mapping[str, Any]
    ) -> ServiceProcessHandle:
        result = cls(service, descriptor)
        result._verify_container()
        status = result._invoke("status", "")
        root = status["identity"]
        if "pid" in result._descriptor and (
            root["pid"] != result.pid
            or root["start_time"] != result._descriptor["start_time"]
        ):
            raise BackendOperationError(
                "Service process identity changed", code="SERVICE_PROCESS_STALE"
            )
        result._descriptor.update(pid=root["pid"], start_time=root["start_time"])
        return result

    def inspect(self) -> list[dict[str, Any]]:
        return self.signal("inspect")

    def signal(self, action: str) -> list[dict[str, Any]]:
        if action not in {"inspect", "stop", "continue", "terminate", "kill"}:
            raise ValueError("Unsupported service process action")
        self._verify_container()
        return self._invoke(
            "signal",
            json.dumps(
                {
                    "action": action,
                    "pid": self.pid,
                    "start_time": self._descriptor["start_time"],
                }
            ),
        )

    def wait(self, *, timeout: float = 60) -> int:
        deadline = time.monotonic() + timeout
        while True:
            self._verify_container()
            status = self._invoke("status", "")
            if status["exit_code"] is not None:
                return int(status["exit_code"])
            if time.monotonic() >= deadline:
                raise TimeoutException(
                    "Service process wait timed out", outcome="running"
                )
            time.sleep(0.1)
