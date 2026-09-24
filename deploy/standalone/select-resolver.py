#!/usr/bin/env python3

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
"""Select a host resolver usable from a standalone runc network namespace.

Automatic selection is deliberately disabled for systemd-resolved hosts: its
flat resolv.conf cannot represent per-link domain routing or VPN DNS policy.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import tempfile


MAX_RESOLVER_BYTES = 1024 * 1024
SYSTEMD_RESOLVED_FILE = Path("/run/systemd/resolve/resolv.conf")
DEFAULT_SOURCES = (Path("/etc/resolv.conf"),)


def read_usable_resolver(path: Path) -> bytes:
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"resolver source is not an absolute regular file: {path}")
    if path.stat().st_size > MAX_RESOLVER_BYTES:
        raise ValueError(f"resolver source is too large: {path}")
    content = path.read_bytes()
    servers = []
    for line in content.decode("utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if not fields or fields[0] != "nameserver":
            continue
        if len(fields) != 2:
            raise ValueError(f"invalid nameserver entry in {path}")
        address = ipaddress.ip_address(fields[1])
        mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
        if (
            address.is_loopback
            or address.is_unspecified
            or address.is_link_local
            or (
                mapped is not None
                and (mapped.is_loopback or mapped.is_unspecified or mapped.is_link_local)
            )
            or (isinstance(address, ipaddress.IPv6Address) and address.scope_id)
        ):
            raise ValueError(f"resolver is local to another network namespace: {address}")
        servers.append(address)
    if not servers:
        raise ValueError(f"resolver source has no nameserver: {path}")
    return content


def select_resolver(
    explicit_source: Path | None,
    defaults=DEFAULT_SOURCES,
    systemd_resolved_file: Path = SYSTEMD_RESOLVED_FILE,
) -> bytes:
    if explicit_source is not None:
        return read_usable_resolver(explicit_source)
    if systemd_resolved_file.exists():
        raise ValueError(
            "systemd-resolved may use per-link or split DNS; automatic resolver "
            "selection cannot preserve that policy. Set AKERNEL_RUNC_RESOLV_CONF "
            "to an approved resolver file reachable from runc sandboxes"
        )
    failures = []
    for source in defaults:
        try:
            return read_usable_resolver(source)
        except (OSError, UnicodeError, ValueError) as exc:
            failures.append(f"{source}: {exc}")
    raise ValueError(
        "no usable host resolver; set AKERNEL_RUNC_RESOLV_CONF to an absolute "
        "file containing reachable non-loopback nameservers (" + "; ".join(failures) + ")"
    )


def write_resolver(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="explicit host resolver file")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        content = select_resolver(args.source)
        write_resolver(args.output, content)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f"standalone runc resolver: {exc}\n")


if __name__ == "__main__":
    main()
