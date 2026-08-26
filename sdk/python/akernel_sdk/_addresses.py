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

"""Address normalization helpers for the AKernel Python SDK.

The public SDK accepts a compact ``AKERNEL_SERVER_ADDRESS`` value:

* ``host:port``: shared-port mode.  Frontend API and exec WebSocket use the
  explicit port with TLS; public port-forward URLs use it with plain HTTP.
* ``host:port``: shared-port mode.  Frontend API, exec WebSocket, and public
  port-forward URLs all use the explicit port with TLS by default.

``AKERNEL_GATEWAY_ADDRESS`` remains an explicit override for standalone or
custom network topologies.  When it is set without a scheme, it is treated as a
plain HTTP/WebSocket gateway.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_API_PORT = 443
DEFAULT_PUBLIC_PORT = 80


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    scheme: str
    explicit_port: bool

    @property
    def use_tls(self) -> bool:
        return self.scheme in ("https", "wss")

    @property
    def websocket_scheme(self) -> str:
        return "wss" if self.use_tls else "ws"

    def authority(self, *, omit_default_port: bool = False) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if omit_default_port and (
            (self.scheme in ("http", "ws") and self.port == 80)
            or (self.scheme in ("https", "wss") and self.port == 443)
        ):
            return host
        return f"{host}:{self.port}"

    def base_url(self, *, omit_default_port: bool = True) -> str:
        return f"{self.scheme}://{self.authority(omit_default_port=omit_default_port)}"


def _parse_endpoint(
    raw: str,
    *,
    default_port: int,
    default_scheme: str,
) -> Endpoint:
    value = raw.strip()
    if not value:
        raise RuntimeError("address is empty")

    parsed = urlparse(value if "://" in value else f"//{value}")
    if not parsed.hostname:
        raise RuntimeError(f"invalid address: {raw!r}")

    try:
        port = parsed.port
    except ValueError as e:
        raise RuntimeError(f"invalid address port: {raw!r}") from e

    explicit_port = port is not None
    return Endpoint(
        host=parsed.hostname,
        port=port or default_port,
        scheme=parsed.scheme or default_scheme,
        explicit_port=explicit_port,
    )


def _server_address_raw() -> str:
    raw = os.environ.get("AKERNEL_SERVER_ADDRESS", "").strip()
    if not raw:
        raise RuntimeError("AKERNEL_SERVER_ADDRESS is not set")
    return raw


def _gateway_override_raw() -> str:
    return (
        os.environ.get("AKERNEL_GATEWAY_ADDRESS", "").strip()
        or os.environ.get("YR_GATEWAY_ADDRESS", "").strip()
    )


def api_endpoint_from_env() -> Endpoint:
    """Return the frontend API endpoint derived from AKERNEL_SERVER_ADDRESS."""
    return _parse_endpoint(
        _server_address_raw(),
        default_port=DEFAULT_API_PORT,
        default_scheme="https",
    )


def gateway_endpoint_from_env() -> Endpoint:
    """Return the public port-forwarding gateway endpoint.

    An explicit gateway override is parsed as plain HTTP by default because
    standalone exposes Traefik's web entrypoint without TLS.  Without an
    explicit gateway, host-only server addresses use public 80, while
    host:port server addresses reuse the API port with plain HTTP.
    """
    override = _gateway_override_raw()
    if override:
        return _parse_endpoint(
            override,
            default_port=DEFAULT_PUBLIC_PORT,
            default_scheme="http",
        )

    server = api_endpoint_from_env()
    if server.explicit_port:
        return Endpoint(
            host=server.host,
            port=server.port,
            scheme="http",
            explicit_port=True,
        )
    return Endpoint(
        host=server.host,
        port=DEFAULT_PUBLIC_PORT,
        scheme="http",
        explicit_port=False,
    )


def exec_endpoint_from_env() -> Endpoint:
    """Return the endpoint used by the exec WebSocket (/terminal/ws).

    File copy and PTY traffic terminate on the frontend alongside the API, so
    they always follow AKERNEL_SERVER_ADDRESS.
    """
    return api_endpoint_from_env()
