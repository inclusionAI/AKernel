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

"""Adapter for the frontend/RRT ``openyuanrong-sandbox`` backend."""

from __future__ import annotations

import inspect
import os
from collections.abc import Mapping
from typing import Any

import yr_sandbox

from ..types import (
    CommandInfo,
    CommandResult,
    EntryInfo,
    SandboxInfo,
)
from .base import (
    Backend,
    BackendConfig,
    BackendSession,
    Capability,
    SandboxSpec,
)
from .errors import BackendOperationError, UnsupportedBackendFeatureError

_NAMESPACE = "default"
_DEFAULT_LISTEN_PORT = 8766


def _convert_error(operation: str, error: Exception) -> BackendOperationError:
    return BackendOperationError(f"{operation} failed: {error}")


def _supports_keyword(callable_value: Any, name: str) -> bool:
    """Return whether a native callable accepts one keyword argument."""

    try:
        parameters = inspect.signature(callable_value).parameters.values()
    except (TypeError, ValueError):
        # Extension and dynamically generated callables may not expose a
        # signature. Treat those as current backends and preserve their native
        # error if the keyword is rejected.
        return True
    return any(
        parameter.name == name
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _command_result(value: Any) -> CommandResult:
    return CommandResult(
        stdout=str(value.stdout),
        stderr=str(value.stderr),
        exit_code=int(value.exit_code),
    )


def _command_info(value: Any) -> CommandInfo:
    return CommandInfo(
        pid=int(value.pid),
        command=str(value.command),
        running=bool(value.running),
    )


def _entry_info(value: Any) -> EntryInfo:
    return EntryInfo(
        name=str(value.name),
        path=str(value.path),
        type=str(value.type),
        size=int(value.size),
        permissions=str(value.permissions),
        modified_time=float(value.modified_time),
    )


class _CommandsDriver:
    def __init__(self, commands: Any) -> None:
        self._commands = commands
        self._handles: dict[int, Any] = {}

    def run(
        self,
        cmd: str,
        *,
        envs: Mapping[str, str] | None,
        cwd: str | None,
        timeout: int,
    ) -> CommandResult:
        try:
            value = self._commands.run(
                cmd,
                envs=dict(envs) if envs is not None else None,
                cwd=cwd,
                timeout=timeout,
            )
            return _command_result(value)
        except Exception as error:
            raise _convert_error("command execution", error) from error

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
        pid = int(handle.pid)
        self._handles[pid] = handle
        return pid

    def wait(self, pid: int, timeout: int | None) -> CommandResult:
        handle = self._handles.get(pid)
        if handle is None:
            raise BackendOperationError(f"no command handle for pid {pid}")
        try:
            return _command_result(handle.wait(timeout))
        except Exception as error:
            raise _convert_error(f"wait for process {pid}", error) from error

    def kill(self, pid: int) -> bool:
        try:
            return bool(self._commands.kill(pid))
        except Exception as error:
            raise _convert_error(f"kill process {pid}", error) from error

    def send_stdin(self, pid: int, data: str, eof: bool) -> None:
        try:
            self._commands.send_stdin(pid, data, eof)
        except Exception as error:
            raise _convert_error(f"send stdin to process {pid}", error) from error

    def list(self) -> list[CommandInfo]:
        try:
            return [_command_info(value) for value in self._commands.list()]
        except Exception as error:
            raise _convert_error("list processes", error) from error


class _FilesystemDriver:
    def __init__(self, files: Any) -> None:
        self._files = files

    def read(self, path: str, *, binary: bool) -> str | bytes:
        try:
            return self._files.read(path, format="bytes" if binary else "text")
        except Exception as error:
            raise _convert_error(f"read {path}", error) from error

    def write(self, path: str, data: str | bytes) -> EntryInfo:
        try:
            return _entry_info(self._files.write(path, data))
        except Exception as error:
            raise _convert_error(f"write {path}", error) from error

    def list(self, path: str, depth: int) -> list[EntryInfo]:
        try:
            return [_entry_info(value) for value in self._files.list(path, depth)]
        except Exception as error:
            raise _convert_error(f"list {path}", error) from error

    def exists(self, path: str) -> bool:
        try:
            return bool(self._files.exists(path))
        except Exception as error:
            raise _convert_error(f"check {path}", error) from error

    def remove(self, path: str) -> None:
        try:
            self._files.remove(path)
        except Exception as error:
            raise _convert_error(f"remove {path}", error) from error

    def rename(self, old_path: str, new_path: str) -> EntryInfo:
        try:
            return _entry_info(self._files.rename(old_path, new_path))
        except Exception as error:
            raise _convert_error(f"rename {old_path}", error) from error

    def make_dir(self, path: str) -> bool:
        try:
            return bool(self._files.make_dir(path))
        except Exception as error:
            raise _convert_error(f"create directory {path}", error) from error

    def get_info(self, path: str) -> EntryInfo:
        try:
            return _entry_info(self._files.get_info(path))
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
        sandbox: Any,
        spec: SandboxSpec,
    ) -> None:
        self.id = str(sandbox.id)
        self.commands = _CommandsDriver(sandbox.commands)
        self.files = _FilesystemDriver(sandbox.files)
        self._sandbox = sandbox
        self._spec = spec
        self._terminated = False
        self._closed = False

    def is_running(self) -> bool:
        if self._terminated or self._closed:
            return False
        return bool(self._sandbox.is_running())

    def get_info(self) -> SandboxInfo:
        try:
            value = self._sandbox.get_info()
        except Exception as error:
            raise _convert_error("get sandbox info", error) from error
        return SandboxInfo(
            id=str(value.id),
            state=str(value.state),
            cpu=value.cpu,
            memory=value.memory,
            image=value.image,
            xpu=self._spec.xpu,
            storage_mb=self._spec.storage_mb,
        )

    def reload(self) -> bool:
        if self._terminated or self._closed:
            return False
        reload_sandbox = getattr(self._sandbox, "reload", None)
        if not callable(reload_sandbox):
            raise UnsupportedBackendFeatureError(
                "The installed openyuanrong-sandbox backend does not support "
                "sandbox reload. Upgrade it to a version with failover support."
            )
        try:
            return bool(reload_sandbox())
        except Exception:
            return False

    def terminate(self) -> None:
        if self._terminated:
            return
        # Release the native handle before deleting the remote sandbox. This
        # lets transports such as reverse tunnels close while their routes are
        # still available. The stable-ID delete still uses a fresh native
        # client, so a failed deletion remains retryable after local cleanup.
        self.close()
        try:
            yr_sandbox.Sandbox.delete(self.id)
        except Exception as error:
            raise _convert_error("terminate sandbox", error) from error
        self._terminated = True

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._sandbox.close()
        except Exception as error:
            raise _convert_error("close sandbox resources", error) from error
        finally:
            self._closed = True


