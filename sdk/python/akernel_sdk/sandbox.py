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

import json
import ssl
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from . import _openyuanrong
from ._addresses import Endpoint, api_endpoint_from_env, gateway_endpoint_from_env
from ._sandbox_resources import normalize_xpu, validate_storage_mb
from .commands import Commands
from .filesystem import Filesystem
from .pty import Pty
from .types import HttpReverseTunnel, Mount, S3Config, SandboxInfo

_SUPPORTED_RUNTIMES = ("runsc", "kata")
_traefik_internal_ip_cache: str | None = None


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


def _get_traefik_internal_ip(gateway: Endpoint) -> tuple[str, int]:
    """Resolve Traefik's direct address for ``internal=True`` URLs."""

    global _traefik_internal_ip_cache
    if _traefik_internal_ip_cache is not None:
        return _traefik_internal_ip_cache, gateway.port

    server = api_endpoint_from_env()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(
        f"{server.base_url()}/internal-stats",
        timeout=5,
        context=context,
    ) as response:
        payload = json.loads(response.read())
    pod_ip = payload.get("pod_ip")
    if not isinstance(pod_ip, str) or not pod_ip:
        raise RuntimeError("/internal-stats response does not contain pod_ip")
    _traefik_internal_ip_cache = pod_ip
    return pod_ip, gateway.port


