# Copyright (c) 2026 Ant Group Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Explicit, isolated connection configuration for AKernel resource classes."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from ._addresses import _parse_endpoint, api_endpoint_from_env
from ._backends.base import BackendConfig
from ._backends.registry import new_backend
from ._tls import verification_from_env
from .sandbox import Sandbox


def _validate_address(address: str) -> None:
    if not isinstance(address, str) or not address.strip():
        raise ValueError("address must be a non-empty string")
    parsed = urlsplit(address if "://" in address else f"https://{address}")
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "address must be an HTTP(S) origin without credentials or a path"
        )


class Client:
    """Bind Sandbox operations to one cluster without mutating environment state.

    Explicit parameters win over AKERNEL_* defaults. gateway_address defaults
    to the API entry; specify it for deployments with a separate data gateway.
    Existing top-level Sandbox continues using its environment-based backend.
    """

    def __init__(
        self,
        server_address: str | None = None,
        token: str | None = None,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        gateway_address: str | None = None,
        verify_tls: bool | None = None,
    ) -> None:
        if server_address is not None and api_url is not None:
            raise ValueError("choose server_address or api_url")
        if token is not None and api_key is not None:
            raise ValueError("choose token or api_key")
        address = server_address if server_address is not None else api_url
        if address is not None:
            _validate_address(address)
        if gateway_address is not None:
            _validate_address(gateway_address)
        endpoint = (
            api_endpoint_from_env()
            if address is None
            else _parse_endpoint(
                address,
                default_port=80 if address.startswith("http://") else 443,
                default_scheme="https",
            )
        )
        credential = token if token is not None else api_key
        if credential is None:
            credential = os.environ.get("AKERNEL_TOKEN", "")
        if not isinstance(credential, str) or not credential.strip():
            raise ValueError("token/api_key must be a non-empty string")
        gateway = (
            endpoint
            if gateway_address is None
            else _parse_endpoint(
                gateway_address,
                default_port=443 if gateway_address.startswith("https://") else 80,
                default_scheme="http",
            )
        )
        verify = verification_from_env() if verify_tls is None else verify_tls
        if not isinstance(verify, bool):
            raise TypeError("verify_tls must be a boolean")
        config = BackendConfig(endpoint, gateway, credential.strip(), verify)
        backend = new_backend(config)

        class BoundSandbox(Sandbox):
            _bound_backend = backend
            _bound_gateway = gateway

        self.Sandbox = BoundSandbox
