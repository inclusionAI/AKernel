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

"""Internal openYuanrong adapter used by the public AKernel SDK."""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from typing import Any

import yr
from yr.runtime_holder import global_runtime
from yr.sandbox.sandbox import _sanitize_instance_id

from ._addresses import api_endpoint_from_env, gateway_endpoint_from_env
from ._instance import _SandboxInstance
from ._resource_api import parse_resource_nodes, query_resource_view
from ._sandbox_resources import (
    normalize_xpu,
    storage_bytes,
    validate_storage_mb,
    xpu_custom_resource,
)
from .types import HttpReverseTunnel, Mount, NodeInfo, S3Config

logger = logging.getLogger(__name__)

_NAMESPACE = "akernel"
_DEFAULT_LOCAL_ROOTFS = "/home/yuanrong/yr-runtime-rootfs.img"
_initialized = False
_init_lock = threading.Lock()


def ensure_initialized() -> None:
    """Initialize the openYuanrong client once for the current process."""

    global _initialized
    if _initialized:
        return

    with _init_lock:
        if _initialized:
            return

        token = os.environ.get("AKERNEL_TOKEN", "").strip()
        if not token:
            raise RuntimeError("AKERNEL_TOKEN is not set")

        endpoint = api_endpoint_from_env()
        # Keep openYuanrong tuning inside its backend initialization boundary.
        os.environ.setdefault("YR_HTTP_CONNECTION_NUM", "2")
        os.environ.setdefault("YR_SERVER_ADDRESS", endpoint.authority())
        gateway_override = os.environ.get("AKERNEL_GATEWAY_ADDRESS", "").strip()
        if gateway_override and not os.environ.get("YR_GATEWAY_ADDRESS", "").strip():
            os.environ["YR_GATEWAY_ADDRESS"] = gateway_override

        yr.init(
            yr.Config(
                server_address=endpoint.authority(),
                auth_token=token,
                enable_tls=endpoint.use_tls,
                server_name="akernel-sdk",
                in_cluster=False,
                http_ioc_threads_num=8,
                bypass_datasystem=True,
            )
        )
        atexit.register(yr.finalize)
        _initialized = True


