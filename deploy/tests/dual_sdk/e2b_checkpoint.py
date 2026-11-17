#!/usr/bin/env python3
"""Phased real E2B checkpoint qualification; operators control source outage/placement."""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_restore"))
from e2b import Sandbox
from qualification import placement
from shared_checkpoint_probe import SERVER, command, state, write_exclusive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["capture", "restore"])
    parser.add_argument("--connection-file", required=True, type=Path)
    parser.add_argument("--ca", required=True, type=Path)
    parser.add_argument("--e2b-origin", help="optional HTTP E2B entry; native placement remains trusted TLS")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--expected-node", required=True)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--delta", type=int, default=7)
    parser.add_argument("--hold-seconds", type=int, default=0)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ["SSL_CERT_FILE"] = str(args.ca.resolve())
    config = json.loads(args.connection_file.read_text())
    options = {
        "api_key": config["api_key"],
        "api_url": args.e2b_origin or config["api_url"],
        "sandbox_url": args.e2b_origin or config["gateway_address"],
        "request_timeout": 300,
        "retries": 0,
    }
    if args.phase == "capture" and args.state.exists():
        raise FileExistsError("fresh capture state required")
    saved = json.loads(args.state.read_text()) if args.phase == "restore" else None
    box = Sandbox.create(
        saved["snapshot_id"] if saved else "base", timeout=1200, **options
    )
    result = None
    try:
        where = placement(config, box.sandbox_id)
        assert where["node_id"] == args.expected_node, (
            "actual placement differs from operator constraint"
        )
        if saved is None:
            box.files.write("/tmp/native-memory-probe.py", SERVER)
            box.files.write(
                "/tmp/native-persistent.txt", "source-file-before-checkpoint\n"
            )
            box.commands.run(
                "python3 /tmp/native-memory-probe.py", background=True, timeout=0
            )
            for attempt in range(30):
                try:
                    state(box)
                    break
                except Exception:
                    if attempt == 29:
                        raise
                    time.sleep(0.1)
            expected = state(box, 17)
            assert expected["balance"] == 58
            command(
                box,
                'python3 -c \'f=open("/tmp/native-sparse.bin","wb"); f.write(b"sparse-start"); f.seek(32*1024*1024); f.write(b"sparse-middle"); f.truncate(64*1024*1024); f.close()\'',
            )
            started = time.monotonic()
            snapshot = box.create_snapshot(
                name="dual-e2b-source-outage-" + uuid.uuid4().hex, request_timeout=300
            )
            assert state(box) == expected, "leave-running capture changed source state"
            result = {
                "source_id": box.sandbox_id,
                "source_node": where["node_id"],
                "snapshot_id": snapshot.snapshot_id,
                "expected": expected,
                "checkpoint_seconds": time.monotonic() - started,
                "protocol": "e2b",
                "placement": where,
                "source_deleted_after_capture": True,
            }
            write_exclusive(args.state, result)
        else:
            assert state(box) == saved["expected"], (
                "memory/nonce/business state differs"
            )
            assert (
                box.files.read("/tmp/native-persistent.txt")
                == "source-file-before-checkpoint\n"
            )
            command(
                box,
                'python3 -c \'f=open("/tmp/native-sparse.bin","rb"); assert f.read(12)==b"sparse-start"; f.seek(32*1024*1024); assert f.read(13)==b"sparse-middle"; assert f.seek(0,2)==64*1024*1024\'',
            )
            assert (
                state(box, args.delta)["balance"]
                == saved["expected"]["balance"] + args.delta
            )
            box.files.write(
                "/tmp/native-persistent.txt", "private-write:" + where["node_id"]
            )
            assert (
                box.files.read("/tmp/native-persistent.txt")
                == "private-write:" + where["node_id"]
            )
            result = {
                "result": "validated",
                "protocol": "e2b",
                "sandbox_id": box.sandbox_id,
                "snapshot_id": saved["snapshot_id"],
                "placement": where,
                "source_node": saved["source_node"],
                "memory_bytes": saved["expected"]["bytes"],
                "checks": [
                    "actual target placement",
                    "memory nonce/hash/business state",
                    "files/sparse file",
                    "private memory/files",
                ],
            }
            if args.result:
                write_exclusive(args.result, result)
            if args.hold_seconds:
                time.sleep(args.hold_seconds)
    finally:
        box.kill()
    if result and args.phase == "restore" and args.result:
        result["result"] = "passed"
        args.result.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
