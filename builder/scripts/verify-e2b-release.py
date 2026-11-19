#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Require an intact E2B runtime paired with the ADX platform release."""
import argparse
import hashlib
import json
from pathlib import Path


def verify(component_path, release_path, *, runtime=None, architecture=None):
    if runtime is not None and any(path.is_symlink() for path in (
            component_path, component_path.parent, component_path.parent / "envd")):
        raise ValueError("E2B runtime metadata must use regular files and directories")
    component = json.loads(component_path.read_text())
    release = json.loads(release_path.read_text())
    if any(not component.get(key) or component[key] != release.get(key)
           for key in ("commit", "target")):
        raise ValueError("E2B runtime and ADX platform must come from the same build and architecture")
    release_files = release.get("files", {})
    if component["files"]["adx-execd"] != release_files.get("runtime/adx-execd"):
        raise ValueError("E2B runtime and platform Execd checksums differ")
    if runtime is not None:
        machine = {"amd64": "x86_64", "arm64": "aarch64"}[architecture]
        if component["target"] != f"{machine}-unknown-linux-gnu":
            raise ValueError("E2B runtime does not match the node architecture")
        for name, path, release_name in (
            ("adx-runtime-rootfs.img", runtime / "akernel-runtime-rootfs.img", "runtime/adx-runtime-rootfs.img"),
            ("envd-manifest.json", component_path.parent / "envd/manifest.json", "runtime/envd/manifest.json"),
            ("envd-LICENSE", component_path.parent / "envd/LICENSE", "runtime/envd/LICENSE"),
        ):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"E2B runtime artifact is missing or invalid: {name}")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            checksum = digest.hexdigest()
            if checksum != component["files"].get(name):
                raise ValueError(f"E2B runtime artifact checksum mismatch: {name}")
            if release_name in release_files and checksum != release_files[release_name]:
                raise ValueError(f"E2B platform release checksum mismatch: {release_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component_manifest", type=Path)
    parser.add_argument("release_manifest", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--architecture", choices=("amd64", "arm64"))
    args = parser.parse_args()
    if (args.runtime is None) != (args.architecture is None):
        parser.error("--runtime and --architecture must be specified together")
    verify(args.component_manifest, args.release_manifest,
           runtime=args.runtime, architecture=args.architecture)
