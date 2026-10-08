#!/usr/bin/env python3
"""Download and verify the ADX SDK candidate used by AKernel gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
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
    )
    if dependency.name != "adx-sandbox":
        raise ValueError(f"unexpected ADX SDK project: {dependency.name}")
    if len(dependency.sha256) != 64 or any(
        character not in "0123456789abcdef" for character in dependency.sha256
    ):
        raise ValueError("ADX SDK sha256 must be 64 lowercase hexadecimal digits")
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
    if filename != expected:
        raise ValueError(f"unexpected ADX SDK wheel filename: {filename}")

    output_dir.mkdir(parents=True, exist_ok=True)
    wheel = output_dir / filename
    if wheel.exists() and _sha256(wheel) == dependency.sha256:
        return wheel

    temporary = wheel.with_suffix(wheel.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with urllib.request.urlopen(dependency.url, timeout=60) as response:
            temporary.write_bytes(response.read())
        actual = _sha256(temporary)
        if actual != dependency.sha256:
            raise ValueError(
                "ADX SDK checksum mismatch: "
                f"expected {dependency.sha256}, received {actual}"
            )
        temporary.replace(wheel)
    finally:
        temporary.unlink(missing_ok=True)
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
