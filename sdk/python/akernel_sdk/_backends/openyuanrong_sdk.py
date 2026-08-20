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

"""Adapter for the actor-based ``openyuanrong-sdk`` backend."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..types import CommandInfo, CommandResult, EntryInfo, SandboxInfo
from . import openyuanrong_sdk_impl as _impl
from .base import (
    Backend,
    BackendConfig,
    BackendSession,
    Capability,
    SandboxSpec,
)
from .errors import BackendOperationError
from .openyuanrong_sdk_commands import (
    CommandHandle as NativeCommandHandle,
)
from .openyuanrong_sdk_commands import Commands as NativeCommands
from .openyuanrong_sdk_filesystem import Filesystem as NativeFilesystem

_NAMESPACE = "akernel"
logger = logging.getLogger(__name__)


def _convert_error(operation: str, error: Exception) -> BackendOperationError:
    return BackendOperationError(f"{operation} failed: {error}")


def _rollback_instance(instance: Any, operation: str) -> None:
    try:
        _impl.terminate_instance(instance)
    except Exception:
        logger.warning(
            "failed to roll back an actor after %s failed",
            operation,
            exc_info=True,
        )


class _CommandsDriver:
    def __init__(self, instance: Any) -> None:
        self._commands = NativeCommands(instance)
        self._handles: dict[int, NativeCommandHandle] = {}

    def run(
        self,
        cmd: str,
        *,
        envs: Mapping[str, str] | None,
        cwd: str | None,
        timeout: int,
    ) -> CommandResult:
        try:
            result = self._commands.run(
                cmd,
                envs=dict(envs) if envs is not None else None,
                cwd=cwd,
                timeout=timeout,
            )
        except Exception as error:
            raise _convert_error("command execution", error) from error
        assert isinstance(result, CommandResult)
        return result

    def start(
        self,
        cmd: str,
        *,
        envs: Mapping[str, str] | None,
        cwd: str | None,
        stdin: bool,
    ) -> int:
        try:
            handle = self._commands.run(
                cmd,
                background=True,
                envs=dict(envs) if envs is not None else None,
                cwd=cwd,
                stdin=stdin,
            )
        except Exception as error:
            raise _convert_error("background command start", error) from error
        assert isinstance(handle, NativeCommandHandle)
        self._handles[handle.pid] = handle
        return handle.pid

    def wait(self, pid: int, timeout: int | None) -> CommandResult:
        handle = self._handles.get(pid)
        if handle is None:
            raise BackendOperationError(f"no command handle for pid {pid}")
        try:
            return handle.wait(timeout)
        except Exception as error:
            raise _convert_error(f"wait for process {pid}", error) from error

    def kill(self, pid: int) -> bool:
        try:
            return self._commands.kill(pid)
        except Exception as error:
            raise _convert_error(f"kill process {pid}", error) from error

    def send_stdin(self, pid: int, data: str, eof: bool) -> None:
        try:
            self._commands.send_stdin(pid, data, eof)
        except Exception as error:
            raise _convert_error(f"send stdin to process {pid}", error) from error

    def list(self) -> list[CommandInfo]:
        try:
            return self._commands.list()
        except Exception as error:
            raise _convert_error("list processes", error) from error


class _FilesystemDriver:
    def __init__(self, instance: Any, sandbox_id: str) -> None:
        self._files = NativeFilesystem(instance, instance_id=sandbox_id)

    def read(self, path: str, *, binary: bool) -> str | bytes:
        try:
            return self._files.read(path, format="bytes" if binary else "text")
        except Exception as error:
            raise _convert_error(f"read {path}", error) from error

    def write(self, path: str, data: str | bytes) -> EntryInfo:
        try:
            return self._files.write(path, data)
        except Exception as error:
            raise _convert_error(f"write {path}", error) from error

    def list(self, path: str, depth: int) -> list[EntryInfo]:
        try:
            return self._files.list(path, depth)
        except Exception as error:
            raise _convert_error(f"list {path}", error) from error

    def exists(self, path: str) -> bool:
        try:
            return self._files.exists(path)
        except Exception as error:
            raise _convert_error(f"check {path}", error) from error

    def remove(self, path: str) -> None:
        try:
            self._files.remove(path)
        except Exception as error:
            raise _convert_error(f"remove {path}", error) from error

    def rename(self, old_path: str, new_path: str) -> EntryInfo:
        try:
            return self._files.rename(old_path, new_path)
        except Exception as error:
            raise _convert_error(f"rename {old_path}", error) from error

    def make_dir(self, path: str) -> bool:
        try:
            return self._files.make_dir(path)
        except Exception as error:
            raise _convert_error(f"create directory {path}", error) from error

    def get_info(self, path: str) -> EntryInfo:
        try:
            return self._files.get_info(path)
        except Exception as error:
            raise _convert_error(f"get info for {path}", error) from error

    def copy_from_local(self, local_path: str, remote_path: str) -> None:
        try:
            self._files.copy_from_local(local_path, remote_path)
        except FileNotFoundError:
            raise
        except Exception as error:
            raise _convert_error(
                f"copy {local_path} to {remote_path}", error
            ) from error

    def copy_to_local(self, remote_path: str, local_path: str) -> None:
        try:
            self._files.copy_to_local(remote_path, local_path)
        except Exception as error:
            raise _convert_error(
                f"copy {remote_path} to {local_path}", error
            ) from error


class _Session:
    def __init__(
        self,
        instance: Any,
        sandbox_id: str,
        spec: SandboxSpec,
        tunnel_client: Any,
    ) -> None:
        self.id = sandbox_id
        self.commands = _CommandsDriver(instance)
        self.files = _FilesystemDriver(instance, sandbox_id)
        self._instance = instance
        self._spec = spec
        self._tunnel_client = tunnel_client
        self._terminated = False
        self._closed = False

    def is_running(self) -> bool:
        if self._terminated:
            return False
        return _impl.ping_instance(self._instance)

    def get_info(self) -> SandboxInfo:
        try:
            state = _impl.instance_state(self._instance)
        except Exception as error:
            raise _convert_error("get sandbox info", error) from error
        return SandboxInfo(
            id=self.id,
            state=state,
            cpu=self._spec.cpu,
            memory=self._spec.memory,
            image=self._spec.image,
            xpu=self._spec.xpu,
            storage_mb=self._spec.storage_mb,
        )

    def terminate(self) -> None:
        if self._terminated:
            return
        try:
            _impl.terminate_instance(self._instance)
        except Exception as error:
            raise _convert_error("terminate sandbox", error) from error
        self._terminated = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._tunnel_client is not None:
            try:
                self._tunnel_client.stop()
            except Exception as error:
                raise _convert_error("close reverse tunnel", error) from error
            finally:
                self._tunnel_client = None


class OpenYuanRongSdkBackend:
    """Actor-based backend implemented by ``openyuanrong-sdk``."""

    name = "openyuanrong-sdk"
    namespace = _NAMESPACE
    capabilities = frozenset(Capability)

    def __init__(self, _config: BackendConfig) -> None:
        _impl.ensure_initialized()

    def create(self, spec: SandboxSpec) -> BackendSession:
        options = _impl.build_options(
            image=spec.image,
            rootfs=spec.rootfs,
            runtime=spec.runtime,
            cpu=spec.cpu,
            memory=spec.memory,
            cpu_limit=spec.cpu_limit,
            mem_limit=spec.mem_limit,
            idle_timeout=spec.idle_timeout,
            schedule_timeout=spec.schedule_timeout,
            env=spec.env,
            name=spec.name,
            port_forwardings=spec.port_forwardings,
            mounts=spec.mounts,
            reverse_tunnel=spec.reverse_tunnel,
            detached=spec.detached,
            node_id=spec.node_id,
            xpu=spec.xpu,
            storage_mb=spec.storage_mb,
            network_policy=spec.network_policy,
            extra_config=spec.extra_config,
        )
        try:
            instance = _impl.create_instance(
                options,
                cwd=spec.command_cwd,
            )
        except Exception as error:
            raise _convert_error("create sandbox", error) from error

        try:
            sandbox_id = _impl.real_instance_id(instance)
        except Exception as error:
            _rollback_instance(instance, "physical ID resolution")
            raise _convert_error("create sandbox", error) from error

        tunnel_client = None
        try:
            if spec.reverse_tunnel is not None:
                tunnel_client = _impl.start_reverse_tunnel(
                    instance,
                    spec.reverse_tunnel,
                    name=spec.name,
                )
        except Exception as error:
            _rollback_instance(instance, "reverse tunnel startup")
            raise _convert_error("start reverse tunnel", error) from error
        try:
            return _Session(instance, sandbox_id, spec, tunnel_client)
        except Exception as error:
            if tunnel_client is not None:
                try:
                    tunnel_client.stop()
                except Exception:
                    logger.warning(
                        "failed to close a reverse tunnel after session "
                        "initialization failed",
                        exc_info=True,
                    )
            _rollback_instance(instance, "session initialization")
            raise _convert_error("initialize sandbox session", error) from error

    def delete_named(self, name: str) -> None:
        try:
            _impl.delete_named_instance(name)
        except Exception as error:
            raise _convert_error(f"delete sandbox {name!r}", error) from error

    def close(self) -> None:
        _impl.finalize()


def create_backend(config: BackendConfig) -> Backend:
    """Construct the actor backend for the lazy registry."""

    return OpenYuanRongSdkBackend(config)
