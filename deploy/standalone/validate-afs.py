#!/usr/bin/env python3
"""Require an existing AFS DFS mount before binding shared checkpoints."""
import argparse
from pathlib import Path
import re
import subprocess


def validate(path: str, prefix: str) -> str:
    if not re.fullmatch(r"(/[A-Za-z0-9_.-]+)+", path) or ".." in Path(path).parts:
        raise ValueError("AKERNEL_AFS_MOUNT must be a safe absolute directory")
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("AKERNEL_AFS_MOUNT must be a directory")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
        raise ValueError("AKERNEL_AFS_CHECKPOINT_PREFIX must use safe relative components")
    info = subprocess.run(
        ["findmnt", "-rn", "-M", str(resolved), "-o", "SOURCE,FSTYPE,OPTIONS"],
        check=True, text=True, capture_output=True,
    ).stdout.split()
    if (len(info) != 3 or info[0] != "afs-dfs" or not info[1].startswith("fuse")
            or "rw" not in info[2].split(",")):
        raise ValueError("AKERNEL_AFS_MOUNT must be a writable mounted AFS DFS filesystem")
    return str(resolved)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--prefix", default="akernel/checkpoints")
    args = parser.parse_args()
    print(validate(args.path, args.prefix))


if __name__ == "__main__":
    main()
