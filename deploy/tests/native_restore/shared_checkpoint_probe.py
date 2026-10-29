#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Phased cross-node native checkpoint probe; operators own the source outage."""

import argparse
import base64
import json
import os
from pathlib import Path
import time

from akernel_sdk import Client

IMAGE = "docker.io/library/python@sha256:57d96f71de145a98e7ceca18ee4ade77bba4103a33a72dfaea026b23625c6bf0"
SERVER = '''import hashlib,json,secrets
from http.server import BaseHTTPRequestHandler,HTTPServer
nonce=secrets.token_hex(32)
memory=bytearray(b"native-memory-state" * 1048576)
balance=41
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  body=json.dumps(dict(nonce=nonce,balance=balance,sha256=hashlib.sha256(memory).hexdigest(),bytes=len(memory))).encode()
  self.send_response(200);self.end_headers();self.wfile.write(body)
 def do_POST(self):
  global balance
  balance+=int(self.rfile.read(int(self.headers["Content-Length"])))
  self.do_GET()
HTTPServer(("127.0.0.1",8765),Handler).serve_forever()
'''


def command(sandbox, code):
    result = sandbox.commands.run(code, timeout=30)
    if result.exit_code:
        raise RuntimeError(f"probe command failed: {result.exit_code}: {result.stderr[:500]}")
    return result.stdout


def state(sandbox, delta=None):
    script = 'import urllib.request; '
    script += 'r=urllib.request.Request("http://127.0.0.1:8765/",'
    script += ('data=' + repr(str(delta).encode()) if delta is not None else 'data=None') + '); '
    script += 'print(urllib.request.urlopen(r,timeout=5).read().decode())'
    encoded = base64.b64encode(script.encode()).decode()
    return json.loads(command(sandbox, "python3 -c 'import base64;exec(base64.b64decode(\"" + encoded + "\"))'"))


def write_exclusive(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(data, output, indent=2)
        output.write("\n")


def capture(client, args):
    if args.state.exists():
        raise FileExistsError("capture state exists; use a fresh owned test path")
    source = client.Sandbox.create(
        image=args.image, runtime="runsc", storage_mb=512, cpu=1000, memory=1024,
        node_id=args.node_id, failover=False, detached=True, timeout=300,
    )
    try:
        source.files.write("/tmp/native-memory-probe.py", SERVER)
        source.files.write("/tmp/native-persistent.txt", "source-file-before-checkpoint\n")
        source.commands.run("python3 /tmp/native-memory-probe.py", background=True)
        # Startup is a condition with a deadline, not an unbounded retry.
        for attempt in range(20):
            try:
                expected = state(source)
                break
            except Exception:
                if attempt == 19:
                    raise
                time.sleep(0.1)
        expected = state(source, 17)
        assert expected["balance"] == 58
        command(source, "python3 -c 'f=open(\"/tmp/native-sparse.bin\",\"wb\"); f.write(b\"sparse-start\"); f.seek(32*1024*1024); f.write(b\"sparse-middle\"); f.truncate(64*1024*1024); f.close()'")
        started = time.monotonic()
        snapshot = source.create_snapshot(name=args.name, timeout=300)
        checkpoint_seconds = time.monotonic() - started
        assert state(source) == expected, "leave-running capture changed the source process"
        data = {"source_id": source.sandbox_id, "source_node": args.node_id,
                "snapshot_id": snapshot.snapshot_id, "expected": expected,
                "checkpoint_seconds": checkpoint_seconds, "image": args.image}
        write_exclusive(args.state, data)
        source.close()
        print(json.dumps({k: v for k, v in data.items() if k != "expected"}), flush=True)
    except BaseException:
        source.kill(delete_remote=True)
        raise


def restore(client, args):
    saved = json.loads(args.state.read_text())
    if args.node_id == saved["source_node"]:
        raise ValueError("cross-node validation requires a different target")
    started = time.monotonic()
    sandbox = client.Sandbox.from_snapshot(saved["snapshot_id"], node_id=args.node_id,
                                           detached=True, timeout=300)
    try:
        restored_seconds = time.monotonic() - started
        assert state(sandbox) == saved["expected"], "process memory or business state was not restored"
        assert sandbox.files.read("/tmp/native-persistent.txt") == "source-file-before-checkpoint\n"
        command(sandbox, "python3 -c 'f=open(\"/tmp/native-sparse.bin\",\"rb\"); assert f.read(12)==b\"sparse-start\"; f.seek(32*1024*1024); assert f.read(13)==b\"sparse-middle\"; assert f.seek(0,2)==64*1024*1024'")
        assert state(sandbox, args.delta)["balance"] == saved["expected"]["balance"] + args.delta
        sandbox.files.write("/tmp/native-persistent.txt", "private-write:" + args.node_id)
        assert sandbox.files.read("/tmp/native-persistent.txt") == "private-write:" + args.node_id
        result = {"target_node": args.node_id, "sandbox_id": sandbox.sandbox_id,
                  "snapshot_id": saved["snapshot_id"], "restore_seconds": restored_seconds,
                  "validated_seconds": time.monotonic() - started, "delta": args.delta,
                  "memory_bytes": saved["expected"]["bytes"], "result": "passed"}
        if args.result:
            write_exclusive(args.result, result)
        print(json.dumps(result), flush=True)
    finally:
        sandbox.kill(delete_remote=True)


def concurrent(client, args):
    """Keep both consumers alive while exercising independent memory and disk."""
    saved = json.loads(args.state.read_text())
    nodes = args.node_id.split(",")
    if len(nodes) != 2 or len(set(nodes)) != 2:
        raise ValueError("concurrent phase requires two distinct comma-separated node IDs")
    sandboxes = []
    try:
        for node in nodes:
            sandboxes.append(client.Sandbox.from_snapshot(
                saved["snapshot_id"], node_id=node, detached=True, timeout=300))
        for sandbox in sandboxes:
            assert state(sandbox) == saved["expected"]
        for i, sandbox in enumerate(sandboxes):
            sandbox.files.write("/tmp/native-persistent.txt", f"private-instance-{i}")
            assert state(sandbox, 7 + i)["balance"] == saved["expected"]["balance"] + 7 + i
        for i, sandbox in enumerate(sandboxes):
            assert sandbox.files.read("/tmp/native-persistent.txt") == f"private-instance-{i}"
            assert state(sandbox)["balance"] == saved["expected"]["balance"] + 7 + i
        result = {"result": "passed", "snapshot_id": saved["snapshot_id"],
                  "nodes": nodes, "sandbox_ids": [s.sandbox_id for s in sandboxes],
                  "checks": ["simultaneous consumers", "private memory", "private filesystem"]}
        if args.result:
            write_exclusive(args.result, result)
        print(json.dumps(result), flush=True)
        if args.hold_seconds:
            time.sleep(args.hold_seconds)
    finally:
        for sandbox in sandboxes:
            sandbox.kill(delete_remote=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["capture", "restore", "concurrent"])
    parser.add_argument("--connection-file", type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--name", default="native-cross-node-probe")
    parser.add_argument("--delta", type=int, default=7)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--hold-seconds", type=int, default=0,
                        help="hold validated concurrent instances for operator extent inspection")
    args = parser.parse_args()
    client = Client(**json.loads(args.connection_file.read_text())) if args.connection_file else Client()
    {"capture": capture, "restore": restore, "concurrent": concurrent}[args.phase](client, args)


if __name__ == "__main__":
    main()
