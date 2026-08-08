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

"""Public API for the AKernel Python SDK."""

import importlib

from ._backends.errors import (
    BackendNotInstalledError,
    BackendOperationError,
    InvalidBackendError,
    UnsupportedBackendFeatureError,
)
from ._backends.registry import selected_backend
from .types import (
    CommandInfo,
    CommandResult,
    EntryInfo,
    HttpReverseTunnel,
    Mount,
    NetworkPolicy,
    NodeInfo,
    S3Config,
    SandboxInfo,
)

__all__ = [
    "Sandbox",
    "S3Config",
    "Mount",
    "NetworkPolicy",
    "HttpReverseTunnel",
    "CommandResult",
    "CommandInfo",
    "CommandHandle",
    "EntryInfo",
    "SandboxInfo",
    "NodeInfo",
    "Pty",
    "PtySession",
    "PtyError",
    "resources",
    "get_backend",
    "InvalidBackendError",
    "BackendNotInstalledError",
    "UnsupportedBackendFeatureError",
    "BackendOperationError",
]

_LAZY_IMPORTS = {
    "Sandbox": (".sandbox", "Sandbox"),
    "CommandHandle": (".commands", "CommandHandle"),
    "Pty": (".pty", "Pty"),
    "PtySession": (".pty", "PtySession"),
    "PtyError": (".pty", "PtyError"),
    "resources": ("._resources", "resources"),
}


def get_backend() -> str | None:
    """Return the backend selected during package import without loading it."""

    return selected_backend()


def __getattr__(name: str) -> object:
    """Load backend-dependent public objects only when they are requested."""

    target = _LAZY_IMPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    module = importlib.import_module(module_name, __package__)
    value = getattr(module, attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()).union(__all__))
