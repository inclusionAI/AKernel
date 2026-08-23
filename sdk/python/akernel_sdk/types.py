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

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import idna

# Default yr.get() timeout in seconds.
# Ref: yr.common.constants.DEFAULT_GET_TIMEOUT
YR_GET_DEFAULT_TIMEOUT = 300

# Extra seconds added to a command timeout for RPC and serialization overhead.
YR_GET_TIMEOUT_BUFFER = 30


_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9_-]+$")
_NETWORK_ACTIONS = frozenset({"allow", "deny"})
_NETWORK_DIRECTIONS = frozenset({"ingress", "egress", "both"})
_NETWORK_PROTOCOLS = frozenset({"any", "tcp", "udp", "icmp"})
_TRAFFIC_POLICY_MODES = frozenset({"stateless", "stateful"})
_MAX_TRAFFIC_RULES = 256
# UINT32_MAX is reserved for FunctionSystem's control-plane and published-port
# rules, which must remain effective even when user traffic is default-deny.
_MAX_USER_RULE_PRIORITY = (1 << 32) - 2


def _normalize_domain_pattern(pattern: str, description: str) -> str:
    if not isinstance(pattern, str):
        raise TypeError(f"{description} patterns must be strings")
    value = pattern.strip().lower()
    if value.endswith("."):
        value = value[:-1]
    wildcard = value.startswith("*.")
    if wildcard:
        value = value[2:]
    if not value or "*" in value or "?" in value:
        raise ValueError(f"invalid {description} pattern: {pattern!r}")
    try:
        value = idna.encode(value, uts46=True).decode("ascii")
    except idna.IDNAError:
        # Preserve DNS-SD-compatible underscores in ASCII owner names while
        # normalizing ordinary international names to punycode.
        if any(ord(char) > 127 for char in value):
            raise ValueError(f"invalid {description} pattern: {pattern!r}") from None
    if len(value) > 253:
        raise ValueError(f"invalid {description} pattern: {pattern!r}")
    for label in value.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or _DNS_LABEL_PATTERN.fullmatch(label) is None
        ):
            raise ValueError(f"invalid {description} pattern: {pattern!r}")
    return f"*.{value}" if wildcard else value


def _normalize_dns_pattern(pattern: str) -> str:
    return _normalize_domain_pattern(pattern, "DNS blacklist")


def _normalize_choice(value: str, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip().lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}")
    return normalized


