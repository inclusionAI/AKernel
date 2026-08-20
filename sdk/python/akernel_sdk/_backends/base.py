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

"""Backend-neutral contracts used by the public AKernel SDK."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol

from .._addresses import Endpoint
from ..types import (
    CommandInfo,
    CommandResult,
    EntryInfo,
    HttpReverseTunnel,
    Mount,
    NetworkPolicy,
    S3Config,
    SandboxInfo,
)


class Capability(Enum):
    """Features whose availability differs between backends."""

    S3_ROOTFS = auto()
    NODE_PLACEMENT = auto()
    CUSTOM_REVERSE_TUNNEL_PORTS = auto()
    REVERSE_WEBSOCKET = auto()


@dataclass(frozen=True)
class BackendConfig:
    """Normalized connection configuration passed to one backend."""

    api_endpoint: Endpoint
    gateway_endpoint: Endpoint
    token: str = field(repr=False)


@dataclass(frozen=True)
class SandboxSpec:
    """Validated, immutable inputs for creating a sandbox."""

    image: str | None
    rootfs: S3Config | None
    runtime: str
    cpu: int
    memory: int
    cpu_limit: int
    mem_limit: int
    idle_timeout: int
    schedule_timeout: int
    env: Mapping[str, str]
    name: str | None
    command_cwd: str | None
    port_forwardings: tuple[int, ...]
    mounts: tuple[Mount, ...]
    reverse_tunnel: HttpReverseTunnel | None
    detached: bool
    node_id: str | None
    xpu: str | None
    storage_mb: int | None
    network_policy: NetworkPolicy | None
    extra_config: Mapping[str, object]


class CommandsDriver(Protocol):
    """Backend command operations consumed by :class:`Commands`."""

    def run(
        self,
        cmd: str,
        *,
        envs: Mapping[str, str] | None,
        cwd: str | None,
        timeout: int,
    ) -> CommandResult: ...

    def start(
        self,
        cmd: str,
        *,
        envs: Mapping[str, str] | None,
        cwd: str | None,
        stdin: bool,
    ) -> int: ...

    def wait(self, pid: int, timeout: int | None) -> CommandResult: ...

    def kill(self, pid: int) -> bool: ...

    def send_stdin(self, pid: int, data: str, eof: bool) -> None: ...

    def list(self) -> list[CommandInfo]: ...


class FilesystemDriver(Protocol):
    """Backend filesystem operations consumed by :class:`Filesystem`."""

    def read(self, path: str, *, binary: bool) -> str | bytes: ...

    def write(self, path: str, data: str | bytes) -> EntryInfo: ...

    def list(self, path: str, depth: int) -> list[EntryInfo]: ...

    def exists(self, path: str) -> bool: ...

    def remove(self, path: str) -> None: ...

    def rename(self, old_path: str, new_path: str) -> EntryInfo: ...

    def make_dir(self, path: str) -> bool: ...

    def get_info(self, path: str) -> EntryInfo: ...

    def copy_from_local(self, local_path: str, remote_path: str) -> None: ...

    def copy_to_local(self, remote_path: str, local_path: str) -> None: ...


class BackendSession(Protocol):
    """One backend-native remote sandbox hidden behind AKernel types."""

    @property
    def id(self) -> str: ...

    @property
    def commands(self) -> CommandsDriver: ...

    @property
    def files(self) -> FilesystemDriver: ...

    def is_running(self) -> bool: ...

    def get_info(self) -> SandboxInfo: ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


class Backend(Protocol):
    """Process-level backend factory and shared resource owner."""

    name: str
    namespace: str
    capabilities: frozenset[Capability]

    def create(self, spec: SandboxSpec) -> BackendSession: ...

    def delete_named(self, name: str) -> None: ...

    def close(self) -> None: ...
