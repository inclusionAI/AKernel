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

"""Stable public value types used by the AKernel Python SDK."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

# Default yr.get() timeout in seconds.
# Ref: yr.common.constants.DEFAULT_GET_TIMEOUT
YR_GET_DEFAULT_TIMEOUT = 300

# Extra seconds added to a command timeout for RPC and serialization overhead.
YR_GET_TIMEOUT_BUFFER = 30


# DNS blacklist patterns use exact ASCII names or a single leading ``*.``.
# Each label is 1-63 characters from this set; a hyphen cannot be an endpoint.
_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9_-]+$")


def _normalize_dns_pattern(pattern: str) -> str:
    if not isinstance(pattern, str):
        raise TypeError("dns blacklist patterns must be strings")
    value = pattern.strip().lower().rstrip(".")
    wildcard = value.startswith("*.")
    if wildcard:
        value = value[2:]
    if not value or "*" in value or "?" in value or len(value) > 253:
        raise ValueError(f"invalid DNS blacklist pattern: {pattern!r}")
    for label in value.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or _DNS_LABEL_PATTERN.fullmatch(label) is None
        ):
            raise ValueError(f"invalid DNS blacklist pattern: {pattern!r}")
    return f"*.{value}" if wildcard else value


@dataclass(frozen=True)
class NetworkPolicy:
    """Creation-time network policy for an AKernel sandbox.

    Use :meth:`block` to deny new flows except the YuanRong control proxy and
    published sandbox ports used by direct filesystem I/O, reverse tunnels,
    and explicit port forwarding. Use :meth:`deny_dns` to reject conventional
    DNS queries matching exact names or leading ``*.`` suffix patterns.

    DNS patterns are ASCII names up to 253 characters, split into labels of
    1-63 lowercase letters, digits, underscores, or hyphens. Hyphens cannot
    start or end a label. A wildcard is accepted only as the leading ``*.``;
    use ASCII punycode for internationalized names. Input is normalized to
    lowercase and trailing dots are removed.
    """

    block_network: bool = False
    dns_blacklist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.block_network, bool):
            raise TypeError("block_network must be a boolean")
        if isinstance(self.dns_blacklist, (str, bytes)):
            raise TypeError("dns_blacklist must be a sequence of patterns")
        normalized = tuple(
            dict.fromkeys(_normalize_dns_pattern(item) for item in self.dns_blacklist)
        )
        if self.block_network and normalized:
            raise ValueError("block_network and dns_blacklist cannot be combined")
        object.__setattr__(self, "dns_blacklist", normalized)

    @classmethod
    def block(cls) -> NetworkPolicy:
        """Deny new flows except control and published sandbox-port routes."""

        return cls(block_network=True)

    @classmethod
    def deny_dns(cls, *patterns: str) -> NetworkPolicy:
        """Deny DNS queries matching exact names or leading ``*.`` patterns."""

        if not patterns:
            raise ValueError("deny_dns requires at least one domain pattern")
        return cls(dns_blacklist=patterns)

    @property
    def is_empty(self) -> bool:
        """Whether this policy has no effect and should be omitted."""

        return not self.block_network and not self.dns_blacklist

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible public API representation."""

        value: dict[str, Any] = {}
        if self.block_network:
            value["blockNetwork"] = True
        if self.dns_blacklist:
            value["dnsBlacklist"] = list(self.dns_blacklist)
        return value


@dataclass(frozen=True)
class EntryInfo:
    """Metadata for a filesystem entry inside a sandbox."""

    name: str
    path: str
    type: str
    size: int
    permissions: str
    modified_time: float


@dataclass(frozen=True)
class CommandResult:
    """Result returned by a completed command."""

    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class CommandInfo:
    """Read-only snapshot of a process tracked by a sandbox."""

    pid: int
    command: str
    running: bool


@dataclass(frozen=True)
class SandboxInfo:
    """Current state and requested resources for a sandbox."""

    id: str
    state: str
    cpu: int | None
    memory: int | None
    image: str | None
    xpu: str | None = None
    storage_mb: int | None = None


