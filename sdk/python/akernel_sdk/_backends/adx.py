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

"""Adapter for the API Server/Execd ``adx-environment`` backend."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import adx_environment

from ..types import (
    CommandInfo,
    CommandResult,
    DNSPolicy,
    DNSRule,
    EntryInfo,
    Mount,
    NetworkPolicy,
    NetworkRule,
    PortRange,
    SandboxInfo,
    SnapshotInfo,
    TrafficPolicy,
)
from .base import (
    Backend,
    BackendConfig,
    BackendSession,
    Capability,
    ForkOutcome,
    SandboxSpec,
    SnapshotRestoreSpec,
)
from .errors import (
    BackendOperationError,
    UnsupportedBackendFeatureError,
    retain_failure_context,
)

_NAMESPACE = "default"
_DEFAULT_LISTEN_PORT = 8766
# Pinned ADX reserves 30s for initialization and 30s for create handoff. Below
# this reserve it either rejects the budget or silently increases it.
_CREATE_RESERVE_SECONDS = 60
logger = logging.getLogger(__name__)


def _validate_restore_timeout(schedule_timeout: int, timeout: int) -> None:
    minimum = schedule_timeout + _CREATE_RESERVE_SECONDS
    if timeout < minimum:
        raise ValueError(
            "restore timeout must be at least schedule_timeout + 60 seconds "
            f"({minimum} seconds for this source)"
        )


@dataclass(frozen=True)
class _NativeHostPathMount(adx_environment.Mount):
    """Adapt node HostPath sources through the pinned native Mount serializer.

    The pinned native SDK checks isinstance(Mount) before calling to_dict, but
    its stock value type only represents Image/S3. Keep that constructor and
    transport intact without replacing native classes or mutating global state.
    """

    host_path: str = ""
    read_only: bool = True

    def __post_init__(self) -> None:
        self._public_mount()

    def _public_mount(self) -> Mount:
        if self.image_url is not None or self.s3_config is not None:
            raise ValueError("HostPath adapter requires only a host_path source")
        return Mount(
            target=self.target,
            type=self.type,
            host_path=self.host_path,
            read_only=self.read_only,
        )

    def to_dict(self) -> dict[str, Any]:
        return self._public_mount().to_dict()


def _native_mount(mount: Mount) -> Any:
    if mount.host_path is not None:
        return _NativeHostPathMount(
            target=mount.target,
            host_path=mount.host_path,
            read_only=mount.read_only,
        )
    return adx_environment.Mount(
        target=mount.target,
        image_url=mount.image_url,
        s3_config=(
            adx_environment.S3Config(
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


def _control_pty_connection(connection: Any) -> Any:
    """PTY direct routing uses the control TLS entrypoint, not the data port."""

    return adx_environment.ConnectionConfig(
        server_address=connection.server_address,
        token=connection.token,
        use_tls=connection.use_tls,
        verify_tls=connection.verify_tls,
        token_provider=connection.token_provider,
    )


def _native_port_range(value: PortRange | int | None) -> Any:
    if value is None:
        return None
    assert isinstance(value, PortRange)
    return adx_environment.PortRange(first=value.first, last=value.last)


def _native_network_rule(rule: NetworkRule) -> Any:
    return adx_environment.NetworkRule(
        action=rule.action,
        direction=rule.direction,
        protocol=rule.protocol,
        cidr=rule.cidr,
        domain=rule.domain,
        port_range=_native_port_range(rule.port_range),
        environment_port_range=_native_port_range(rule.sandbox_port_range),
        priority=rule.priority,
    )


def _native_traffic_policy(policy: TrafficPolicy | None) -> Any:
    if policy is None:
        return None
    return adx_environment.TrafficPolicy(
        ingress_default_action=policy.ingress_default_action,
        egress_default_action=policy.egress_default_action,
        rules=tuple(_native_network_rule(rule) for rule in policy.rules),
        mode=policy.mode,
    )


def _native_dns_rule(rule: DNSRule) -> Any:
    return adx_environment.DNSRule(pattern=rule.pattern, action=rule.action)


def _native_dns_policy(policy: DNSPolicy | None) -> Any:
    if policy is None:
        return None
    return adx_environment.DNSPolicy(
        default_action=policy.default_action,
        rules=tuple(_native_dns_rule(rule) for rule in policy.rules),
    )


def _native_network_policy(policy: NetworkPolicy) -> Any:
    return adx_environment.NetworkPolicy(
        block_network=policy.block_network,
        dns_blacklist=policy.dns_blacklist,
        traffic=_native_traffic_policy(policy.traffic),
        dns=_native_dns_policy(policy.dns),
    )


def _convert_error(operation: str, error: Exception) -> BackendOperationError:
    return BackendOperationError(
        f"{operation} failed: {error}",
        code=getattr(error, "code", None),
        retry=getattr(error, "retry", None),
        outcome=getattr(error, "outcome", None),
        request_id=getattr(error, "request_id", None),
        operation_id=getattr(error, "operation_id", None),
        environment_id=getattr(error, "environment_id", None),
        status_code=getattr(error, "status_code", None),
        scheduling_failure=getattr(error, "scheduling_failure", None),
        failure=getattr(error, "failure", None),
        probe_failure=getattr(error, "probe_failure", None),
        request_forwarded=getattr(error, "request_forwarded", None),
        cleanup_errors=getattr(error, "cleanup_errors", ()) or (),
    )


def _route_read_retryable(error: Exception) -> bool:
    cause: BaseException | None = error
    for _ in range(4):
        if cause is None:
            break
        if isinstance(cause, adx_environment.EnvironmentError) and getattr(
            cause, "status_code", None
        ) in (409, 503):
            return getattr(cause, "retry", None) != "never"
        cause = cause.__cause__
    return False


def _command_result(value: Any) -> CommandResult:
    status = getattr(value, "status", None)
    return CommandResult(
        stdout=str(value.stdout),
        stderr=str(value.stderr),
        exit_code=int(value.exit_code) if value.exit_code is not None else None,
        status=getattr(status, "value", status),
        error_code=getattr(value, "error_code", None),
        error_message=getattr(value, "error_message", None),
    )


def _command_timeout_message(value: Any, timeout: int) -> str | None:
    status = getattr(value, "status", None)
    error_code = getattr(value, "error_code", None)
    stderr = str(getattr(value, "stderr", ""))
    if not (
        status == adx_environment.CommandStatus.TIMED_OUT
        or error_code in ("COMMAND_TIMEOUT", "WAIT_TIMEOUT")
        or (
            getattr(value, "exit_code", None) == -1
            and stderr.startswith("Command timed out after ")
        )
    ):
        return None
    return str(getattr(value, "error_message", None) or stderr).strip() or (
        f"Command timed out after {timeout} seconds"
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
            if timeout_message := _command_timeout_message(value, timeout):
                raise BackendOperationError(
                    f"command execution failed: {timeout_message}"
                )
            result = _command_result(value)
        except Exception as error:
            raise _convert_error("command execution", error) from error
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
            # A successful submission may precede PID assignment. Resolve the
            # stable command ID with reads, never submit the command twice.
            pid = handle.pid
            deadline = time.monotonic() + 10
            while pid is None:
                values = self._commands.list()
                row = next((v for v in values if v.id == handle.id), None)
                pid = row.pid if row is not None else None
                if pid is None:
                    if time.monotonic() >= deadline:
                        raise BackendOperationError(
                            "submitted command has no PID; it was not resubmitted",
                            outcome="unknown",
                            operation_id=str(handle.id),
                        )
                    time.sleep(0.1)
            pid = int(pid)
        except Exception as error:
            raise _convert_error("background command start", error) from error
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
            return [
                _command_info(value)
                for value in self._commands.list()
                if value.pid is not None
            ]
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
        spec: SandboxSpec | SnapshotRestoreSpec,
        connection: Any,
    ) -> None:
        self.id = str(sandbox.id)
        self.commands = _CommandsDriver(sandbox.commands)
        self.files = _FilesystemDriver(sandbox.files)
        self.pty = adx_environment.Pty(
            self.id, connection=_control_pty_connection(connection)
        )
        self._sandbox = sandbox
        self._spec = spec
        self._connection = connection
        self._terminated = False
        self._closed = False
        self._network_policy = spec.network_policy
        self._network_policy_known = True

    def is_running(self) -> bool:
        if self._terminated or self._closed:
            return False
        return bool(self._sandbox.is_running())

    def get_info(self) -> SandboxInfo:
        try:
            value = self._sandbox.get_info()
        except Exception as error:
            raise _convert_error("get sandbox info", error) from error
        raw_image = getattr(value, "image", None)
        image = str(raw_image).strip() if raw_image is not None else ""
        return SandboxInfo(
            id=str(value.id),
            state=str(value.state),
            cpu=value.cpu,
            memory=value.memory,
            image=image or None,
            xpu=self._spec.xpu,
            storage_mb=self._spec.storage_mb,
        )

    def create_snapshot(self, name: str | None, timeout: int) -> SnapshotInfo:
        try:
            value = self._sandbox.create_snapshot(name=name, timeout_seconds=timeout)
            return SnapshotInfo(str(value.snapshot_id), tuple(value.names))
        except Exception as error:
            raise _convert_error("create snapshot", error) from error

    def fork(
        self,
        count: int,
        idle_timeout: int | None,
        snapshot_timeout: int,
        restore_timeout: int | None,
    ) -> ForkOutcome:
        if self._terminated or self._closed:
            raise BackendOperationError("fork failed: sandbox is closed")
        if not self._network_policy_known:
            raise BackendOperationError(
                "fork requires a confirmed source network policy; reapply it first",
                code="NETWORK_POLICY_UNCONFIRMED",
                outcome="not_started",
                environment_id=self.id,
            )
        timeout = (
            self._spec.schedule_timeout + _CREATE_RESERVE_SECONDS
            if restore_timeout is None
            else restore_timeout
        )
        _validate_restore_timeout(self._spec.schedule_timeout, timeout)
        # The pinned ADX API has snapshot/create/delete primitives, not a batch
        # fork route. Capture once and keep the snapshot until all creates settle.
        snapshot = self.create_snapshot(None, snapshot_timeout)
        results: list[BackendSession | Exception] = []
        cleanup_error: Exception | None = None
        try:
            spec = SnapshotRestoreSpec(
                snapshot_id=snapshot.snapshot_id,
                idle_timeout=(
                    self._spec.idle_timeout if idle_timeout is None else idle_timeout
                ),
                schedule_timeout=self._spec.schedule_timeout,
                timeout=timeout,
                name=None,
                command_cwd=self._spec.command_cwd,
                port_forwardings=self._spec.port_forwardings,
                mounts=self._spec.mounts,
                reverse_tunnel=self._spec.reverse_tunnel,
                detached=False,
                failover=self._spec.failover,
                node_id=None,
                # ADX restores resources but not execution network policy.
                # Send the last acknowledged policy atomically with creation.
                network_policy=self._network_policy,
            )
            for _ in range(count):
                try:
                    results.append(AdxBackend._restore_snapshot(spec, self._connection))
                except Exception as error:
                    results.append(error)
        except BaseException:
            # No result handles reach the caller on interruption. Release only
            # children whose identities are known; never touch the source.
            for child in results:
                if not isinstance(child, Exception):
                    try:
                        child.terminate()
                    except Exception:
                        logger.warning(
                            "failed to clean interrupted fork child %s",
                            child.id,
                            exc_info=True,
                        )
            raise
        finally:
            try:
                AdxBackend._delete_snapshot(snapshot.snapshot_id, self._connection)
            except Exception as error:
                cleanup_error = error
                logger.warning(
                    "fork snapshot %s needs explicit cleanup",
                    snapshot.snapshot_id,
                    exc_info=True,
                )
        return ForkOutcome(snapshot.snapshot_id, results, cleanup_error)

    def reload(self) -> bool:
        if self._terminated or self._closed:
            return False
        reload_sandbox = getattr(self._sandbox, "reload", None)
        if not callable(reload_sandbox):
            raise UnsupportedBackendFeatureError(
                "The installed adx backend does not support "
                "sandbox reload. Upgrade it to a version with failover support."
            )
        try:
            if not reload_sandbox():
                return False
            # The lifecycle commit can precede Edge applying the new route.
            # Probe read-only data traffic; never repeat the rollback itself.
            deadline = time.monotonic() + 10
            while True:
                try:
                    self._sandbox.commands.list()
                    return True
                except adx_environment.EnvironmentError as error:
                    if (
                        getattr(error, "status_code", None) != 409
                        or getattr(error, "retry", None) == "never"
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.1)
        except Exception:
            return False

    def wait_entrypoint(self) -> int:
        if not self._spec.inherit_entrypoint:
            raise RuntimeError("inherit_entrypoint was not enabled for this sandbox")
        wait = getattr(self._sandbox, "wait_entrypoint", None)
        if not callable(wait):
            raise UnsupportedBackendFeatureError(
                "The installed adx backend does not support "
                "waiting for an inherited image entrypoint. Upgrade it to "
                "0.10.2rc1 or newer."
            )
        try:
            return int(wait())
        except Exception as error:
            raise _convert_error(
                "wait for inherited image entrypoint", error
            ) from error

    @property
    def entrypoint_exit_info(self) -> Mapping[str, object] | None:
        if not self._spec.inherit_entrypoint:
            return None
        value = getattr(self._sandbox, "entrypoint_exit_info", None)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise BackendOperationError(
                "inherited image entrypoint returned invalid exit information"
            )
        return dict(value)

    def update_network_policy(self, policy: NetworkPolicy | None) -> None:
        if self._terminated or self._closed:
            raise BackendOperationError(
                "update network policy failed: sandbox is closed"
            )
        update = getattr(self._sandbox, "update_network_policy", None)
        if not callable(update):
            raise UnsupportedBackendFeatureError(
                "The installed adx backend does not support "
                "dynamic network policy updates. Upgrade the backend package."
            )
        native_policy = None if policy is None else _native_network_policy(policy)
        try:
            update(native_policy)
        except BaseException as error:
            # A failed response/reconnect can follow a committed replacement.
            # Require an explicit successful reapply before deriving children.
            self._network_policy_known = False
            if isinstance(error, Exception):
                raise _convert_error("update network policy", error) from error
            retain_failure_context(error, self.id)
            raise
        self._network_policy = policy
        self._network_policy_known = True

    def terminate(self) -> None:
        if self._terminated:
            return
        # Release the native handle before deleting the remote sandbox. This
        # lets transports such as reverse tunnels close while their routes are
        # still available. The stable-ID delete still uses a fresh native
        # client, so a failed deletion remains retryable after local cleanup.
        close_error: Exception | None = None
        try:
            self.close()
        except Exception as error:
            close_error = error
        try:
            adx_environment.Environment.delete(self.id, connection=self._connection)
        except Exception as error:
            failure = _convert_error("terminate sandbox", error)
            retain_failure_context(failure, self.id, close_error)
            if close_error is not None:
                logger.warning(
                    "Local cleanup also failed for sandbox %s: %s",
                    self.id,
                    close_error,
                )
            raise failure from error
        self._terminated = True
        if close_error is not None:
            raise close_error

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._sandbox.close()
        except Exception as error:
            raise _convert_error("close sandbox resources", error) from error
        finally:
            self._closed = True


class _OwnedSandbox(adx_environment.Environment):
    """Native handle whose lifecycle is owned by the AKernel session."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        try:
            super().__init__(*args, **kwargs)
        except BaseException as error:
            # Native 0.1.0rc2 catches Exception only, and can lose a known ID
            # when tunnel bootstrap fails. Retain ownership until construction
            # returns, including Ctrl+C before a _Session exists.
            identity = getattr(self, "_sid", "")
            if identity:
                retain_failure_context(error, identity)
            client = getattr(self, "_client", None)
            if client is not None:
                try:
                    if not getattr(self, "_closed", False):
                        self.close()
                except Exception as cleanup_error:
                    if identity:
                        retain_failure_context(error, identity, cleanup_error)
                try:
                    client.close()
                except Exception as cleanup_error:
                    if identity:
                        retain_failure_context(error, identity, cleanup_error)
            if identity:
                try:
                    # A fresh client makes rollback retryable even if native
                    # construction already closed its transport after failure.
                    adx_environment.Environment.delete(
                        identity, connection=kwargs.get("connection")
                    )
                except Exception as cleanup_error:
                    retain_failure_context(error, identity, cleanup_error)
                    logger.warning(
                        "failed to roll back constructing sandbox %s",
                        identity,
                        exc_info=True,
                    )
            raise

    def __del__(self) -> None:
        # GC can run while HTTPX holds a non-reentrant pool lock. Network
        # deletion and transport closure belong to explicit session cleanup.
        pass