class Sandbox:
    """A remote AKernel sandbox.

    The public API is backend-neutral. AKernel supports the gVisor ``runsc``
    runtime and Kata Containers on KVM-capable nodes.
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
        xpu: str | None = None,
        storage_mb: int | None = None,
    ) -> None:
        """Create and wait for a sandbox to become ready.

        Args:
            image: OCI image used as the sandbox root filesystem.
            rootfs: S3-compatible EROFS root filesystem configuration.
            runtime: Sandbox runtime name: ``runsc`` or ``kata``.
            cpu: Requested CPU in millicores.
            memory: Requested memory in MiB.
            cpu_limit: CPU limit in millicores, or zero to follow ``cpu``.
            mem_limit: Memory limit in MiB, or zero to follow ``memory``.
            idle_timeout: Seconds before an idle sandbox is reclaimed.
            schedule_timeout: Scheduling timeout in seconds, or ``-1`` for no
                deadline.
            env: Environment variables applied to the sandbox process.
            name: Optional stable name for a detached sandbox.
            cwd: Initial working directory inside the sandbox.
            port_forwardings: Sandbox TCP ports exposed through the gateway.
            mounts: Additional read-only OCI or S3-backed mounts.
            reverse_tunnel: SDK-side HTTP service exposed inside the sandbox.
            detached: Keep the sandbox alive when this client closes.
            node_id: Require placement on a specific AKernel node.
            xpu: Experimental whole-device accelerator request in
                ``type:model:count`` format. Currently only exact-model NVIDIA
                GPU requests with the ``runsc`` runtime are supported.
            storage_mb: Experimental writable root filesystem quota in MiB.
                When omitted, the configured default is used. Explicit quotas
                currently require the ``runsc`` runtime.

        Raises:
            TypeError: An argument has an invalid type.
            ValueError: Arguments are invalid or mutually incompatible.
            RuntimeError: The backend cannot create or initialize the sandbox.
        """

        if image is not None and (not isinstance(image, str) or not image.strip()):
            raise ValueError("image must be a non-empty string")
        if rootfs is not None and not isinstance(rootfs, S3Config):
            raise TypeError("rootfs must be an S3Config")
        if image is not None and rootfs is not None:
            raise ValueError("image and rootfs are mutually exclusive")
        if runtime not in _SUPPORTED_RUNTIMES:
            raise ValueError(
                f"unsupported runtime {runtime!r}; "
                f"supported runtimes: {', '.join(_SUPPORTED_RUNTIMES)}"
            )
        normalized_xpu = normalize_xpu(xpu)
        validate_storage_mb(storage_mb)
        if normalized_xpu is not None and runtime != "runsc":
            raise ValueError("xpu is currently supported only by runsc")
        if storage_mb is not None and runtime != "runsc":
            raise ValueError("storage_mb is currently supported only by runsc")
        if env is not None:
            if not isinstance(env, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            ):
                raise TypeError("env must map strings to strings")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError("name must be a non-empty string")
        if cwd is not None and not isinstance(cwd, str):
            raise TypeError("cwd must be a string")
        if not isinstance(detached, bool):
            raise TypeError("detached must be a boolean")
        if reverse_tunnel is not None and not isinstance(
            reverse_tunnel, HttpReverseTunnel
        ):
            raise TypeError("reverse_tunnel must be an HttpReverseTunnel")

        ports = _normalize_ports(port_forwardings)
        mount_list = _normalize_mounts(mounts)
        if reverse_tunnel is not None:
            conflicts = set(ports).intersection(
                {reverse_tunnel.reverse_port, reverse_tunnel.listen_port}
            )
            if conflicts:
                rendered = ", ".join(str(port) for port in sorted(conflicts))
                raise ValueError(
                    f"reverse tunnel ports conflict with port_forwardings: {rendered}"
                )

        self._instance: Any = None
        self._tunnel_client: Any = None
        self._pty: Pty | None = None
        self._closed = False
        self._detached = detached
        self._reverse_tunnel = reverse_tunnel
        self._forwarded_ports = set(ports)
        self._image = image
        self._cpu = cpu
        self._memory = memory
        self._xpu = normalized_xpu
        self._storage_mb = storage_mb
        self._id = ""

        _openyuanrong.ensure_initialized()
        options = _openyuanrong.build_options(
            image=image,
            rootfs=rootfs,
            runtime=runtime,
            cpu=cpu,
            memory=memory,
            cpu_limit=cpu_limit,
            mem_limit=mem_limit,
            idle_timeout=idle_timeout,
            schedule_timeout=schedule_timeout,
            env=env,
            name=name,
            port_forwardings=ports,
            mounts=mount_list,
            reverse_tunnel=reverse_tunnel,
            detached=detached,
            node_id=node_id,
            xpu=normalized_xpu,
            storage_mb=storage_mb,
        )
        self._instance = _openyuanrong.create_instance(options, cwd=cwd)
        self._id = _openyuanrong.real_instance_id(self._instance)

        try:
            if reverse_tunnel is not None:
                self._tunnel_client = _openyuanrong.start_reverse_tunnel(
                    self._instance,
                    reverse_tunnel,
                    name=name,
                )
        except Exception:
            _openyuanrong.terminate_instance(self._instance)
            self._closed = True
            raise

        self._files = Filesystem(self._instance, instance_id=self._id)
        self._commands = Commands(self._instance)
        self._pty = Pty(self._id)

    @property
    def files(self) -> Filesystem:
        """Filesystem operations for this sandbox."""

        return self._files

    @property
    def commands(self) -> Commands:
        """Command execution and process management for this sandbox."""

        return self._commands

    @property
    def pty(self) -> Pty:
        """Factory for interactive pseudo-terminal sessions."""

        assert self._pty is not None
        return self._pty

    @property
    def id(self) -> str:
        """Physical sandbox ID, matching the value displayed by ``ak list``."""

        return self._id

    @property
    def reverse_tunnel(self) -> HttpReverseTunnel | None:
        """Configured reverse tunnel, or ``None`` when it is disabled."""

        return self._reverse_tunnel

    def get_port_url(self, port: int, *, internal: bool = False) -> str:
        """Return the gateway URL for a declared sandbox port.

        Args:
            port: Port included in ``port_forwardings`` at sandbox creation.
            internal: Resolve Traefik's directly reachable address instead of
                the public gateway address.

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
        if internal:
            pod_ip, gateway_port = _get_traefik_internal_ip(gateway)
            direct = Endpoint(
                host=pod_ip,
                port=gateway_port,
                scheme=gateway.scheme,
                explicit_port=True,
            )
            return f"{direct.base_url()}/{self.id}/{port}"
        return f"{gateway.base_url()}/{self.id}/{port}"

    def is_running(self) -> bool:
        """Return whether the sandbox currently responds to a health check."""

        if self._closed or self._instance is None:
            return False
        return _openyuanrong.ping_instance(self._instance)

    def get_info(self) -> SandboxInfo:
        """Return current sandbox state and requested resources."""

        return SandboxInfo(
            id=self.id,
            state=_openyuanrong.instance_state(self._instance),
            cpu=self._cpu,
            memory=self._memory,
            image=self._image,
            xpu=self._xpu,
            storage_mb=self._storage_mb,
        )

    def kill(self) -> None:
        """Release client resources and terminate a non-detached sandbox."""

        if self._closed:
            return
        self._closed = True

        if self._tunnel_client is not None:
            self._tunnel_client.stop()
            self._tunnel_client = None
        if self._pty is not None:
            self._pty._close()
        if not self._detached and self._instance is not None:
            _openyuanrong.terminate_instance(self._instance)

    @classmethod
    def delete(cls, name: str) -> None:
        """Terminate a named detached sandbox.

        Args:
            name: Name supplied when the detached sandbox was created.
        """

        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        _openyuanrong.delete_named_instance(name)

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.kill()

    def __del__(self) -> None:
        try:
            self.kill()
        except Exception:
            pass