class OpenYuanRongSandboxBackend:
    """Backend implemented by ``openyuanrong-sandbox``."""

    name = "openyuanrong-sandbox"
    namespace = _NAMESPACE
    capabilities = frozenset(
        {
            Capability.S3_ROOTFS,
            Capability.NODE_PLACEMENT,
        }
    )

    def __init__(self, config: BackendConfig) -> None:
        os.environ["YR_SERVER_ADDRESS"] = config.api_endpoint.authority()
        os.environ["YR_TLS"] = "1" if config.api_endpoint.use_tls else "0"
        os.environ["YR_GATEWAY_ADDRESS"] = config.gateway_endpoint.authority()
        os.environ["YR_GATEWAY_TLS"] = (
            "1" if config.gateway_endpoint.use_tls else "0"
        )
        os.environ["YR_TOKEN"] = config.token

    def _validate(self, spec: SandboxSpec) -> None:
        tunnel = spec.reverse_tunnel
        if tunnel is not None and tunnel.reverse_port != tunnel.listen_port - 1:
            raise UnsupportedBackendFeatureError(
                "Backend 'openyuanrong-sandbox' requires reverse_port to equal "
                "listen_port - 1."
            )

    def create(self, spec: SandboxSpec) -> BackendSession:
        self._validate(spec)
        supports_failover = _supports_keyword(yr_sandbox.Sandbox, "failover")
        if spec.failover and not supports_failover:
            raise UnsupportedBackendFeatureError(
                "The installed openyuanrong-sandbox backend does not support "
                "automatic sandbox failover. Upgrade it to a version with "
                "failover support."
            )
        rootfs = None
        if spec.rootfs is not None:
            rootfs = yr_sandbox.S3Config(
                endpoint=spec.rootfs.endpoint,
                bucket=spec.rootfs.bucket,
                object=spec.rootfs.object,
                access_key=spec.rootfs.access_key,
                secret_key=spec.rootfs.secret_key,
            )
        network = None
        if spec.network_policy is not None:
            network = yr_sandbox.NetworkPolicy(
                block_network=spec.network_policy.block_network,
                dns_blacklist=spec.network_policy.dns_blacklist,
            )
        mounts = [
            yr_sandbox.Mount(
                target=mount.target,
                image_url=mount.image_url,
                s3_config=(
                    yr_sandbox.S3Config(
                        endpoint=mount.s3_config.endpoint,
                        bucket=mount.s3_config.bucket,
                        object=mount.s3_config.object,
                        access_key=mount.s3_config.access_key,
                        secret_key=mount.s3_config.secret_key,
                    )
                    if mount.s3_config is not None
                    else None
                ),
                type=mount.type,
            )
            for mount in spec.mounts
        ]
        create_timeout = max(60, spec.schedule_timeout + 30)
        create_args = dict(
            image=spec.image,
            rootfs=rootfs,
            runtime=spec.runtime,
            cpu=spec.cpu,
            memory=spec.memory,
            cpu_limit=spec.cpu_limit,
            mem_limit=spec.mem_limit,
            idle_timeout=spec.idle_timeout,
            schedule_timeout=spec.schedule_timeout,
            env=dict(spec.env),
            name=spec.name,
            cwd=spec.command_cwd,
            port_forwardings=list(spec.port_forwardings),
            mounts=mounts,
            upstream=(
                spec.reverse_tunnel.target
                if spec.reverse_tunnel is not None
                else None
            ),
            tunnel_connect_timeout=(
                spec.reverse_tunnel.connect_timeout
                if spec.reverse_tunnel is not None
                else None
            ),
            proxy_port=(
                spec.reverse_tunnel.listen_port
                if spec.reverse_tunnel is not None
                else _DEFAULT_LISTEN_PORT
            ),
            detached=spec.detached,
            node_id=spec.node_id,
            xpu=spec.xpu,
            storage_mb=spec.storage_mb,
            network=network,
            extra_config=dict(spec.extra_config),
            create_timeout=create_timeout,
        )
        if supports_failover:
            create_args["failover"] = spec.failover
        try:
            sandbox = yr_sandbox.Sandbox(**create_args)
        except Exception as error:
            raise _convert_error("create sandbox", error) from error
        return _Session(sandbox, spec)

    def delete_named(self, name: str) -> None:
        sandbox_id = f"{self.namespace}-{name}"
        try:
            yr_sandbox.Sandbox.delete(sandbox_id)
        except Exception as error:
            raise _convert_error(f"delete sandbox {name!r}", error) from error

    def close(self) -> None:
        """The backend has no process-level client to close."""


def create_backend(config: BackendConfig) -> Backend:
    """Construct the ``openyuanrong-sandbox`` backend for the lazy registry."""

    return OpenYuanRongSandboxBackend(config)
