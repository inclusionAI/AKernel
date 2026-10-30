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
from typing import Any, Protocol

from .._addresses import Endpoint
from .._sandboxes import SandboxQuery
from ..types import (
    CommandInfo,
    CommandResult,
    EntryInfo,
    HttpReverseTunnel,
    Mount,
    NetworkPolicy,
    S3Config,
    SandboxInfo,
    SnapshotInfo,
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
    verify_tls: bool = False


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
    failover: bool
    inherit_entrypoint: bool
    node_id: str | None
    xpu: str | None
    storage_mb: int | None
    storage_limit_mb: int
    network_policy: NetworkPolicy | None
    extra_config: Mapping[str, object]
    create_timeout: int | None = None


@dataclass(frozen=True)
class SnapshotRestoreSpec:
    """Restore inputs, deliberately excluding fresh-boot resource defaults."""

    snapshot_id: str
    idle_timeout: int
    schedule_timeout: int
    timeout: int
    name: str | None
    command_cwd: str | None
    port_forwardings: tuple[int, ...]
    mounts: tuple[Mount, ...]
    reverse_tunnel: HttpReverseTunnel | None
    detached: bool
    failover: bool
    node_id: str | None
    network_policy: NetworkPolicy | None
    extra_config: Mapping[str, object] = field(default_factory=dict)
    xpu: str | None = None
    storage_mb: int | None = None
    inherit_entrypoint: bool = False


@dataclass(frozen=True)
class SandboxConnectSpec:
    """Local attachment options; never fresh-boot or change remote resources."""

    environment_id: str
    timeout: int
    command_cwd: str | None
    port_forwardings: tuple[int, ...]
    reverse_tunnel: HttpReverseTunnel | None
    detached: bool
    idle_timeout: int = 300
    schedule_timeout: int = 30
    network_policy: NetworkPolicy | None = None
    mounts: tuple[Mount, ...] = ()
    extra_config: Mapping[str, object] = field(default_factory=dict)
    xpu: str | None = None
    storage_mb: int | None = None
    inherit_entrypoint: bool = False
    failover: bool = False


@dataclass(frozen=True)
class CommandReference:
    """Atomic command attachment; PID alone is not a durable identity."""

    pid: int
    command_id: str | None


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
    ) -> CommandReference: ...

    def wait(self, command: str | int, timeout: int | None) -> CommandResult: ...

    def kill(self, command: str | int) -> bool: ...

    def send_stdin(self, command: str | int, data: str, eof: bool) -> None: ...

    def list(self) -> list[CommandInfo]: ...

    def connect(self, command: str | int) -> CommandReference: ...

    def command_id(self, pid: int) -> str | None: ...




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


class PtyDriver(Protocol):
    """Backend-native PTY factory consumed by :class:`Pty`."""

    def create(
        self,
        command: list[str],
        *,
        rows: int,
        cols: int,
        on_data: Any,
        timeout: float,
    ) -> Any: ...


class BackendSession(Protocol):
    """One backend-native remote sandbox hidden behind AKernel types."""

    @property
    def id(self) -> str: ...

    @property
    def commands(self) -> CommandsDriver: ...

    @property
    def files(self) -> FilesystemDriver: ...

    @property
    def pty(self) -> PtyDriver | None: ...

    def is_running(self) -> bool: ...

    def get_info(self) -> SandboxInfo: ...

    @property
    def fork_snapshot_id(self) -> str | None: ...

    def pause(self, timeout: int) -> bool: ...

    def create_snapshot(self, name: str | None, timeout: int) -> SnapshotInfo: ...

    def fork(
        self,
        count: int,
        idle_timeout: int | None,
        snapshot_timeout: int,
        restore_timeout: int | None,
    ) -> ForkOutcome: ...

    def reload(self) -> bool: ...

    def wait_entrypoint(self) -> int: ...

    @property
    def entrypoint_exit_info(self) -> Mapping[str, object] | None: ...

    def update_network_policy(self, policy: NetworkPolicy | None) -> None: ...

    def rollback_creation(self) -> bool:
        """Release local resources; return True only for confirmed remote deletion.

        Unconfirmed ownership permits local closure only and returns False.
        """
        ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


@dataclass
class ForkOutcome:
    """Known child results and acknowledgement of temporary snapshot deletion."""

    snapshot_id: str
    results: list[BackendSession | Exception]
    cleanup_error: Exception | None = None


class Backend(Protocol):
    """Process-level backend factory and shared resource owner."""

    name: str
    namespace: str
    capabilities: frozenset[Capability]

    def create(self, spec: SandboxSpec) -> BackendSession: ...

    def from_snapshot(self, spec: SnapshotRestoreSpec) -> BackendSession: ...

    def connect(self, spec: SandboxConnectSpec) -> BackendSession: ...

    def get_sandbox(self, sandbox_id: str) -> SandboxInfo: ...

    def delete_sandbox(self, sandbox_id: str) -> bool: ...

    def list_sandboxes(
        self, query: SandboxQuery, next_token: str | None, limit: int
    ) -> tuple[list[SandboxInfo], str | None]: ...

    def get_snapshot(self, snapshot_id: str) -> SnapshotInfo: ...

    def list_snapshots(
        self, name: str | None, next_token: str | None, limit: int | None
    ) -> tuple[list[SnapshotInfo], str | None]: ...

    def delete_snapshot(self, snapshot_id: str) -> None: ...

    def pty_for(self, instance_id: str) -> PtyDriver: ...

    def delete_named(self, name: str) -> None: ...

    def close(self) -> None: ...
