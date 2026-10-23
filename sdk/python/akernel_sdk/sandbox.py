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

"""Public Sandbox API for AKernel."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from ._addresses import gateway_endpoint_from_env
from ._backends.base import BackendSession, SandboxSpec, SnapshotRestoreSpec
from ._backends.errors import retain_failure_context
from ._backends.registry import load_backend
from ._dockerfile_launch import DockerfileLaunch
from ._fork import ForkCleanupError
from ._sandbox_resources import (
    normalize_xpu,
    validate_storage_limit_mb,
    validate_storage_mb,
)
from ._snapshots import SnapshotPaginator, optional_name
from ._snapshots import snapshot_id as _snapshot_id
from .commands import CommandHandle, Commands
from .filesystem import Filesystem
from .pty import Pty
from .types import (
    HttpReverseTunnel,
    Mount,
    NetworkPolicy,
    S3Config,
    SandboxInfo,
    SnapshotInfo,
)

logger = logging.getLogger(__name__)


def _validate_port(name: str, port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError(f"{name} must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")


def _normalize_ports(port_forwardings: Sequence[int] | None) -> list[int]:
    if port_forwardings is None:
        return []
    if isinstance(port_forwardings, (str, bytes)):
        raise TypeError("port_forwardings must be a sequence of integers")
    ports = list(port_forwardings)
    for port in ports:
        _validate_port("forwarded port", port)
    if len(set(ports)) != len(ports):
        raise ValueError("port_forwardings must not contain duplicate ports")
    return ports


def _normalize_mounts(mounts: Sequence[Mount] | None) -> list[Mount]:
    if mounts is None:
        return []
    if isinstance(mounts, (str, bytes)):
        raise TypeError("mounts must be a sequence of Mount objects")
    result = list(mounts)
    if not all(isinstance(mount, Mount) for mount in result):
        raise TypeError("mounts must contain only Mount objects")
    return result


def _normalize_extra_config(
    extra_config: Mapping[str, object] | None,
) -> Mapping[str, object]:
    """Validate and defensively copy runtime-owned JSON configuration."""

    if extra_config is None:
        return MappingProxyType({})
    if not isinstance(extra_config, Mapping):
        raise TypeError("extra_config must be a mapping")

    active_containers: set[int] = set()

    def normalize(value: object, path: str) -> object:
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{path} must contain only finite numbers")
            return value
        if isinstance(value, Mapping):
            marker = id(value)
            if marker in active_containers:
                raise ValueError("extra_config must not contain circular references")
            active_containers.add(marker)
            try:
                result: dict[str, object] = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise TypeError(f"{path} keys must be strings")
                    result[key] = normalize(item, f"{path}.{key}")
                return result
            finally:
                active_containers.remove(marker)
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            marker = id(value)
            if marker in active_containers:
                raise ValueError("extra_config must not contain circular references")
            active_containers.add(marker)
            try:
                return [
                    normalize(item, f"{path}[{index}]")
                    for index, item in enumerate(value)
                ]
            finally:
                active_containers.remove(marker)
        raise TypeError(f"{path} contains a non-JSON-compatible value")

    normalized = normalize(extra_config, "extra_config")
    assert isinstance(normalized, dict)
    return MappingProxyType(normalized)


def _validate_integer(
    name: str,
    value: int,
    *,
    minimum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be greater than or equal to {minimum}")


class Sandbox:
    """A remote AKernel sandbox.

    The public API is backend-neutral. The selected backend and cluster
    determine which sandbox runtime identifiers are available.
    """

    def __init__(
        self,
        image: str | None = None,
        rootfs: S3Config | None = None,
        runtime: str = "runsc",
        cpu: int = 1000,
        memory: int = 4096,
        cpu_limit: int = 0,
        mem_limit: int = 0,
        idle_timeout: int = 300,
        schedule_timeout: int = 30,
        env: Mapping[str, str] | None = None,
        name: str | None = None,
        cwd: str | None = None,
        port_forwardings: Sequence[int] | None = None,
        mounts: Sequence[Mount] | None = None,
        reverse_tunnel: HttpReverseTunnel | None = None,
        detached: bool = False,
        node_id: str | None = None,
        *,
        failover: bool = False,
        inherit_entrypoint: bool = False,
        xpu: str | None = None,
        storage_mb: int | None = None,
        storage_limit_mb: int = 0,
        network_policy: NetworkPolicy | None = None,
        dockerfile: DockerfileLaunch | None = None,
        extra_config: Mapping[str, object] | None = None,
    ) -> None:
        """Create and wait for a sandbox to become ready.

        Args:
            image: OCI image used as the sandbox root filesystem.
            rootfs: S3-compatible EROFS root filesystem configuration.
            runtime: Sandbox runtime identifier. Defaults to ``runsc``;
                availability is determined by the backend and cluster.
            cpu: Requested CPU in millicores.
            memory: Requested memory in MiB.
            cpu_limit: CPU limit in millicores, or zero to follow ``cpu``.
            mem_limit: Memory limit in MiB, or zero to follow ``memory``.
            idle_timeout: Seconds before an idle sandbox is reclaimed.
            schedule_timeout: Positive scheduling timeout in seconds.
            env: Environment variables applied to the sandbox process.
            name: Optional stable name for a detached sandbox.
            cwd: Default working directory for subsequent ``commands.run()``
                calls that omit ``cwd``. The directory and any missing parents
                are created before the sandbox is returned. This does not
                override the inherited image entrypoint's working directory,
                which uses the image's OCI WORKDIR when
                ``inherit_entrypoint=True``.
            port_forwardings: Sandbox TCP ports exposed through the gateway.
            mounts: Additional read-only OCI/S3 mounts or explicit node HostPath
                bind mounts for trusted management callers. HostPath writes
                require ``Mount(..., read_only=False)``.
            reverse_tunnel: SDK-side HTTP service exposed inside the sandbox.
            detached: Keep the sandbox alive when this client closes.
            node_id: Require placement on a specific AKernel node.
            failover: Restore the same logical sandbox from its latest
                checkpoint after failure. A shared checkpoint can move to
                another eligible node; `node_id` still requires the specified
                node. Use :meth:`reload` to request rollback explicitly.
            inherit_entrypoint: Start the OCI image's effective ENTRYPOINT and
                CMD as the sandbox workload. Valid only with ``image``.
            xpu: Experimental whole-device accelerator request in
                ``type:model:count`` format. Currently only exact-model NVIDIA
                GPU requests are supported. The backend validates runtime
                compatibility.
            storage_mb: Experimental storage scheduling quota in MiB, reserved
                when placing the sandbox. When omitted, a positive
                ``storage_limit_mb`` is reserved, or the cluster default is
                used if no positive limit is given.
            storage_limit_mb: Actual hard limit on writable root filesystem
                capacity in MiB. Zero follows ``storage_mb`` or the cluster
                default. A positive limit must be at least ``storage_mb`` when
                that scheduling quota is specified. The backend validates
                runtime compatibility.
            network_policy: Optional creation-time network policy. Omitting it
                leaves sandbox networking unrestricted.
            dockerfile: Supported Dockerfile direct-launch configuration.
                Dockerfile direct launch remains available as a supported
                capability. Its documented strict subset evolves incrementally
                with production experience; unsupported inputs fail closed. The
                specific API surface may evolve, with documentation and migration
                guidance for material changes. ``FROM`` supplies only the root
                filesystem; its OCI ENV, USER, WORKDIR, CMD and ENTRYPOINT
                configuration is not inherited. The sandbox applies only state
                explicitly declared in this Dockerfile, then executes build-time
                instructions in-sandbox. Mutually exclusive with ``image`` and
                ``rootfs``.
            extra_config: Optional JSON-compatible configuration owned by the
                selected runtime. AKernel validates and forwards it without
                interpreting runtime-specific fields.

        Raises:
            TypeError: An argument has an invalid type.
            ValueError: Arguments are invalid or mutually incompatible.
            RuntimeError: The backend cannot create or initialize the sandbox.
        """

        if image is not None and (not isinstance(image, str) or not image.strip()):
            raise ValueError("image must be a non-empty string")
        if rootfs is not None and not isinstance(rootfs, S3Config):
            raise TypeError("rootfs must be an S3Config")
        if dockerfile is not None:
            if not isinstance(dockerfile, DockerfileLaunch):
                raise TypeError("dockerfile must be a DockerfileLaunch")
        if sum(value is not None for value in (image, rootfs, dockerfile)) > 1:
            raise ValueError(
                "image, rootfs and dockerfile are mutually exclusive: at most one "
                "may be given"
            )
        if not isinstance(runtime, str):
            raise TypeError("runtime must be a string")
        runtime = runtime.strip()
        if not runtime:
            raise ValueError("runtime must be a non-empty string")
        normalized_xpu = normalize_xpu(xpu)
        validate_storage_mb(storage_mb)
        validate_storage_limit_mb(storage_limit_mb, storage_mb)
        if network_policy is not None and not isinstance(network_policy, NetworkPolicy):
            raise TypeError("network_policy must be a NetworkPolicy or None")
        _validate_integer("cpu", cpu, minimum=1)
        _validate_integer("memory", memory, minimum=1)
        _validate_integer("cpu_limit", cpu_limit, minimum=0)
        _validate_integer("mem_limit", mem_limit, minimum=0)
        _validate_integer("idle_timeout", idle_timeout, minimum=0)
        _validate_integer("schedule_timeout", schedule_timeout, minimum=1)
        if cpu_limit and cpu_limit < cpu:
            raise ValueError("cpu_limit must be 0 or greater than or equal to cpu")
        if mem_limit and mem_limit < memory:
            raise ValueError("mem_limit must be 0 or greater than or equal to memory")
        if env is not None:
            if not isinstance(env, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            ):
                raise TypeError("env must map strings to strings")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError("name must be a non-empty string")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise TypeError("cwd must be a string")
            if not cwd.startswith("/"):
                raise ValueError("cwd must be an absolute POSIX path")
        if not isinstance(detached, bool):
            raise TypeError("detached must be a boolean")
        if not isinstance(failover, bool):
            raise TypeError("failover must be a boolean")
        if not isinstance(inherit_entrypoint, bool):
            raise TypeError("inherit_entrypoint must be a boolean")
        if inherit_entrypoint and image is None:
            raise ValueError("inherit_entrypoint requires an image")
        if node_id is not None:
            if not isinstance(node_id, str):
                raise TypeError("node_id must be a string")
            if not node_id.strip():
                raise ValueError("node_id must be a non-empty string")
        if reverse_tunnel is not None and not isinstance(
            reverse_tunnel, HttpReverseTunnel
        ):
            raise TypeError("reverse_tunnel must be an HttpReverseTunnel")

        ports = _normalize_ports(port_forwardings)
        mount_list = _normalize_mounts(mounts)
        normalized_extra_config = _normalize_extra_config(extra_config)
        if reverse_tunnel is not None:
            conflicts = set(ports).intersection(
                {reverse_tunnel.reverse_port, reverse_tunnel.listen_port}
            )
            if conflicts:
                rendered = ", ".join(str(port) for port in sorted(conflicts))
                raise ValueError(
                    f"reverse tunnel ports conflict with port_forwardings: {rendered}"
                )

        parsed_dockerfile = None
        if dockerfile is not None:
            from ._dockerfile import parse_dockerfile

            parsed_dockerfile = parse_dockerfile(dockerfile.context, strict=True)
            image = parsed_dockerfile.base_image
            if not isinstance(image, str) or not image.strip():
                raise ValueError("Dockerfile base image must be a non-empty string")

        self._session: BackendSession | None = None
        self._startup_command: CommandHandle | None = None
        self._pty: Pty | None = None
        self._closed = False
        self._terminated = detached
        self._detached = detached
        self._reverse_tunnel = reverse_tunnel
        self._forwarded_ports = set(ports)
        self._image = image
        self._cpu: int | None = cpu
        self._memory: int | None = memory
        self._xpu = normalized_xpu
        self._storage_mb = storage_mb
        self._inherit_entrypoint = inherit_entrypoint
        self._id = ""

        spec = SandboxSpec(
            image=image,
            rootfs=rootfs,
            runtime=runtime,
            cpu=cpu,
            memory=memory,
            cpu_limit=cpu_limit,
            mem_limit=mem_limit,
            idle_timeout=idle_timeout,
            schedule_timeout=schedule_timeout,
            env=MappingProxyType(dict(env or {})),
            name=name,
            command_cwd=cwd,
            port_forwardings=tuple(ports),
            mounts=tuple(mount_list),
            reverse_tunnel=reverse_tunnel,
            detached=detached,
            failover=failover,
            inherit_entrypoint=inherit_entrypoint,
            node_id=node_id,
            xpu=normalized_xpu,
            storage_mb=storage_mb,
            storage_limit_mb=storage_limit_mb,
            network_policy=(
                None
                if network_policy is None or network_policy.is_empty
                else network_policy
            ),
            extra_config=normalized_extra_config,
        )
        self._session = load_backend().create(spec)
        try:
            self._id = self._session.id
            self._files = Filesystem(self._session.files)
            self._commands = Commands(self._session.commands)
            self._pty = Pty(self._id, driver=self._session.pty)
            if dockerfile is not None and parsed_dockerfile is not None:
                from ._dockerfile_runner import apply_dockerfile

                apply_result = apply_dockerfile(
                    self,
                    parsed_dockerfile,
                    dockerfile.context,
                    auto_start_cmd=dockerfile.auto_start_cmd,
                    run_timeout=dockerfile.run_timeout,
                )
                self._startup_command = apply_result.startup_command
        except BaseException as failure:
            retain_failure_context(failure, self._session.id)
            self._closed = True
            try:
                self._session.terminate()
            except Exception as error:
                retain_failure_context(failure, self._session.id, error)
                logger.warning(
                    "failed to roll back a partially initialized sandbox",
                    exc_info=True,
                )
            try:
                self._session.close()
            except Exception as error:
                retain_failure_context(failure, self._session.id, error)
                logger.warning(
                    "failed to close a partially initialized sandbox session",
                    exc_info=True,
                )
            raise

    def create_snapshot(
        self, name: str | None = None, *, timeout: int = 300
    ) -> SnapshotInfo:
        """Save a reusable memory/filesystem snapshot and keep this sandbox running.

        Runtime support is required. External services, client-side Python state
        and open client transports are not captured. The snapshot outlives its
        source; explicitly delete it when no longer needed. An ambiguous failure
        is propagated with request/outcome context, never automatically retried.
        ``timeout`` is in seconds (1..3600).
        """
        name = optional_name(name)
        _validate_integer("timeout", timeout, minimum=1)
        if timeout > 3600:
            raise ValueError("timeout must be between 1 and 3600")
        if self._closed or self._session is None:
            raise RuntimeError("cannot snapshot a closed sandbox")
        return self._session.create_snapshot(name, timeout)

    def fork(
        self,
        count: int = 1,
        *,
        idle_timeout: int | None = None,
        snapshot_timeout: int = 300,
        restore_timeout: int | None = None,
    ) -> list[Sandbox | Exception]:
        """Capture once and derive ``count`` independent running sandboxes.

        Like E2B, return one ordered Sandbox or Exception per requested child;
        partial failures keep successful siblings. Capture failures raise.
        The source keeps its identity and lifetime. Children inherit resources
        and this client's acknowledged policy, cwd, ports, mounts and tunnel.
        They are owned handles, including when the source is detached.

        ``count`` is 1..20. All timeouts use seconds: ``snapshot_timeout`` bounds
        capture (1..3600), ``restore_timeout`` is each child's logical create
        budget (minimum/default source schedule_timeout + 60), and
        ``idle_timeout`` overrides the source idle timeout for children.
        Invalid create budgets fail before capture. Transport margins, tunnel
        setup and cleanup add elapsed time; this is not an overall wall-clock
        deadline or an E2B-style absolute lifetime.

        The ADX adapter currently composes snapshot/create/delete requests,
        creating children sequentially. Temporary snapshot deletion is requested
        after all attempts; physical collection waits for child references.
        On deletion failure, ForkCleanupError preserves results and snapshot ID.
        Unknown create outcomes retain structured operation context and are not
        blindly replayed. External services and writable host mounts are shared
        external state; Python handles/streams are not cloned. Quiesce tools
        before forking and reconnect streams if capture interrupts them.
        """
        _validate_integer("count", count, minimum=1)
        if count > 20:
            raise ValueError("count must be between 1 and 20")
        _validate_integer("snapshot_timeout", snapshot_timeout, minimum=1)
        if snapshot_timeout > 3600:
            raise ValueError("snapshot_timeout must be between 1 and 3600")
        if idle_timeout is not None:
            _validate_integer("idle_timeout", idle_timeout, minimum=0)
        if restore_timeout is not None:
            _validate_integer("restore_timeout", restore_timeout, minimum=1)
        if self._closed or self._session is None:
            raise RuntimeError("cannot fork a closed sandbox")
        outcome = self._session.fork(
            count, idle_timeout, snapshot_timeout, restore_timeout
        )
        results: list[Sandbox | Exception] = []
        cleanup_failures: list[tuple[Sandbox, Exception]] = []
        try:
            for child in outcome.results:
                if isinstance(child, Exception):
                    results.append(child)
                    continue
                try:
                    results.append(
                        type(self)._from_session(
                            child,
                            reverse_tunnel=self._reverse_tunnel,
                            ports=sorted(self._forwarded_ports),
                            detached=False,
                            cleanup_failures=cleanup_failures,
                        )
                    )
                except Exception as error:
                    results.append(error)
        except BaseException:
            for child in outcome.results:
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
        if outcome.cleanup_error is not None or cleanup_failures:
            cleanup_error = (
                outcome.cleanup_error
                if outcome.cleanup_error is not None
                else cleanup_failures[0][1]
            )
            raise ForkCleanupError(
                outcome.snapshot_id,
                results,
                cleanup_error,
                pending_sandboxes=tuple(child for child, _ in cleanup_failures),
                child_cleanup_errors=tuple(error for _, error in cleanup_failures),
            ) from cleanup_error
        return results

    @classmethod
    def get_snapshot(cls, snapshot_id: str | SnapshotInfo) -> SnapshotInfo:
        """Query catalog metadata by opaque ID, including pending deletion.

        A returned descriptor does not guarantee that new restores are allowed.
        The backend checks snapshot readiness when restoring.
        """
        return load_backend().get_snapshot(_snapshot_id(snapshot_id))

    @classmethod
    def list_snapshots(
        cls,
        *,
        name: str | None = None,
        limit: int | None = None,
        next_token: str | None = None,
    ) -> SnapshotPaginator:
        """List the accessible catalog, optionally filtered by name.

        Like E2B, ``limit`` is the page size and pages are requested using
        ``next_items()``. Unlike E2B's instance listing, this class method does
        not filter by source sandbox, even when accessed through an instance.
        """
        name = optional_name(name)
        if limit is not None:
            _validate_integer("limit", limit, minimum=1)
        if next_token is not None and not isinstance(next_token, str):
            raise TypeError("next_token must be a string or None")
        backend = load_backend()
        return SnapshotPaginator(
            lambda token: backend.list_snapshots(name, token, limit), next_token
        )

    @classmethod
    def delete_snapshot(cls, snapshot_id: str | SnapshotInfo) -> None:
        """Delete a reusable snapshot; an already absent snapshot is a no-op.

        Existing sandboxes restored from it remain alive. Deletion prevents
        new restores; physical collection is asynchronous after references are
        released, and metadata may remain queryable while deletion is pending.
        Unlike E2B's boolean return, this API returns None: the backend does not
        distinguish new deletion from an already requested/absent snapshot.
        """
        load_backend().delete_snapshot(_snapshot_id(snapshot_id))

    @classmethod
    def from_snapshot(
        cls,
        snapshot_id: str | SnapshotInfo,
        *,
        idle_timeout: int = 300,
        schedule_timeout: int = 30,
        timeout: int | None = None,
        name: str | None = None,
        cwd: str | None = None,
        port_forwardings: Sequence[int] | None = None,
        mounts: Sequence[Mount] | None = None,
        reverse_tunnel: HttpReverseTunnel | None = None,
        detached: bool = False,
        failover: bool = False,
        node_id: str | None = None,
        network_policy: NetworkPolicy | None = None,
    ) -> Sandbox:
        """Restore a ready snapshot into a new sandbox with a new ID.

        Inherits captured runtime, resource geometry and process state; does not
        rerun Dockerfile setup or the image entrypoint. The source is unaffected.
        Rebind client-side tunnels, ports and mounts explicitly as needed.
        ``cwd`` sets a command default without creating directories. Supply
        ``network_policy`` explicitly to restore restrictions: the pinned ADX
        backend uses its default if omitted or empty. ``timeout`` is the logical
        create budget, at least schedule_timeout + 60 seconds (also the default).
        Shorter budgets raise ValueError before creation; transport, tunnel and
        cleanup overhead can add elapsed time.
        The SDK never starts a second logical restore to hide a failed one;
        the transport may retry the same operation using a stable request ID.
        Client handles from the source must not be reused against the new sandbox.
        """
        identity = _snapshot_id(snapshot_id)
        name = optional_name(name)
        _validate_integer("idle_timeout", idle_timeout, minimum=0)
        _validate_integer("schedule_timeout", schedule_timeout, minimum=1)
        if timeout is None:
            timeout = schedule_timeout + 60
        _validate_integer("timeout", timeout, minimum=1)
        for key, value in (("detached", detached), ("failover", failover)):
            if not isinstance(value, bool):
                raise TypeError(f"{key} must be a boolean")
        if cwd is not None and (not isinstance(cwd, str) or not cwd.startswith("/")):
            raise ValueError("cwd must be an absolute POSIX path")
        if node_id is not None and (
            not isinstance(node_id, str) or not node_id.strip()
        ):
            raise ValueError("node_id must be a non-empty string")
        if network_policy is not None and not isinstance(network_policy, NetworkPolicy):
            raise TypeError("network_policy must be a NetworkPolicy or None")
        if reverse_tunnel is not None and not isinstance(
            reverse_tunnel, HttpReverseTunnel
        ):
            raise TypeError("reverse_tunnel must be an HttpReverseTunnel")
        ports = _normalize_ports(port_forwardings)
        mount_list = _normalize_mounts(mounts)
        if reverse_tunnel is not None and set(ports).intersection(
            {reverse_tunnel.listen_port, reverse_tunnel.reverse_port}
        ):
            raise ValueError("reverse tunnel ports conflict with port_forwardings")
        spec = SnapshotRestoreSpec(
            snapshot_id=identity,
            idle_timeout=idle_timeout,
            schedule_timeout=schedule_timeout,
            timeout=timeout,
            name=name,
            command_cwd=cwd,
            port_forwardings=tuple(ports),
            mounts=tuple(mount_list),
            reverse_tunnel=reverse_tunnel,
            detached=detached,
            failover=failover,
            node_id=node_id,
            network_policy=network_policy,
        )
        return cls._from_session(
            load_backend().from_snapshot(spec),
            reverse_tunnel=reverse_tunnel,
            ports=ports,
            detached=detached,
        )

    @classmethod
    def _from_session(
        cls,
        session: BackendSession,
        *,
        reverse_tunnel: HttpReverseTunnel | None,
        ports: Sequence[int],
        detached: bool,
        cleanup_failures: list[tuple[Sandbox, Exception]] | None = None,
    ) -> Sandbox:
        # Bypass fresh creation: __init__ would repeat fresh-boot side effects.
        result = cls.__new__(cls)
        result._session = session
        result._startup_command = None
        result._pty = None
        result._closed = False
        result._terminated = detached
        result._detached = detached
        result._reverse_tunnel = reverse_tunnel
        result._forwarded_ports = set(ports)
        result._inherit_entrypoint = False
        result._id = result._session.id
        try:
            info = result._session.get_info()
            result._image, result._cpu, result._memory = (
                info.image,
                info.cpu,
                info.memory,
            )
            result._xpu, result._storage_mb = info.xpu, info.storage_mb
            result._files = Filesystem(result._session.files)
            result._commands = Commands(result._session.commands)
            result._pty = Pty(result._id, driver=result._session.pty)
        except BaseException as failure:
            retain_failure_context(failure, result._session.id)
            try:
                result._session.terminate()
            except Exception as error:
                retain_failure_context(failure, result._session.id, error)
                if cleanup_failures is not None:
                    cleanup_failures.append((result, error))
                logger.warning(
                    "failed to clean up restored sandbox %s", result.id, exc_info=True
                )
            raise
        return result

    @property
    def files(self) -> Filesystem:
        """Filesystem operations for this sandbox."""

        return self._files

    @property
    def commands(self) -> Commands:
        """Command execution and process management for this sandbox."""

        return self._commands

    @property
    def startup_command(self) -> CommandHandle | None:
        """Background CMD/ENTRYPOINT handle for a Dockerfile launch, if dispatched.

        The handle is available only when ``DockerfileLaunch.auto_start_cmd``
        is true and the Dockerfile declares a startup command. It is None for
        normal image/rootfs launches, disabled startup dispatch, or Dockerfiles
        without CMD or ENTRYPOINT. Sandbox construction does not guarantee that
        the process remains running or healthy after dispatch.

        For ``image=..., inherit_entrypoint=True``, this property remains None;
        use :meth:`wait_entrypoint` and :attr:`entrypoint_exit_info` instead.
        """

        return self._startup_command

    @property
    def pty(self) -> Pty:
        """Factory for interactive pseudo-terminal sessions."""

        assert self._pty is not None
        return self._pty

    @property
    def id(self) -> str:
        """Logical sandbox ID, matching the value displayed by ``ak list``."""

        return self._id

    @property
    def reverse_tunnel(self) -> HttpReverseTunnel | None:
        """Configured reverse tunnel, or ``None`` when it is disabled."""

        return self._reverse_tunnel

    def reload(self) -> bool:
        """Roll this sandbox back to its latest local anonymous checkpoint.

        The logical sandbox identity and existing command, filesystem, and PTY
        facades remain valid. ``False`` means that the rollback was not
        completed, including when no usable checkpoint exists, the sandbox has
        already been closed, or the backend reports an operational failure.
        """

        if self._closed or self._session is None:
            return False
        return self._session.reload()

    def wait_entrypoint(self) -> int:
        """Wait for the inherited OCI image process and return its exit code.

        Requires ``image=..., inherit_entrypoint=True``. For Dockerfile direct
        launches, use :attr:`startup_command` and its ``wait()`` method instead.
        This waits for process exit, not application readiness. An exit after
        successful sandbox creation does not by itself terminate the sandbox.
        """

        if not self._inherit_entrypoint:
            raise RuntimeError("inherit_entrypoint was not enabled for this sandbox")
        if self._closed or self._session is None:
            raise RuntimeError("sandbox is closed")
        return self._session.wait_entrypoint()

    @property
    def entrypoint_exit_info(self) -> Mapping[str, object] | None:
        """Structured exit details cached after :meth:`wait_entrypoint`.

        None before exit details have been collected or when entrypoint
        inheritance is disabled, including Dockerfile direct launches.
        """

        if not self._inherit_entrypoint or self._session is None:
            return None
        return self._session.entrypoint_exit_info

    def update_network_policy(self, policy: NetworkPolicy | None) -> None:
        """Atomically replace the complete network policy of this sandbox.

        Passing None or an empty NetworkPolicy clears the existing policy and
        restores unrestricted networking. The desired policy is retained
        across sandboxd restarts, explicit reloads, and same-node failover.

        Args:
            policy: The replacement policy, or None to clear it.

        Raises:
            TypeError: If policy is not a NetworkPolicy or None.
            RuntimeError: If the sandbox is already closed.
            UnsupportedBackendFeatureError: If the selected backend cannot
                update policies dynamically.
        """

        if policy is not None and not isinstance(policy, NetworkPolicy):
            raise TypeError("policy must be a NetworkPolicy or None")
        if self._closed or self._session is None:
            raise RuntimeError("sandbox is closed")
        normalized = None if policy is None or policy.is_empty else policy
        self._session.update_network_policy(normalized)

    def get_port_url(self, port: int, *, internal: bool = False) -> str:
        """Return the gateway URL for a declared sandbox port.

        Args:
            port: Port included in ``port_forwardings`` at sandbox creation.
            internal: Kept for caller compatibility. ADX uses the configured
                gateway address for both internal and public access.

        Raises:
            ValueError: The port is invalid or was not declared.
        """

        _validate_port("port", port)
        if port not in self._forwarded_ports:
            raise ValueError(
                f"port {port} is not in port_forwardings: "
                f"{sorted(self._forwarded_ports)}"
            )

        gateway = gateway_endpoint_from_env()
        return f"{gateway.base_url()}/{self.id}/{port}"

    def is_running(self) -> bool:
        """Return whether the sandbox currently responds to a health check."""

        if self._closed or self._session is None:
            return False
        return self._session.is_running()

    def get_info(self) -> SandboxInfo:
        """Return sandbox state and resources.

        After confirmed termination of an owned sandbox, return local stopped
        state without using a closed transport. Failed deletion and detached
        client closure do not establish remote termination.
        """

        if self._session is None or (
            self._closed and self._terminated and not self._detached
        ):
            return SandboxInfo(
                id=self.id,
                state="stopped",
                cpu=self._cpu,
                memory=self._memory,
                image=self._image,
                xpu=self._xpu,
                storage_mb=self._storage_mb,
            )
        info = self._session.get_info()
        return SandboxInfo(
            id=info.id,
            state=info.state,
            cpu=info.cpu,
            memory=info.memory,
            image=info.image,
            xpu=info.xpu if info.xpu is not None else self._xpu,
            storage_mb=(
                info.storage_mb if info.storage_mb is not None else self._storage_mb
            ),
        )

    def kill(self) -> None:
        """Release client resources and terminate a non-detached sandbox.

        Call explicitly or use a context manager; garbage collection does not
        clean up sandboxes. Failures propagate after local cleanup is attempted.
        If remote deletion fails, another explicit kill() can retry even though
        the local handle is closed. Detached sandboxes only close local clients.
        """

        if self._closed and self._terminated:
            return

        local_errors: list[Exception] = []
        terminate_error: Exception | None = None

        if self._session is not None:
            if not self._terminated:
                try:
                    self._session.terminate()
                except Exception as error:
                    terminate_error = error
                else:
                    self._terminated = True

        if not self._closed:
            if self._pty is not None:
                try:
                    self._pty._close()
                except Exception as error:
                    local_errors.append(error)

            if self._session is not None:
                try:
                    self._session.close()
                except Exception as error:
                    local_errors.append(error)
            self._closed = True

        if terminate_error is not None:
            for cleanup_error in local_errors:
                logger.warning(
                    "local sandbox cleanup also failed after termination error: %s",
                    cleanup_error,
                )
            raise terminate_error
        if local_errors:
            for cleanup_error in local_errors[1:]:
                logger.warning(
                    "additional sandbox cleanup failure: %s",
                    cleanup_error,
                )
            raise local_errors[0]

    @classmethod
    def delete(cls, name: str) -> None:
        """Terminate a named detached sandbox.

        Backend failures propagate so callers can retry unconfirmed deletion.

        Args:
            name: Name supplied when the detached sandbox was created.
        """

        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        load_backend().delete_named(name)

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(
        self,
        _exc_type: object,
        exc_value: BaseException | None,
        _traceback: object,
    ) -> None:
        try:
            self.kill()
        except Exception:
            if exc_value is None:
                raise
            logger.warning(
                "Failed to clean up sandbox %s while leaving an exceptional block",
                self._id,
                exc_info=True,
            )
