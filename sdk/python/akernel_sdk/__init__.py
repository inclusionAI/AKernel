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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._compose import ComposeOperationError, ComposeProject, ServiceHandle
    from ._docker_runtime import DockerRuntime
    from ._image_plan import ImageMountPlan
    from ._image_resolver import prepare_compose_images
    from ._service_process import ServiceProcessHandle

from ._backends.errors import (
    AuthenticationException,
    BackendNotInstalledError,
    BackendOperationError,
    CommandExitException,
    InvalidBackendError,
    NotFoundException,
    PermissionDeniedException,
    RateLimitException,
    SandboxNotFoundException,
    ServiceBusyException,
    TimeoutException,
    UnsupportedBackendFeatureError,
)
from ._backends.registry import selected_backend
from ._fork import ForkCleanupError
from ._sandboxes import SandboxPaginator, SandboxQuery, SandboxState
from ._snapshots import SnapshotPaginator
from .types import (
    CommandInfo,
    CommandResult,
    DNSPolicy,
    DNSRule,
    EntryInfo,
    HttpReverseTunnel,
    Mount,
    NetworkPolicy,
    NetworkRule,
    NodeInfo,
    PortRange,
    S3Config,
    SandboxInfo,
    SnapshotInfo,
    TrafficPolicy,
)

__all__ = [
    "Sandbox",
    "Client",
    "SandboxQuery",
    "SandboxState",
    "SandboxPaginator",
    "S3Config",
    "Mount",
    "NetworkPolicy",
    "NetworkRule",
    "PortRange",
    "TrafficPolicy",
    "DNSPolicy",
    "DNSRule",
    "HttpReverseTunnel",
    "CommandResult",
    "CommandInfo",
    "CommandHandle",
    "EntryInfo",
    "SandboxInfo",
    "SnapshotInfo",
    "SnapshotPaginator",
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
    "AuthenticationException",
    "PermissionDeniedException",
    "NotFoundException",
    "SandboxNotFoundException",
    "TimeoutException",
    "RateLimitException",
    "ServiceBusyException",
    "CommandExitException",
    "ForkCleanupError",
    "DockerContext",
    "DockerfileLaunch",
    "DockerContextEntry",
    "LocalDockerContext",
    "parse_dockerfile",
    "check_direct_launch",
    "apply_dockerfile",
    "ParsedDockerfile",
    "DockerfileApplyResult",
    "DockerfileCheckResult",
    "DockerfileBuildError",
    "DockerfileParseError",
    "BuildInstruction",
    "ComposeProject",
    "ComposeOperationError",
    "ServiceHandle",
    "ServiceProcessHandle",
    "DockerRuntime",
    "ImageMountPlan",
    "prepare_compose_images",
]

_LAZY_IMPORTS = {
    "ComposeProject": ("._compose", "ComposeProject"),
    "ComposeOperationError": ("._compose", "ComposeOperationError"),
    "ServiceHandle": ("._compose", "ServiceHandle"),
    "ServiceProcessHandle": ("._service_process", "ServiceProcessHandle"),
    "DockerRuntime": ("._docker_runtime", "DockerRuntime"),
    "ImageMountPlan": ("._image_plan", "ImageMountPlan"),
    "prepare_compose_images": ("._image_resolver", "prepare_compose_images"),
    "Client": ("._client", "Client"),
    "Sandbox": (".sandbox", "Sandbox"),
    "CommandHandle": (".commands", "CommandHandle"),
    "Pty": (".pty", "Pty"),
    "PtySession": (".pty", "PtySession"),
    "PtyError": (".pty", "PtyError"),
    "resources": ("._resources", "resources"),
    "DockerContext": ("._dockercontext", "DockerContext"),
    "DockerfileLaunch": ("._dockerfile_launch", "DockerfileLaunch"),
    "DockerContextEntry": ("._dockercontext", "DockerContextEntry"),
    "LocalDockerContext": ("._dockercontext", "LocalDockerContext"),
    "parse_dockerfile": ("._dockerfile", "parse_dockerfile"),
    "check_direct_launch": ("._dockerfile", "check_direct_launch"),
    "ParsedDockerfile": ("._dockerfile", "ParsedDockerfile"),
    "DockerfileCheckResult": ("._dockerfile", "DockerfileCheckResult"),
    "DockerfileParseError": ("._dockerfile", "DockerfileParseError"),
    "BuildInstruction": ("._dockerfile", "BuildInstruction"),
    "apply_dockerfile": ("._dockerfile_runner", "apply_dockerfile"),
    "DockerfileApplyResult": ("._dockerfile_runner", "DockerfileApplyResult"),
    "DockerfileBuildError": ("._dockerfile", "DockerfileBuildError"),
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