def _normalize_cidr(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cidr must be a non-empty IPv4 address or CIDR")
    try:
        network = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as error:
        raise ValueError(f"invalid IPv4 address or CIDR: {value!r}") from error
    if network.version != 4:
        raise ValueError(f"invalid IPv4 address or CIDR: {value!r}")
    return str(network)


@dataclass(frozen=True)
class PortRange:
    """Inclusive TCP or UDP port interval.

    Omit ``last`` to select one port. Both endpoints must be in 1..65535.
    """

    first: int
    last: int | None = None

    def __post_init__(self) -> None:
        last = self.first if self.last is None else self.last
        for name, value in (("first", self.first), ("last", last)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"port range {name} must be an integer")
            if value < 1 or value > 65535:
                raise ValueError(f"port range {name} must be in 1..65535")
        if self.first > last:
            raise ValueError("port range first must not exceed last")
        object.__setattr__(self, "last", last)

    def to_dict(self) -> dict[str, int]:
        """Return the JSON-compatible inclusive interval."""

        assert self.last is not None
        return {"first": self.first, "last": self.last}


def _normalize_port_range(value: object | None, name: str) -> PortRange | None:
    if value is None or isinstance(value, PortRange):
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, PortRange, or None")
    return PortRange(value)


@dataclass(frozen=True)
class NetworkRule:
    """One IPv4 traffic rule expressed from the sandbox's point of view.

    ``cidr`` and ``domain`` are mutually exclusive. Omitting both matches any
    peer address. A domain is valid only for egress and may be exact or start
    with ``*.``. ``port_range`` selects the peer port and
    ``sandbox_port_range`` selects the local sandbox port.
    """

    action: str = "allow"
    direction: str = "egress"
    protocol: str = "any"
    cidr: str | None = None
    domain: str | None = None
    port_range: PortRange | int | None = None
    sandbox_port_range: PortRange | int | None = None
    priority: int = 100

    def __post_init__(self) -> None:
        action = _normalize_choice(self.action, "action", _NETWORK_ACTIONS)
        direction = _normalize_choice(self.direction, "direction", _NETWORK_DIRECTIONS)
        protocol = _normalize_choice(self.protocol, "protocol", _NETWORK_PROTOCOLS)
        if self.cidr is not None and self.domain is not None:
            raise ValueError("cidr and domain cannot be combined in one rule")
        cidr = _normalize_cidr(self.cidr) if self.cidr is not None else None
        domain = (
            _normalize_domain_pattern(self.domain, "domain")
            if self.domain is not None
            else None
        )
        if domain is not None and direction != "egress":
            raise ValueError("domain rules are valid only for egress")
        port_range = _normalize_port_range(self.port_range, "port_range")
        sandbox_port_range = _normalize_port_range(
            self.sandbox_port_range, "sandbox_port_range"
        )
        if (port_range is not None or sandbox_port_range is not None) and (
            protocol not in ("tcp", "udp")
        ):
            raise ValueError("port ranges require protocol='tcp' or 'udp'")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise TypeError("priority must be an integer")
        if self.priority < 1 or self.priority > _MAX_USER_RULE_PRIORITY:
            raise ValueError(f"priority must be in 1..{_MAX_USER_RULE_PRIORITY}")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "cidr", cidr)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "port_range", port_range)
        object.__setattr__(self, "sandbox_port_range", sandbox_port_range)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible schema v2 rule."""

        value: dict[str, Any] = {
            "action": self.action,
            "direction": self.direction,
            "protocol": self.protocol,
            "priority": self.priority,
        }
        peer: dict[str, Any] = {}
        if self.cidr is not None:
            peer["cidr"] = self.cidr
        if self.domain is not None:
            peer["domain"] = self.domain
        if self.port_range is not None:
            assert isinstance(self.port_range, PortRange)
            peer["portRange"] = self.port_range.to_dict()
        if peer:
            value["peer"] = peer
        if self.sandbox_port_range is not None:
            assert isinstance(self.sandbox_port_range, PortRange)
            value["sandboxPortRange"] = self.sandbox_port_range.to_dict()
        return value


@dataclass(frozen=True)
class TrafficPolicy:
    """Generic IPv4 policy with independent direction defaults."""

    ingress_default_action: str = "allow"
    egress_default_action: str = "allow"
    rules: Sequence[NetworkRule] = ()
    mode: str = "stateful"

    def __post_init__(self) -> None:
        ingress = _normalize_choice(
            self.ingress_default_action,
            "ingress_default_action",
            _NETWORK_ACTIONS,
        )
        egress = _normalize_choice(
            self.egress_default_action,
            "egress_default_action",
            _NETWORK_ACTIONS,
        )
        mode = _normalize_choice(self.mode, "mode", _TRAFFIC_POLICY_MODES)
        if isinstance(self.rules, (str, bytes)):
            raise TypeError("rules must be a sequence of NetworkRule values")
        rules = tuple(self.rules)
        if len(rules) > _MAX_TRAFFIC_RULES:
            raise ValueError(
                f"traffic policies support at most {_MAX_TRAFFIC_RULES} rules"
            )
        if any(not isinstance(rule, NetworkRule) for rule in rules):
            raise TypeError("rules must contain only NetworkRule values")
        object.__setattr__(self, "ingress_default_action", ingress)
        object.__setattr__(self, "egress_default_action", egress)
        object.__setattr__(self, "rules", rules)
        object.__setattr__(self, "mode", mode)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible schema v2 traffic policy."""

        return {
            "ingressDefaultAction": self.ingress_default_action,
            "egressDefaultAction": self.egress_default_action,
            "mode": self.mode,
            "rules": [rule.to_dict() for rule in self.rules],
        }


