#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Prepare a verified ADX overlay on an immutable, qualified AKernel image.

This integration build preserves the base runtime and Ubuntu/tini rootfs. It
does not change the default release-archive pins or publish an ADX release.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import uuid


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timestamp", type=int, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9./_:-]+@sha256:[0-9a-f]{64}", args.base_image):
        parser.error("base-image must be an immutable repository@sha256 reference")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit) or args.timestamp <= 0:
        parser.error("a full ADX commit and positive reproducible timestamp are required")
    package = args.package_dir.resolve(strict=True)
    manifest = json.loads((package / "manifest.json").read_text())
    if manifest["commit"] != args.commit or manifest["dirty"]:
        raise ValueError("package must come from the requested clean ADX commit")
    run("python3", str(package / "lib/package_manifest.py"), "--host-architecture", str(package))
    if manifest["target"] != "x86_64-unknown-linux-gnu":
        raise ValueError("this overlay currently qualifies Linux amd64 only")
    # EROFS 1.7 prints its version for -V but exits 1; validate the reported
    # version instead of treating that historical CLI behavior as a failure.
    help_result = subprocess.run(["mkfs.erofs", "-V"], check=False, capture_output=True, text=True)
    version = re.search(r"mkfs\.erofs\s+\d[^\n]*", help_result.stdout + help_result.stderr)
    if not version:
        raise ValueError("cannot identify the EROFS builder version")
    args.output.mkdir(parents=True, exist_ok=False)
    output = args.output.resolve()
    container = None
    container_name = "akernel-adx-overlay-" + uuid.uuid4().hex[:12]
    try:
        container = run("docker", "create", "--name", container_name,
                        "--label", "io.akernel.task=adx-overlay", "--network", "none",
                        "--entrypoint", "/bin/true", args.base_image)
        (output / "temporary-container.json").write_text(json.dumps({
            "name": container_name, "id": container, "started": False,
        }) + "\n")
        run("docker", "cp", f"{container}:/opt/akernel/runtime/akernel-runtime-rootfs.img", str(output / "base-rootfs.img"))
    finally:
        if container:
            run("docker", "rm", container)
    rootfs = output / "rootfs"
    run("fsck.erofs", f"--extract={rootfs}", "--preserve", str(output / "base-rootfs.img"))
    execd = rootfs / "usr/local/bin/adx-execd"
    original_execd = sha(execd)
    shutil.copyfile(package / "runtime/adx-execd", execd)
    execd.chmod(0o755)
    # All other files, links, ownership and modes come from the original image.
    # EROFS image metadata is deterministic for these exact qualified inputs.
    runtime = output / "akernel-runtime-rootfs.img"
    image_uuid = uuid.uuid5(uuid.NAMESPACE_URL, args.base_image + args.commit)
    run("mkfs.erofs", "-T", str(args.timestamp), "-U", str(image_uuid), "-E", "noinline_data", str(runtime), str(rootfs))
    run("fsck.erofs", str(runtime))
    shutil.copytree(package, output / "adx-release", symlinks=True)
    provenance = {
        "schema_version": 1,
        "kind": "qualified-adx-overlay",
        "base_image": args.base_image,
        "adx_commit": args.commit,
        "adx_manifest_sha256": sha(package / "manifest.json"),
        "base_rootfs_sha256": sha(output / "base-rootfs.img"),
        "base_execd_sha256": original_execd,
        "execd_sha256": sha(execd),
        "runtime_rootfs_sha256": sha(runtime),
        "source_date_epoch": args.timestamp,
        "mkfs_erofs_version": version.group(),
        "preserved_runtime_dependencies": True,
    }
    (output / "adx-overlay.json").write_text(json.dumps(provenance, indent=2) + "\n")
    (output / "Dockerfile").write_text(
        f"FROM {args.base_image}\n"
        f"COPY adx-release/ /opt/adx/releases/{args.commit}/\n"
        "RUN rm -rf /opt/adx/current && "
        f"ln -s releases/{args.commit} /opt/adx/current && "
        "ln -sfn /opt/adx/current/bin/adxctl /usr/local/bin/adxctl && "
        "python3 /opt/adx/current/lib/package_manifest.py --host-architecture /opt/adx/current\n"
        "COPY akernel-runtime-rootfs.img /opt/akernel/runtime/akernel-runtime-rootfs.img\n"
        "COPY adx-overlay.json /usr/local/share/akernel/adx-overlay.json\n"
        f'LABEL io.akernel.adx.revision="{args.commit}"\n'
        f'LABEL io.akernel.base.digest="{args.base_image.split("@")[1]}"\n'
    )
    (output / ".dockerignore").write_text("*\n!Dockerfile\n!adx-release/**\n!akernel-runtime-rootfs.img\n!adx-overlay.json\n")
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    main()
