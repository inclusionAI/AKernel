#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Reuse the envd-enabled runtime from a verified ADX Execd component archive."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_guest(image, architecture, execd_digest, envd_digest):
    elf_check = Path(__file__).with_name("verify-elf-arch.sh")
    with tempfile.TemporaryDirectory(prefix="akernel-e2b-rootfs-") as temporary:
        root = Path(temporary).resolve()
        subprocess.run(["fsck.erofs", f"--extract={root}", str(image)], check=True, timeout=300)
        for name, digest in (("adx-execd", execd_digest), ("envd", envd_digest)):
            path = root / "usr/local/bin" / name
            if path.resolve() != path or not path.is_file() or not path.stat().st_mode & stat.S_IXUSR:
                raise ValueError(f"E2B guest binary is missing or invalid: {name}")
            subprocess.run(["sh", str(elf_check), str(path), architecture], check=True)
            if sha256(path) != digest:
                raise ValueError(f"E2B guest binary checksum mismatch: {name}")


def prepare(source, output, architecture, *, verify_guest_binaries=False):
    machines = {"amd64": "x86_64", "arm64": "aarch64"}
    if architecture not in machines:
        raise ValueError("unsupported runtime architecture")
    required = {"adx-execd", "adx-runtime-rootfs.img", "envd-manifest.json", "envd-LICENSE"}
    for name in required | {"manifest.json"}:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"E2B requires a complete envd-enabled ADX Execd archive: {name}")
    manifest = json.loads((source / "manifest.json").read_text())
    if (manifest.get("schema_version") != 1 or manifest.get("component") != "execd"
            or manifest.get("target") != f"{machines[architecture]}-unknown-linux-gnu"
            or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("commit", ""))
            or set(manifest.get("files", {})) != required):
        raise ValueError("ADX Execd component identity or file set is invalid")
    for name in required:
        if sha256(source / name) != manifest["files"][name]:
            raise ValueError(f"ADX Execd component checksum mismatch: {name}")
    envd = json.loads((source / "envd-manifest.json").read_text())
    if (envd.get("architecture") != machines[architecture]
            or not re.fullmatch(r"[0-9a-f]{64}", envd.get("sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", envd.get("revision", ""))
            or not isinstance(envd.get("version"), str) or not envd["version"].strip()):
        raise ValueError("envd provenance or architecture is invalid")
    if verify_guest_binaries:
        verify_guest(source / "adx-runtime-rootfs.img", architecture,
                     manifest["files"]["adx-execd"], envd["sha256"])
    output.mkdir(parents=True, exist_ok=False)
    metadata = output / "runtime-metadata"
    (metadata / "envd").mkdir(parents=True)
    shutil.copyfile(source / "adx-runtime-rootfs.img", output / "akernel-runtime-rootfs.img")
    shutil.copyfile(source / "manifest.json", metadata / "adx-component-manifest.json")
    shutil.copyfile(source / "envd-manifest.json", metadata / "envd/manifest.json")
    shutil.copyfile(source / "envd-LICENSE", metadata / "envd/LICENSE")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("architecture", choices=("amd64", "arm64"))
    parser.add_argument("--verify-guest", action="store_true")
    args = parser.parse_args()
    prepare(args.source, args.output, args.architecture, verify_guest_binaries=args.verify_guest)
