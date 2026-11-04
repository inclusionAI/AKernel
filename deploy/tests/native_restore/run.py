#!/usr/bin/env python3
"""Run native Restore faults against an empty dedicated Sandboxd (same filesystem).

Uses a temporary copy of the pinned ADX source and its lockfile. The live
submodule is never modified. This tests the production Controller and adapter
with an in-process authority fixture, not the deployed Coordinator/API.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--rootfs", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--compile-only", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    source = repo / "src/agent-dx"
    pin = subprocess.check_output(["git", "rev-parse", "HEAD:src/agent-dx"], cwd=repo, text=True).strip()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="adx-source-", dir=args.state_dir) as temporary:
        root = Path(temporary)
        archive = root / "source.tar"
        subprocess.run(["git", "archive", pin, "-o", str(archive)], cwd=source, check=True)
        with tarfile.open(archive) as tar:
            tar.extractall(root / "source", filter="data")
        fixture = root / "source/platform/adxlet/tests/akernel_unknown_restore.rs"
        shutil.copyfile(Path(__file__).with_name("unknown_restore.rs"), fixture)
        env = dict(os.environ, ADX_SANDBOXD_TEST_SOCKET=str(args.socket.resolve()),
                   ADX_SANDBOXD_TEST_ROOTFS=str(args.rootfs.resolve()),
                   ADX_NATIVE_STATE_DIR=str(args.state_dir.resolve()))
        command = ["cargo", "test", "--locked", "-p", "adxlet", "--test", "akernel_unknown_restore", "-j", str(args.jobs)]
        command += ["--no-run"] if args.compile_only else ["--", "--ignored", "--nocapture"]
        print(f"ADX source={pin}; native fixture requires Sandboxd/runtime binaries pinned by this AKernel revision", flush=True)
        subprocess.run(command, cwd=root / "source", env=env, check=True)


if __name__ == "__main__":
    main()
