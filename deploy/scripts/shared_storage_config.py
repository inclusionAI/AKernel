#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Render shared-storage settings for a guided Terraform deployment profile."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import PurePosixPath


def settings(
    enabled: bool, storage_class: str, host: str, claim: str,
    cache_mib: int = 4096, cache_host_path: str = "", cache_large_write: bool = True,
) -> dict:
    if cache_mib < 0:
        raise ValueError("JuiceFS cache size must be non-negative")
    if cache_host_path and not re.fullmatch(r"(/[A-Za-z0-9_-]+)+", cache_host_path):
        raise ValueError("JuiceFS cache path must be absolute without traversal")
    if host and claim:
        raise ValueError("select one checkpoint host path or RWX claim")
    if enabled and (host or claim):
        raise ValueError("managed JuiceFS cannot use an external checkpoint source")
    if enabled and not storage_class:
        raise ValueError("managed JuiceFS requires JUICEFS_STORAGE_CLASS")
    if host:
        path = PurePosixPath(host)
        if not path.is_absolute() or host == "/" or ".." in path.parts:
            raise ValueError("checkpoint host path must be absolute without traversal")
    if claim and not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", claim):
        raise ValueError("checkpoint claim must be a Kubernetes resource name")
    return {
        "checkpoint_storage": {
            "kind": "distributed_fs" if host or claim else "local",
            "host_path": host,
            "existing_claim": claim,
        },
        "juicefs": {
            "enabled": enabled,
            "ceph_storage_class": storage_class,
            "postgres_storage_class": storage_class,
            "cache_mib": cache_mib,
            "cache_host_path": cache_host_path,
            "cache_large_write": cache_large_write,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--juicefs-enabled", choices=("true", "false"), default="false")
    parser.add_argument("--juicefs-storage-class", default="")
    parser.add_argument("--juicefs-cache-mib", type=int, default=4096)
    parser.add_argument("--juicefs-cache-host-path", default="")
    parser.add_argument("--juicefs-cache-large-write", choices=("true", "false"), default="true")
    parser.add_argument("--checkpoint-host-path", default="")
    parser.add_argument("--checkpoint-existing-claim", default="")
    args = parser.parse_args()
    try:
        config = settings(
            args.juicefs_enabled == "true",
            args.juicefs_storage_class,
            args.checkpoint_host_path,
            args.checkpoint_existing_claim,
            args.juicefs_cache_mib,
            args.juicefs_cache_host_path,
            args.juicefs_cache_large_write == "true",
        )
    except ValueError as error:
        parser.error(str(error))
    for name, value in config.items():
        print(f"{name} = {json.dumps(value)}")


if __name__ == "__main__":
    main()
