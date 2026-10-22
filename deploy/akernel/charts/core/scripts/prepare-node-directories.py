#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Prepare operator-owned directories on an explicitly mounted node volume."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate configuration key: {key}")
        result[key] = value
    return result


def declarations(text):
    items = json.loads(text, object_pairs_hook=unique_object)
    if not isinstance(items, list):
        raise ValueError("directories must be a JSON array")
    paths = []
    result = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"path", "uid", "gid", "mode"}:
            raise ValueError("each directory requires exactly path, uid, gid and mode")
        path = item["path"]
        if (not isinstance(path, str) or not path or path.startswith("/")
                or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or any(c in {"", ".", ".."} for c in path.split("/"))):
            raise ValueError("directory path must be a nonempty relative POSIX path without traversal")
        if any(path == p or path.startswith(p + "/") or p.startswith(path + "/") for p in paths):
            raise ValueError(f"duplicate or overlapping directory path: {path}")
        for key in ("uid", "gid"):
            if type(item[key]) is not int or not 0 <= item[key] < 2**32 - 1:
                raise ValueError(f"{key} must be an integer in 0..4294967294")
        mode = item["mode"]
        if not isinstance(mode, str) or not re.fullmatch(r"[0-7]{4}", mode):
            raise ValueError("mode must be a four-digit octal string, for example 0770")
        paths.append(path)
        result.append(dict(item, mode=int(mode, 8)))
    return result


def is_volume_root(root):
    # Unlike os.path.ismount(), mountinfo also identifies same-filesystem binds.
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)

    with Path("/proc/self/mountinfo").open() as stream:
        return any(unescape(line.split()[4]) == root for line in stream)


def check_parent(fd, path):
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError(f"unsafe parent {path}: must be owned by the initializer UID and not group/other writable")


def check_leaf(fd, item):
    info = os.fstat(fd)
    actual = (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
    expected = (item["uid"], item["gid"], item["mode"])
    if actual != expected:
        raise ValueError(
            f"directory {item['path']} permissions mismatch: "
            f"expected uid={expected[0]} gid={expected[1]} mode={expected[2]:04o}; "
            f"found uid={actual[0]} gid={actual[1]} mode={actual[2]:04o}; existing data unchanged"
        )


def prepare_one(root_fd, item):
    fd = os.dup(root_fd)
    parts = item["path"].split("/")
    try:
        for index, part in enumerate(parts):
            parent = "/".join(parts[:index]) or "."
            check_parent(fd, parent)
            created = False
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            # Never follow a symlink, including an existing dangling symlink.
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            descend = False
            try:
                if index == len(parts) - 1:
                    if created:
                        os.fchown(child, item["uid"], item["gid"])
                        os.fchmod(child, item["mode"])
                    check_leaf(child, item)
                    return {"path": item["path"], "action": "created" if created else "exists",
                            "uid": item["uid"], "gid": item["gid"], "mode": f"{item['mode']:04o}"}
                if created:
                    os.fchmod(child, 0o755)
                check_parent(child, "/".join(parts[:index + 1]))
                os.close(fd)
                fd = child
                descend = True
            finally:
                if not descend:
                    os.close(child)
    finally:
        os.close(fd)


def prepare(root, items):
    if not os.path.isabs(root) or os.path.normpath(root) != root or root == "/":
        raise ValueError("root must be a normalized absolute mounted volume path other than /")
    # O_NOFOLLOW on the root alone does not protect symlinked ancestors.
    if os.path.realpath(root) != root:
        raise ValueError("volume root and its ancestors must not contain symlinks")
    if not is_volume_root(root):
        raise ValueError(f"volume root is not mounted: {root}; refusing to create directories on the container filesystem")
    root_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        check_parent(root_fd, root)
        return [prepare_one(root_fd, item) for item in items]
    finally:
        os.close(root_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="volumeMount root visible to the init container")
    parser.add_argument("--directories-json", required=True, help="operator-declared path/uid/gid/mode array")
    args = parser.parse_args(argv)
    try:
        items = declarations(args.directories_json)
        result = prepare(args.root, items)
    except (ValueError, OSError) as error:
        print(f"node directory preparation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "complete", "directories": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
