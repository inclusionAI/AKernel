# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

"""One-shot, sandbox/PID-scoped runc delete fault for exclusive Linux fixtures."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path


def process_identity(pid):
    try:
        start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        return [pid, start, Path("/proc/sys/kernel/random/boot_id").read_text().strip()]
    except (OSError, IndexError):
        return None


def install(binary, directory):
    """Atomically install a default-pass launcher; never remove the live path."""
    binary, directory = Path(binary), Path(directory)
    if binary.is_symlink() or not binary.is_file():
        raise RuntimeError("fixture runc must be a regular executable")
    directory.mkdir(mode=0o700)
    shutil.copy2(binary, directory / "real-runc")
    shutil.copy2(__file__, directory / "helper.py")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    launcher = binary.with_name(binary.name + "." + uuid.uuid4().hex + ".tmp")
    launcher.write_text(
        f"#!{sys.executable}\nimport runpy,sys\n"
        f"sys.argv=[{str(directory / 'helper.py')!r},"
        f"{str(directory)!r},*sys.argv[1:]]\n"
        f"runpy.run_path({str(directory / 'helper.py')!r},run_name='__main__')\n"
    )
    launcher.chmod(0o755)
    (directory / "identity.json").write_text(json.dumps({"sha256": digest}))
    os.replace(launcher, binary)
    return digest


def arm(directory, sandbox_id, daemon_pid, *, snapshot=None):
    directory = Path(directory)
    identity = process_identity(daemon_pid)
    if identity is None:
        raise RuntimeError("sandboxd process identity is unavailable")
    token = {
        "args": ["--root", "/run/sandboxd/runc", "delete", "--force", sandbox_id],
        "owner": identity,
        "expires": time.time() + 5,
        "snapshot": snapshot,
    }
    with (directory / "armed.json").open("x") as stream:
        json.dump(token, stream)


def claim(directory, args):
    directory = Path(directory)
    armed = directory / "armed.json"
    try:
        token = json.loads(armed.read_text())
        if args != token["args"]:
            return False
        if token["expires"] <= time.time() or token["owner"] != process_identity(
            os.getppid()
        ):
            armed.unlink(missing_ok=True)
            return False
        # Move the sole token before returning an error. Every subsequent call
        # delegates to the real binary, even if the test controller is killed.
        os.rename(armed, directory / "consumed.json")
        return True
    except (OSError, ValueError, KeyError):
        return False


def _ownership(snapshot):
    result = json.loads(
        subprocess.check_output(
            [sys.executable, "-c", snapshot["script"], *snapshot["args"]],
            text=True,
            timeout=3,
        )
    )
    args = ["HGET", snapshot["control"], "environment:" + snapshot["environment"]]
    packet = f"*{len(args)}\r\n".encode()
    for arg in args:
        raw = arg.encode()
        packet += f"${len(raw)}\r\n".encode() + raw + b"\r\n"
    with socket.create_connection(("127.0.0.1", 6379), timeout=2) as connection:
        connection.sendall(packet)
        with connection.makefile("rb") as stream:
            header = stream.readline()
            if not header.startswith(b"$"):
                raise RuntimeError("unexpected fixture Redis response")
            length = int(header[1:])
            record = json.loads(stream.read(length)) if length >= 0 else {}
    state = record.get("result") or {}
    result.update(
        resources_held=state.get("resources_held"),
        state=state.get("state"),
        assignment=record.get("assignment"),
    )
    return result


def run(directory, args):
    directory = Path(directory)
    injected = claim(directory, args)
    try:
        token = json.loads((directory / "consumed.json").read_text())
        if args == token["args"]:
            event = {
                "time": time.time(),
                "action": "inject" if injected else "delegate",
            }
            if token.get("snapshot"):
                try:
                    event["ownership"] = _ownership(token["snapshot"])
                except Exception as error:
                    event["snapshot_error"] = type(error).__name__
            (
                directory / (str(time.time_ns()) + "-" + uuid.uuid4().hex + ".json")
            ).write_text(json.dumps(event))
    except (OSError, ValueError, KeyError):
        pass
    if injected:
        print(
            "injected runc delete exec failure: no such file or directory",
            file=sys.stderr,
        )
        return 1
    real = str(directory / "real-runc")
    os.execv(real, [real, *args])


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1], sys.argv[2:]))