@dataclass(frozen=True)
class DNSRule:
    """One exact or leading-wildcard DNS query rule."""

    pattern: str
    action: str = "deny"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "action",
            _normalize_choice(self.action, "action", _NETWORK_ACTIONS),
        )
        object.__setattr__(
            self, "pattern", _normalize_domain_pattern(self.pattern, "DNS")
        )

    def to_dict(self) -> dict[str, str]:
        """Return the JSON-compatible DNS rule."""

        return {"action": self.action, "pattern": self.pattern}


@dataclass(frozen=True)
class DNSPolicy:
    """DNS query policy evaluated by sandboxd's managed DNS proxy."""

    default_action: str = "allow"
    rules: Sequence[DNSRule] = ()

    def __post_init__(self) -> None:
        default = _normalize_choice(
            self.default_action, "default_action", _NETWORK_ACTIONS
        )
        if isinstance(self.rules, (str, bytes)):
            raise TypeError("rules must be a sequence of DNSRule values")
        rules = tuple(self.rules)
        if any(not isinstance(rule, DNSRule) for rule in rules):
            raise TypeError("rules must contain only DNSRule values")
        object.__setattr__(self, "default_action", default)
        object.__setattr__(self, "rules", rules)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible DNS policy."""

        return {
            "defaultAction": self.default_action,
            "rules": [rule.to_dict() for rule in self.rules],
        }


@dataclass(frozen=True)
class NetworkPolicy:
    """Creation-time network policy for an AKernel sandbox.

    Use :meth:`block` to deny new flows except the YuanRong control proxy and
    published sandbox ports used by direct filesystem I/O, reverse tunnels,
    and explicit port forwarding. Use :meth:`deny_dns` to reject conventional
    DNS queries matching exact names or leading ``*.`` suffix patterns.

    ``traffic`` and ``dns`` expose the generic schema v2 model. Legacy fields
    and schema v2 sections cannot be combined. Domains are normalized to
    lowercase IDNA ASCII without a trailing dot.
    """

    block_network: bool = False
    dns_blacklist: tuple[str, ...] = ()
    traffic: TrafficPolicy | None = None
    dns: DNSPolicy | None = None

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
        if (self.block_network or normalized) and (
            self.traffic is not None or self.dns is not None
        ):
            raise ValueError("legacy and schema v2 network policies cannot be combined")
        if self.traffic is not None and not isinstance(self.traffic, TrafficPolicy):
            raise TypeError("traffic must be a TrafficPolicy or None")
        if self.dns is not None and not isinstance(self.dns, DNSPolicy):
            raise TypeError("dns must be a DNSPolicy or None")
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

    @classmethod
    def allowlist(
        cls,
        rules: Sequence[NetworkRule],
        *,
        default_action: str = "deny",
        ingress_default_action: str = "allow",
        mode: str = "stateful",
    ) -> NetworkPolicy:
        """Allow selected egress rules and apply a default action to the rest."""

        normalized = tuple(rules)
        if not normalized:
            raise ValueError("allowlist requires at least one NetworkRule")
        return cls(
            traffic=TrafficPolicy(
                ingress_default_action=ingress_default_action,
                egress_default_action=default_action,
                rules=normalized,
                mode=mode,
            )
        )

    @property
    def is_empty(self) -> bool:
        """Whether this policy has no effect and should be omitted."""

        return (
            not self.block_network
            and not self.dns_blacklist
            and self.traffic is None
            and self.dns is None
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible public API representation."""

        value: dict[str, Any] = {}
        if self.block_network:
            value["blockNetwork"] = True
        if self.dns_blacklist:
            value["dnsBlacklist"] = list(self.dns_blacklist)
        if self.traffic is not None or self.dns is not None:
            value["schemaVersion"] = 2
            if self.traffic is not None:
                value["traffic"] = self.traffic.to_dict()
            if self.dns is not None:
                value["dns"] = self.dns.to_dict()
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
