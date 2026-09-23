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

"""Import-time backend selection and lazy backend loading."""

from __future__ import annotations

import atexit
import importlib
import os
import threading
from importlib import metadata
from typing import Final

from .._addresses import api_endpoint_from_env, gateway_endpoint_from_env
from .base import Backend, BackendConfig
from .errors import BackendNotInstalledError, InvalidBackendError

ADX: Final = "adx"
SUPPORTED_BACKENDS: Final = (ADX,)

_MODULES: Final = {
    ADX: "akernel_sdk._backends.adx",
}
_DISTRIBUTIONS: Final = {
    ADX: "adx-sandbox",
}


def _is_installed(distribution: str) -> bool:
    try:
        metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return False
    return True


def _select_backend() -> str | None:
    configured = os.environ.get("AKERNEL_BACKEND", "").strip()
    # Existing deployments can retain their backend selector during migration.
    if configured in ("openyuanrong-sandbox", "openyuanrong-sdk"):
        configured = ADX
    if configured:
        if configured not in SUPPORTED_BACKENDS:
            choices = ", ".join(SUPPORTED_BACKENDS)
            raise InvalidBackendError(
                f"unsupported AKERNEL_BACKEND {configured!r}; expected one of: "
                f"{choices}"
            )
        return configured
    for candidate in SUPPORTED_BACKENDS:
        if _is_installed(_DISTRIBUTIONS[candidate]):
            return candidate
    return None


_selected_backend = _select_backend()
_loaded_backend: Backend | None = None
_load_lock = threading.Lock()


def selected_backend() -> str | None:
    """Return the backend identifier selected when this module was imported."""

    return _selected_backend


def _not_installed_error(backend: str | None) -> BackendNotInstalledError:
    if backend is None:
        return BackendNotInstalledError(
            "The default AKernel backend is not installed. Reinstall with:\n"
            "  pip install akernel-sdk"
        )
    command = "pip install akernel-sdk"
    return BackendNotInstalledError(
        f"Backend {backend!r} is not installed. Install it with:\n  {command}"
    )


def _config_from_env() -> BackendConfig:
    token = os.environ.get("AKERNEL_TOKEN", "").strip()
    if not token:
        raise RuntimeError("AKERNEL_TOKEN is not set")
    return BackendConfig(
        api_endpoint=api_endpoint_from_env(),
        gateway_endpoint=gateway_endpoint_from_env(),
        token=token,
    )


def load_backend() -> Backend:
    """Load and construct the selected backend once."""

    global _loaded_backend
    if _loaded_backend is not None:
        return _loaded_backend

    with _load_lock:
        if _loaded_backend is not None:
            return _loaded_backend
        backend_name = _selected_backend
        if backend_name is None or not _is_installed(_DISTRIBUTIONS[backend_name]):
            raise _not_installed_error(backend_name)
        module = importlib.import_module(_MODULES[backend_name])
        backend = module.create_backend(_config_from_env())
        _loaded_backend = backend
        atexit.register(backend.close)
        return backend
