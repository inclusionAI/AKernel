#!/usr/bin/env python3
"""Apply PVM, telemetry and mounted checkpoint options to ADX deployments."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import subprocess

import yaml


def expand_selector(value: str) -> str:
    """Match ADX's single-pass ${NAME:-default}/$$ discriminator expansion.

    Only profile and role selection use this result. Keep source expressions
    in the generated deployment so ADX expands all configuration exactly once.
    Environment values and defaults are inserted verbatim, never recursively.
    """
    if not isinstance(value, str):
        raise ValueError("deployment selector must be a string")
    result = []
    cursor = 0
    while cursor < len(value):
        start = value.find("$", cursor)
        if start < 0:
            result.append(value[cursor:])
            break
        result.append(value[cursor:start])
        if value.startswith("$$", start):
            result.append("$")
            cursor = start + 2
        elif value.startswith("${", start):
            end = value.find("}", start + 2)
            if end < 0:
                raise ValueError("unterminated environment variable reference")
            name, has_default, default = value[start + 2:end].partition(":-")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid environment variable reference: {name}")
            replacement = os.environ.get(name)
            if not replacement:
                if not has_default:
                    raise ValueError(f"environment variable is not set: {name}")
                replacement = default
            result.append(replacement)
            cursor = end + 1
        else:
            result.append("$")
            cursor = start + 1
    return "".join(result)


def configure(
    source: Path, destination: Path, mount: Path | None, prefix: str, *, pvm: bool = False,
) -> None:
    cfg = yaml.safe_load(source.read_text())
    if not isinstance(cfg, dict):
        raise ValueError("deployment must be a mapping")
    profile_roles = {
        "standalone": ("coordinator", "apiserver", "adxlet", "ingress"),
        "standalone-external-redis": ("coordinator", "apiserver", "adxlet", "ingress"),
        "coordinator": ("coordinator",),
        "node": ("adxlet",),
        "ingress-api": ("apiserver", "ingress"),
    }
    if "profile" in cfg:
        profile = expand_selector(cfg["profile"])
        if profile not in profile_roles:
            raise ValueError(f"unsupported deployment profile: {cfg['profile']}")
        roles = profile_roles[profile]

        def services(role):
            if role not in roles:
                return []
            return [cfg.setdefault("service_overrides", {}).setdefault(role, {})]
    else:
        # Explicit deployments use their actual Service objects. Profile-only
        # keys and absent roles would be rejected by ADX's strict loader.
        def services(role):
            return [service for service in cfg["services"]
                    if expand_selector(service["role"]) == role]

    def config(service):
        if service.get("config") is None:
            service["config"] = {}
        return service["config"]

    if pvm:
        # Nested PVM cleanup needs a longer RPC budget; guest boot stays unchanged.
        for role in ("coordinator", "adxlet"):
            for service in services(role):
                config(service)["rpc_timeout_seconds"] = 90
    if mount is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
            raise ValueError("checkpoint prefix must contain safe relative components")
        if str(mount) != "/mnt/akernel-shared":
            raise ValueError("unexpected shared checkpoint mount")
        info = subprocess.run(
            ["findmnt", "-rn", "-M", str(mount), "-o", "SOURCE,FSTYPE,OPTIONS"],
            check=True, text=True, capture_output=True,
        ).stdout.split()
        if (len(info) != 3 or info[0] != "afs-dfs" or not info[1].startswith("fuse")
                or "rw" not in info[2].split(",")):
            raise ValueError("shared checkpoint path must be a writable AFS DFS mount")
        storage = {"kind": "distributed_fs", "alias": "afs-checkpoints",
                   "mount_root": str(mount), "prefix": prefix}
        for service in services("coordinator"):
            config(service)["checkpoint_reclaimers"] = [storage]
        for service in services("adxlet"):
            node = config(service)
            node["checkpoint_storage"] = storage
            node["checkpoint_dir"] = None
    if os.environ.get("ENABLE_TRACE", "false") == "true":
        for role in ("coordinator", "apiserver", "adxlet", "ingress"):
            for service in services(role):
                service.setdefault("env", {}).update({
                    "ADX_TRACE_ENABLED": "true", "ADX_TRACE_SAMPLE_RATIO": "0.1",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
                })
        for service in services("adxlet"):
            config(service).setdefault("execd_env", {}).update({
                "ADX_TRACE_ENABLED": "true", "ADX_TRACE_SAMPLE_RATIO": "0.1",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://10.88.0.1:4318",
            })
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(cfg, sort_keys=False))
    destination.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--mount", type=Path, default=None)
    parser.add_argument("--prefix", default="akernel/checkpoints")
    parser.add_argument("--pvm", action="store_true", help="use the PVM cleanup RPC budget")
    args = parser.parse_args()
    configure(args.source, args.destination, args.mount, args.prefix, pvm=args.pvm)


if __name__ == "__main__":
    main()
