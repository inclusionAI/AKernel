# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Validate local reflink inputs and optional shared checkpoint mounts."""

from __future__ import annotations

import argparse
import fcntl
import re
import tempfile
import tomllib
from pathlib import Path


def shared_mount(root: Path, mountinfo: str) -> None:
    """Reject a missing, covered, or local substitute before creating files."""
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("shared mount root must be an absolute path without symlinks")
    mounts = []
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if not separator or len(fields) < 6 or len(filesystem) < 3:
            continue
        path = Path(
            re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        )
        mounts.append((path, fields[5], filesystem))
    candidates = [item for item in mounts if item[0] == root]
    if len(candidates) != 1:
        raise ValueError("shared storage mount is missing, covered, or ambiguous")
    _, options, filesystem = candidates[0]
    kind, source = filesystem[:2]
    afs = (kind == "fuse" or kind.startswith("fuse.")) and source == "afs-dfs"
    juicefs = (
        kind == "fuse.juicefs" and source.startswith("JuiceFS:") and len(source) > 8
    )
    if (
        "rw" not in options.split(",")
        or "rw" not in filesystem[2].split(",")
        or not (afs or juicefs)
    ):
        raise ValueError(
            "shared storage requires a writable AFS or JuiceFS mount at mount-root"
        )
    if any(path != root and root in path.parents for path, _, _ in mounts):
        raise ValueError("shared storage contains a covering mount")


def validate_runtime_storage(source: str, mount_root: Path) -> None:
    """Keep runtime state and private writes on their configured local paths."""
    config = tomllib.loads(source)
    runtime = config.get("plugin", {}).get("runtime", {})
    paths = [
        config.get("rootDir"),
        config.get("storeDir"),
        runtime.get("filestore_dir"),
    ]
    firecracker_storage = runtime.get("firecracker", {}).get("storage_dir")
    if firecracker_storage not in (None, ""):
        paths.append(firecracker_storage)
    for value in paths:
        if not isinstance(value, str):
            raise TypeError(
                "sandboxd runtime storage paths must be strings; "
                "rootDir, storeDir, and filestore_dir are required"
            )
        directory = Path(value)
        if not directory.is_absolute() or ".." in directory.parts:
            raise ValueError(
                "sandboxd runtime paths must be absolute without traversal"
            )
        for path in (directory, directory.resolve()):
            if path == mount_root or mount_root in path.parents:
                raise ValueError(
                    "sandboxd runtime storage must remain outside "
                    "the shared checkpoint mount"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mount-root", type=Path)
    parser.add_argument("--local-cache-root", type=Path)
    args = parser.parse_args()
    if args.mount_root is None and args.local_cache_root is None:
        parser.error("a shared mount or local object cache is required")
    if args.mount_root is not None:
        shared_mount(args.mount_root, Path("/proc/self/mountinfo").read_text())
    source = args.config.read_text()
    if args.mount_root is not None:
        validate_runtime_storage(source, args.mount_root)
    validate_reflink_storage(source, args.mount_root, args.local_cache_root)


def validate_reflink_storage(source: str, mount_root: Path | None, cache_root: Path | None) -> None:
    """Probe native FICLONE on the actual filestore/cache mounts before launch."""
    config = tomllib.loads(source)
    runtime = config.get("plugin", {}).get("runtime", {})
    mode = runtime.get("runsc", {}).get("checkpoint_mode", "full")
    if cache_root is not None:
        if not cache_root.is_absolute() or cache_root.resolve() != cache_root:
            raise ValueError("local checkpoint cache must be absolute without symlinks")
        if mount_root is not None and (cache_root == mount_root or mount_root in cache_root.parents or cache_root in mount_root.parents):
            raise ValueError("local checkpoint cache overlaps shared storage")
        paths = [config.get("rootDir"), config.get("storeDir"), runtime.get("filestore_dir"),
                 runtime.get("firecracker", {}).get("storage_dir")]
        for value in paths:
            if value:
                directory = Path(value).resolve()
                if cache_root == directory or cache_root in directory.parents or directory in cache_root.parents:
                    raise ValueError("local checkpoint cache overlaps private runtime storage")
    if mode != "reflink":
        return
    if cache_root is None:
        raise ValueError("runsc reflink requires local checkpoint materialization")
    if runtime.get("filestore_dir_size") not in (None, ""):
        raise ValueError("runsc reflink requires a native filestore without a loop filesystem")
    filestore = Path(runtime["filestore_dir"])
    if filestore.resolve() != filestore:
        raise ValueError("runsc filestore must not contain symlinks")
    for directory in (cache_root, filestore):
        directory.mkdir(parents=True, exist_ok=True)
    if cache_root.stat().st_dev != filestore.stat().st_dev:
        raise ValueError("checkpoint cache and filestore must be on the same filesystem")
    with tempfile.TemporaryFile(dir=filestore) as live, tempfile.TemporaryFile(dir=cache_root) as capture:
        live.write(b"AKernel native reflink preflight\n")
        live.flush()
        fcntl.ioctl(capture.fileno(), 0x40049409, live.fileno())  # Linux FICLONE


if __name__ == "__main__":
    main()
