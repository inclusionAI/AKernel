"""Remove abandoned repository metadata from a fenced Distribution filesystem."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from registry import ABANDONED_BATCH_SIZE, TEMPORARY_REPOSITORY

LAYOUT = ("docker", "registry", "v2", "repositories")
REPOSITORY_ENTRIES = {"_uploads", "_manifests", "_layers"}


def directory(path: Path, device: int) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_dev != device:
        raise ValueError(f"Unexpected Registry directory: {path}")
    return True


def has_current_tag(repository: Path, device: int) -> bool:
    manifests = repository / "_manifests"
    tags = manifests / "tags"
    if not directory(manifests, device) or not directory(tags, device):
        return False
    for tag in tags.iterdir():
        current = tag / "current"
        if not directory(tag, device) or not directory(current, device):
            continue
        try:
            (current / "link").lstat()
        except FileNotFoundError:
            continue
        return True
    return False


def verify_metadata(path: Path, device: int):
    for entry in path.iterdir():
        info = entry.lstat()
        if info.st_dev != device:
            raise ValueError(f"Registry metadata crosses a filesystem: {entry}")
        if stat.S_ISDIR(info.st_mode):
            verify_metadata(entry, device)
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Unexpected Registry metadata: {entry}")


def remove_repository(root: Path, name: str, device: int) -> bool:
    path = root
    for component in (*LAYOUT, *name.split("/")):
        path /= component
        if not directory(path, device):
            return True
    if has_current_tag(path, device) or any(entry.name not in REPOSITORY_ENTRIES for entry in path.iterdir()):
        return False
    verify_metadata(path, device)
    shutil.rmtree(path)
    try:
        path.parent.rmdir()
    except OSError as exc:
        if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
            raise
    return True


def cleanup(root: Path, plan: dict) -> dict[str, list[str]]:
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("Expected the absolute Distribution filesystem rootdirectory")
    names = plan.get("abandoned_repositories") if isinstance(plan, dict) else None
    if not isinstance(names, list) or len(names) > ABANDONED_BATCH_SIZE or any(
        not isinstance(name, str) or not TEMPORARY_REPOSITORY.fullmatch(name) for name in names
    ) or len(set(names)) != len(names):
        raise ValueError("Invalid abandoned repository plan")

    device = root.stat().st_dev
    cleaned = []
    for name in names:
        try:
            if remove_repository(root, name, device):
                cleaned.append(name)
        except ValueError as exc:
            print(f"Deferred abandoned repository {name} ({exc})", file=sys.stderr)
    return {"cleaned_repositories": cleaned}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootdirectory", type=Path, required=True)
    parser.add_argument("--writers-fenced", action="store_true", required=True)
    args = parser.parse_args()
    raw = sys.stdin.read(32769)
    if len(raw) > 32768:
        parser.error("Abandoned repository plan exceeds the input limit")
    try:
        result = cleanup(args.rootdirectory, json.loads(raw))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