def _rollback_created(
    failure: BaseException,
    sandbox: Any,
    connection: Any,
    session: BackendSession | None,
) -> None:
    """Rollback a known create without replacing its original failure."""
    identity = str(sandbox.id)
    retain_failure_context(failure, identity)
    if session is not None:
        try:
            session.terminate()
        except Exception as cleanup_error:
            retain_failure_context(failure, identity, cleanup_error)
            logger.warning("failed to clean up sandbox %s", identity, exc_info=True)
        return
    for cleanup in (
        sandbox.close,
        lambda: adx_environment.Environment.delete(identity, connection=connection),
    ):
        try:
            cleanup()
        except Exception as cleanup_error:
            retain_failure_context(failure, identity, cleanup_error)
            logger.warning("failed to clean up sandbox %s", identity, exc_info=True)


class AdxBackend:
    """Backend implemented by ``adx-environment``."""

    name = "adx"
    namespace = _NAMESPACE
    capabilities = frozenset(
        {
            Capability.S3_ROOTFS,
            Capability.NODE_PLACEMENT,
        }
    )

    def __init__(self, config: BackendConfig) -> None:
        self._connection = adx_environment.ConnectionConfig(
            server_address=config.api_endpoint.authority(),
            token=config.token,
            use_tls=config.api_endpoint.use_tls,
            gateway_address=config.gateway_endpoint.authority(),
            gateway_use_tls=config.gateway_endpoint.use_tls,
            verify_tls=config.verify_tls,
        )

    def pty_for(self, instance_id: str) -> Any:
        """Attach a PTY to an existing sandbox with the backend connection."""

        return adx_environment.Pty(
            instance_id, connection=_control_pty_connection(self._connection)
        )

    def _validate(self, spec: SandboxSpec) -> None:
        tunnel = spec.reverse_tunnel
        if tunnel is not None and tunnel.reverse_port != tunnel.listen_port - 1:
            raise UnsupportedBackendFeatureError(
                "Backend 'adx' requires reverse_port to equal listen_port - 1."
            )

    def create(self, spec: SandboxSpec) -> BackendSession:
        self._validate(spec)
        create_started = time.monotonic()
        rootfs = None
        if spec.rootfs is not None:
            rootfs = adx_environment.S3Config(
                endpoint=spec.rootfs.endpoint,
                bucket=spec.rootfs.bucket,
                object=spec.rootfs.object,
                access_key=spec.rootfs.access_key,
                secret_key=spec.rootfs.secret_key,
            )
        network = None
        if spec.network_policy is not None:
            network = _native_network_policy(spec.network_policy)
        mounts = [_native_mount(mount) for mount in spec.mounts]
        create_timeout = spec.schedule_timeout + 60
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
                spec.reverse_tunnel.target if spec.reverse_tunnel is not None else None
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
            storage_limit_mb=spec.storage_limit_mb,
            network=network,
            extra_config=dict(spec.extra_config),
            create_timeout=create_timeout,
            failover=spec.failover,
            inherit_entrypoint=spec.inherit_entrypoint,
            data_plane_security=adx_environment.DataPlaneSecurityPolicy(
                tunnel_mode="tls",
                port_forward_mode="tls",
            ),
            connection=self._connection,
        )
        try:
            sandbox = _OwnedSandbox(**create_args)
        except Exception as error:
            raise _convert_error("create sandbox", error) from error
        session = None
        try:
            session = _Session(sandbox, spec, self._connection)
            # Creation is authoritative before Edge necessarily applies the
            # next route-stream delta. A read-only direct operation closes
            # that gap so the first caller operation does not observe 503.
            deadline = min(create_started + create_timeout, time.monotonic() + 10)
            retry_delay = 0.1
            while True:
                try:
                    session.commands.list()
                    break
                except BackendOperationError as error:
                    if not _route_read_retryable(error) or time.monotonic() >= deadline:
                        raise
                    time.sleep(min(retry_delay, max(0, deadline - time.monotonic())))
                    retry_delay = min(retry_delay * 2, 1.0)
        except BaseException as error:
            _rollback_created(error, sandbox, self._connection, session)
            raise
        if spec.command_cwd is not None:
            try:
                # The public constructor creates its working directory; the
                # native SDK only uses cwd as a command default.
                sandbox.files.make_dir(spec.command_cwd)
            except BaseException as error:
                _rollback_created(error, sandbox, self._connection, session)
                if isinstance(error, Exception):
                    raise _convert_error(
                        f"initialize sandbox cwd {spec.command_cwd!r}", error
                    ) from error
                raise
        return session

    def from_snapshot(self, spec: SnapshotRestoreSpec) -> BackendSession:
        return self._restore_snapshot(spec, self._connection)

    @staticmethod
    def _restore_snapshot(spec: SnapshotRestoreSpec, connection: Any) -> BackendSession:
        _validate_restore_timeout(spec.schedule_timeout, spec.timeout)
        tunnel = spec.reverse_tunnel
        if tunnel is not None and tunnel.reverse_port != tunnel.listen_port - 1:
            raise UnsupportedBackendFeatureError(
                "Backend 'adx' requires reverse_port to equal listen_port - 1."
            )
        started = time.monotonic()
        try:
            sandbox = _OwnedSandbox.create(
                spec.snapshot_id,
                idle_timeout=spec.idle_timeout,
                schedule_timeout=spec.schedule_timeout,
                create_timeout=spec.timeout,
                name=spec.name,
                cwd=spec.command_cwd,
                port_forwardings=list(spec.port_forwardings),
                mounts=[_native_mount(mount) for mount in spec.mounts],
                upstream=tunnel.target if tunnel else None,
                proxy_port=tunnel.listen_port if tunnel else _DEFAULT_LISTEN_PORT,
                tunnel_connect_timeout=tunnel.connect_timeout if tunnel else None,
                detached=spec.detached,
                failover=spec.failover,
                node_id=spec.node_id,
                network=(
                    _native_network_policy(spec.network_policy)
                    if spec.network_policy is not None
                    else None
                ),
                data_plane_security=adx_environment.DataPlaneSecurityPolicy(
                    tunnel_mode="tls",
                    port_forward_mode="tls",
                ),
                connection=connection,
            )
        except Exception as error:
            raise _convert_error("restore snapshot", error) from error
        session = None
        try:
            session = _Session(sandbox, spec, connection)
            deadline = min(started + spec.timeout, time.monotonic() + 10)
            while True:
                try:
                    session.commands.list()
                    break
                except BackendOperationError as error:
                    if not _route_read_retryable(error) or time.monotonic() >= deadline:
                        raise
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except BaseException as error:
            _rollback_created(error, sandbox, connection, session)
            raise
        return session

    def get_snapshot(self, snapshot_id: str) -> SnapshotInfo:
        try:
            value = adx_environment.Environment.get_snapshot(
                snapshot_id,
                connection=self._connection,
            )
            return SnapshotInfo(str(value.snapshot_id), tuple(value.names))
        except Exception as error:
            raise _convert_error("get snapshot", error) from error

    def list_snapshots(
        self,
        name: str | None,
        next_token: str | None,
        limit: int | None,
    ) -> tuple[list[SnapshotInfo], str | None]:
        try:
            values, token = adx_environment.Environment.list_snapshots(
                name=name,
                page_token=next_token,
                page_size=limit,
                connection=self._connection,
            )
            return [
                SnapshotInfo(str(v.snapshot_id), tuple(v.names)) for v in values
            ], token or None
        except Exception as error:
            raise _convert_error("list snapshots", error) from error

    def delete_snapshot(self, snapshot_id: str) -> None:
        self._delete_snapshot(snapshot_id, self._connection)

    @staticmethod
    def _delete_snapshot(snapshot_id: str, connection: Any) -> None:
        try:
            adx_environment.Environment.delete_snapshot(
                snapshot_id,
                connection=connection,
            )
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return
            raise _convert_error("delete snapshot", error) from error

    def delete_named(self, name: str) -> None:
        sandbox_id = f"{self.namespace}-{name}"
        try:
            adx_environment.Environment.delete(
                sandbox_id,
                connection=self._connection,
            )
        except Exception as error:
            raise _convert_error(f"delete sandbox {name!r}", error) from error

    def close(self) -> None:
        """The backend has no process-level client to close."""


def create_backend(config: BackendConfig) -> Backend:
    """Construct the ``adx-environment`` backend for the lazy registry."""

    return AdxBackend(config)
