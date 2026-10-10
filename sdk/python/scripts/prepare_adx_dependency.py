#!/usr/bin/env python3
"""Download and verify the ADX SDK candidate used by AKernel gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class Dependency:
    name: str
    version: str
    commit: str
    url: str
    sha256: str
    archive_member: str | None = None
    archive_sha256: str | None = None


def _required_string(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing non-empty {name!r} in ADX SDK lock")
    return value


def read_lock(path: Path) -> Dependency:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported ADX SDK lock schema")
    dependency = Dependency(
        name=_required_string(data, "name"),
        version=_required_string(data, "version"),
        commit=_required_string(data, "commit"),
        url=_required_string(data, "url"),
        sha256=_required_string(data, "sha256"),
        archive_member=data.get("archive_member"),
        archive_sha256=data.get("archive_sha256"),
    )
    if dependency.name != "adx-sandbox":
        raise ValueError(f"unexpected ADX SDK project: {dependency.name}")
    if (dependency.archive_member is None) != (dependency.archive_sha256 is None):
        raise ValueError("archive_member and archive_sha256 must be set together")
    for digest in (dependency.sha256, dependency.archive_sha256):
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(
                "ADX SDK sha256 must be 64 lowercase hexadecimal digits"
            )
    if dependency.archive_member is not None:
        expected = f"sdk/adx_sandbox-{dependency.version}-py3-none-any.whl"
        if dependency.archive_member != expected:
            raise ValueError(
                f"unexpected ADX SDK archive member: {dependency.archive_member}"
            )
    return dependency


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare(lock_path: Path, output_dir: Path) -> Path:
    dependency = read_lock(lock_path)
    filename = Path(urlparse(dependency.url).path).name
    expected = f"adx_sandbox-{dependency.version}-py3-none-any.whl"
    if dependency.archive_member is None and filename != expected:
        raise ValueError(f"unexpected ADX SDK wheel filename: {filename}")

    output_dir.mkdir(parents=True, exist_ok=True)
    wheel = output_dir / expected
    if wheel.exists() and _sha256(wheel) == dependency.sha256:
        return wheel

    temporary = wheel.with_suffix(wheel.suffix + ".tmp")
    download = wheel.with_suffix(wheel.suffix + ".download.tmp")
    temporary.unlink(missing_ok=True)
    download.unlink(missing_ok=True)
    try:
        with urllib.request.urlopen(dependency.url, timeout=60) as response:
            with download.open("wb") as stream:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    stream.write(chunk)
        if dependency.archive_member is None:
            download.replace(temporary)
        else:
            if _sha256(download) != dependency.archive_sha256:
                raise ValueError("ADX release archive checksum mismatch")
            with tarfile.open(download, "r:gz") as archive:
                members = [
                    member
                    for member in archive.getmembers()
                    if member.name.removeprefix("./") == dependency.archive_member
                ]
                if len(members) != 1 or not members[0].isfile():
                    raise ValueError("ADX SDK archive member must be one regular file")
                source = archive.extractfile(members[0])
                if source is None:
                    raise ValueError("ADX SDK archive member cannot be read")
                with source, temporary.open("wb") as stream:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        stream.write(chunk)
        actual = _sha256(temporary)
        if actual != dependency.sha256:
            raise ValueError(
                "ADX SDK checksum mismatch: "
                f"expected {dependency.sha256}, received {actual}"
            )
        temporary.replace(wheel)
    finally:
        temporary.unlink(missing_ok=True)
        download.unlink(missing_ok=True)
    return wheel


def main() -> None:
    project_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=project_dir / "adx-sdk.lock.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()

    wheel = prepare(args.lock, args.output_dir)
    if args.install:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", str(wheel)],
            check=True,
        )
    print(wheel)


if __name__ == "__main__":
    main()