def _validate_positive_int(name: str, value: int, *, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        comparator = "non-negative" if allow_zero else "greater than 0"
        raise ValueError(f"{name} must be {comparator}")


def _rootfs_json(
    *, image: str | None, rootfs: S3Config | None, runtime: str
) -> str | None:
    if image is not None:
        return json.dumps(
            {
                "runtime": runtime,
                "type": "image",
                "readonly": False,
                "imageurl": image,
            }
        )
    if rootfs is not None:
        return json.dumps(
            {
                "runtime": runtime,
                "type": "s3",
                "readonly": False,
                "storageInfo": rootfs.to_dict(),
            }
        )
    if runtime != "runsc":
        return json.dumps(
            {
                "runtime": runtime,
                "type": "local",
                "readonly": False,
                "path": _DEFAULT_LOCAL_ROOTFS,
            }
        )
    return None


def build_options(
    *,
    image: str | None,
    rootfs: S3Config | None,
    runtime: str,
    cpu: int,
    memory: int,
    cpu_limit: int,
    mem_limit: int,
    idle_timeout: int,
    schedule_timeout: int,
    env: Mapping[str, str] | None,
    name: str | None,
    port_forwardings: Sequence[int],
    mounts: Sequence[Mount],
    reverse_tunnel: HttpReverseTunnel | None,
    detached: bool,
    node_id: str | None,
    xpu: str | None,
    storage_mb: int | None,
) -> Any:
    """Translate the stable SDK configuration to openYuanrong options."""

    _validate_positive_int("cpu", cpu)
    _validate_positive_int("memory", memory)
    _validate_positive_int("cpu_limit", cpu_limit, allow_zero=True)
    _validate_positive_int("mem_limit", mem_limit, allow_zero=True)
    _validate_positive_int("idle_timeout", idle_timeout, allow_zero=True)
    if schedule_timeout != -1:
        _validate_positive_int("schedule_timeout", schedule_timeout, allow_zero=True)
    if cpu_limit and cpu_limit < cpu:
        raise ValueError("cpu_limit must be 0 or greater than or equal to cpu")
    if mem_limit and mem_limit < memory:
        raise ValueError("mem_limit must be 0 or greater than or equal to memory")
    normalized_xpu = normalize_xpu(xpu)
    validate_storage_mb(storage_mb)
    if normalized_xpu is not None and runtime != "runsc":
        raise ValueError("xpu is currently supported only by runsc")
    if storage_mb is not None and runtime != "runsc":
        raise ValueError("storage_mb is currently supported only by runsc")

    options = yr.InvokeOptions()
    # A Sandbox is driven by one sequential SDK client. Disabling ordered RPC
    # execution prevents a missing sequence number from stalling later calls.
    options.need_order = False
    options.idle_timeout = idle_timeout
    options.schedule_timeout_ms = (
        -1 if schedule_timeout == -1 else schedule_timeout * 1000
    )
    options.cpu = cpu
    options.memory = memory
    options.cpu_limit = cpu_limit
    options.mem_limit = mem_limit
    options.namespace = _NAMESPACE

    rootfs_json = _rootfs_json(image=image, rootfs=rootfs, runtime=runtime)
    if rootfs_json is not None:
        options.custom_extensions["rootfs"] = rootfs_json
    if detached:
        options.custom_extensions["lifecycle"] = "detached"
    if env:
        options.env_vars = dict(env)
    if name:
        options.name = name
    if mounts:
        options.custom_extensions["mounts"] = json.dumps(
            [mount.to_dict() for mount in mounts]
        )
    if normalized_xpu is not None:
        resource_name, count = xpu_custom_resource(normalized_xpu)
        options.custom_resources[resource_name] = count
    if storage_mb is not None:
        options.custom_resources["storage"] = storage_bytes(storage_mb)

    forwarded = list(port_forwardings)
    if reverse_tunnel is not None:
        forwarded.append(reverse_tunnel.reverse_port)
    if forwarded:
        from yr.config import PortForwarding

        options.port_forwardings = [
            PortForwarding(port=port, protocol="TCP") for port in forwarded
        ]

    if node_id is not None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("node_id must be a non-empty string")
        operator = yr.LabelOperator(
            yr.OperatorType.LABEL_IN,
            "NODE_ID",
            [node_id.strip()],
        )
        options.schedule_affinities = [
            yr.Affinity(
                yr.AffinityKind.RESOURCE,
                yr.AffinityType.REQUIRED,
                [operator],
            )
        ]

    return options


def create_instance(options: Any, *, cwd: str | None) -> Any:
    instance_class: Any = _SandboxInstance
    handle = instance_class.options(options).invoke(cwd=cwd)
    yr.get(handle.ping.invoke())
    return handle


def real_instance_id(handle: Any) -> str:
    return global_runtime.get_runtime().get_real_instance_id(handle.instance_id)


def terminate_instance(handle: Any) -> None:
    handle.terminate()


def delete_named_instance(name: str) -> None:
    ensure_initialized()
    handle = yr.get_instance(name, namespace=_NAMESPACE)
    handle.terminate(is_sync=True)


def ping_instance(handle: Any) -> bool:
    try:
        yr.get(handle.ping.invoke())
        return True
    except Exception:
        return False


def instance_state(handle: Any) -> str:
    result = yr.get(handle.get_info.invoke())
    return result.get("state", "unknown")


def start_reverse_tunnel(
    handle: Any,
    tunnel: HttpReverseTunnel,
    *,
    name: str | None,
) -> Any:
    """Start both ends of an HTTP reverse tunnel and return its client."""

    yr.get(
        handle.start_tunnel_server.invoke(
            tunnel.reverse_port,
            tunnel.listen_port,
        )
    )
    gateway = gateway_endpoint_from_env()
    instance_id = real_instance_id(handle)
    safe_id = _sanitize_instance_id(instance_id)
    websocket_url = (
        f"{gateway.websocket_scheme}://"
        f"{gateway.authority(omit_default_port=True)}/"
        f"{safe_id}/{tunnel.reverse_port}"
    )

    from yr.sandbox.tunnel_client import TunnelClient

    client = TunnelClient(tunnel.target)
    logger.info(
        "Starting reverse tunnel: sandbox_id=%s name=%s url=%s target=%s",
        safe_id,
        name or "",
        websocket_url,
        tunnel.target,
    )
    if client.start(websocket_url, timeout=float(tunnel.connect_timeout)):
        return client

    client.stop()
    raise RuntimeError(
        "reverse tunnel connection timed out after "
        f"{float(tunnel.connect_timeout):.1f}s: sandbox_id={safe_id} "
        f"name={name or ''} tunnel_url={websocket_url}"
    )


def _to_float_mapping(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float] = {}
    for key, raw in value.items():
        scalar = raw
        if isinstance(raw, Mapping):
            scalar = raw.get("value", raw.get("scalar", raw))
            if isinstance(scalar, Mapping):
                scalar = scalar.get("value", 0)
        try:
            result[str(key)] = float(scalar)
        except (TypeError, ValueError):
            continue
    return result


def _to_node_info(value: Any) -> NodeInfo:
    if not isinstance(value, Mapping):
        raise TypeError(f"unexpected openYuanrong resource entry: {value!r}")
    labels = value.get("labels", value.get("nodeLabels", {}))
    return NodeInfo(
        id=str(value.get("id", "")),
        status=int(value.get("status", 0)),
        capacity=_to_float_mapping(value.get("capacity", {})),
        allocatable=_to_float_mapping(value.get("allocatable", {})),
        labels=dict(labels) if isinstance(labels, Mapping) else {},
    )


def resources() -> list[NodeInfo]:
    """Return cluster resources through stable AKernel value types."""

    ensure_initialized()
    return parse_resource_nodes(query_resource_view())
