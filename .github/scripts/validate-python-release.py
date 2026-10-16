#!/usr/bin/env python3
"""Validate Python release metadata and remote branch ancestry (Python 3.12)."""

import argparse
import re
import subprocess
import tomllib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--prerelease", choices=("true", "false"), required=True)
    args = parser.parse_args()

    match = re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?P<rc>rc[0-9]+)?", args.tag)
    if match is None:
        parser.error("Release tag must use vX.Y.Z or vX.Y.ZrcN")

    is_rc = match.group("rc") is not None
    if is_rc != (args.prerelease == "true"):
        parser.error(
            "RC releases must set prerelease=true; stable releases must set false"
        )

    with Path("sdk/python/pyproject.toml").open("rb") as stream:
        package_version = tomllib.load(stream)["project"]["version"]
    if args.tag[1:] != package_version:
        parser.error(f"Tag {args.tag} does not match package version {package_version}")

    branch = "develop" if is_rc else "main"
    remote_ref = f"refs/remotes/origin/{branch}"
    subprocess.run(
        [
            "git",
            "fetch",
            "--no-tags",
            "--recurse-submodules=no",
            "origin",
            f"+refs/heads/{branch}:{remote_ref}",
        ],
        check=True,
    )
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", remote_ref], check=False
    )
    if result.returncode != 0:
        parser.exit(1, f"Release tag must point to a commit on {branch}\n")

    print(f"Validated {args.tag} on origin/{branch} (prerelease={args.prerelease})")


if __name__ == "__main__":
    main()