@dataclass(frozen=True)
class CheckpointInfo:
    """Stable identity of a reusable sandbox checkpoint."""

    id: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str):
            raise TypeError("id must be a string")
        normalized = self.id.strip()
        if not normalized:
            raise ValueError("id must be a non-empty string")
        object.__setattr__(self, "id", normalized)


@dataclass(frozen=True)
class NodeInfo:
    """Capacity, allocation, and labels advertised by an AKernel node."""

    id: str
    status: int
    capacity: dict[str, float]
    allocatable: dict[str, float]
    labels: dict[str, Any]


@dataclass(frozen=True)
class S3Config:
    """Location and optional credentials for an S3-compatible object."""

    endpoint: str
    bucket: str
    object: str
    access_key: str | None = None
    secret_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for field_name in ("endpoint", "bucket", "object"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible representation expected by AKernel."""

        value: dict[str, Any] = {
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "object": self.object,
        }
        if self.access_key is not None:
            value["accessKey"] = self.access_key
        if self.secret_key is not None:
            value["secretKey"] = self.secret_key
        return value


@dataclass(frozen=True)
class Mount:
    """Read-only OCI image or S3 object mounted inside a sandbox.

    Exactly one of ``image_url`` and ``s3_config`` must be supplied. ``type``
    selects a read-only bind mount or an EROFS image mount.
    """

    target: str
    image_url: str | None = None
    s3_config: S3Config | None = None
    type: str = "bind"

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not self.target.startswith("/"):
            raise ValueError("target must be an absolute sandbox path")
        source_count = sum(
            source is not None for source in (self.image_url, self.s3_config)
        )
        if source_count != 1:
            raise ValueError("exactly one of image_url and s3_config must be specified")
        if self.image_url is not None and (
            not isinstance(self.image_url, str) or not self.image_url.strip()
        ):
            raise ValueError("image_url must be a non-empty string")
        if self.type not in ("bind", "erofs"):
            raise ValueError("type must be 'bind' or 'erofs'")

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible representation expected by AKernel."""

        value: dict[str, Any] = {
            "type": self.type,
            "target": self.target,
            "options": ["ro"],
        }
        if self.image_url is not None:
            value["image_url"] = self.image_url
        if self.s3_config is not None:
            value["s3_config"] = self.s3_config.to_dict()
        return value


@dataclass(frozen=True)
class HttpReverseTunnel:
    """Expose an SDK-side HTTP or HTTPS service inside a sandbox.

    ``reverse_port`` carries the WebSocket tunnel through the AKernel gateway.
    Sandbox applications call :attr:`url`, which points at the loopback HTTP
    listener on ``listen_port``.
    """

    target: str
    reverse_port: int = 8765
    listen_port: int = 8766
    connect_timeout: float = 60.0

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not self.target.strip():
            raise ValueError("target must be a non-empty HTTP or HTTPS address")
        parsed = urlparse(self.target if "://" in self.target else f"//{self.target}")
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            raise ValueError("target scheme must be http or https")
        if not parsed.hostname:
            raise ValueError("target must contain a hostname")
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("target contains an invalid port") from error
        for name in ("reverse_port", "listen_port"):
            port = getattr(self, name)
            if isinstance(port, bool) or not isinstance(port, int):
                raise TypeError(f"{name} must be an integer")
            if not 1 <= port <= 65535:
                raise ValueError(f"{name} must be between 1 and 65535")
        if self.reverse_port == self.listen_port:
            raise ValueError("reverse_port and listen_port must be different")
        if isinstance(self.connect_timeout, bool):
            raise TypeError("connect_timeout must be a number")
        try:
            timeout = float(self.connect_timeout)
        except (TypeError, ValueError) as error:
            raise TypeError("connect_timeout must be a number") from error
        if timeout <= 0:
            raise ValueError("connect_timeout must be greater than 0")

    @property
    def url(self) -> str:
        """Return the loopback URL used by applications in the sandbox."""

        return f"http://127.0.0.1:{self.listen_port}"
