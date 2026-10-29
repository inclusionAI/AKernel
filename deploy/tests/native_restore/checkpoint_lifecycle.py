#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in SDK lifecycle check on an operator-owned, idle cluster."""

import argparse
import json
from pathlib import Path
import time
import uuid

from akernel_sdk import Client
from shared_checkpoint_probe import IMAGE, write_exclusive


def delete_snapshot(client, identity):
    deadline = time.monotonic() + 60
    while True:
        try:
            client.Sandbox.delete_snapshot(identity)
            return
        except Exception as error:
            if "snapshot is in use" not in str(error) or time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", type=Path, required=True)
    parser.add_argument("--source-node", required=True)
    parser.add_argument("--target-node", required=True)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    if args.source_node == args.target_node or args.result.exists():
        parser.error("distinct nodes and a fresh result path are required")
    client = Client(**json.loads(args.connection_file.read_text()))
    owned = []
    handles = []
    snapshot = None
    failure = None
    timings = {}
    try:
        started = time.monotonic()
        source = client.Sandbox.create(
            image=args.image, runtime="runsc", storage_mb=512, cpu=1000, memory=1024,
            node_id=args.source_node, failover=False, detached=True, timeout=180)
        owned.append(source.sandbox_id)
        handles.append(source)
        timings["create_seconds"] = time.monotonic() - started
        marker = uuid.uuid4().hex
        path = "/tmp/akernel-checkpoint-lifecycle.txt"
        source.files.write(path, marker)
        command = source.commands.run("printf checkpoint-ready", timeout=20)
        assert command.exit_code == 0 and command.stdout == "checkpoint-ready"
        identity = source.sandbox_id
        started = time.monotonic()
        assert source.pause()
        timings["pause_seconds"] = time.monotonic() - started
        source.close()
        started = time.monotonic()
        source = client.Sandbox.connect(identity, timeout=180)
        handles.append(source)
        timings["resume_seconds"] = time.monotonic() - started
        assert source.sandbox_id == identity and source.files.read(path) == marker
        started = time.monotonic()
        snapshot = source.create_snapshot(name="checkpoint-lifecycle-" + uuid.uuid4().hex, timeout=180)
        timings["snapshot_seconds"] = time.monotonic() - started
        command = source.commands.run("printf source-continues", timeout=20)
        assert command.exit_code == 0 and command.stdout == "source-continues"
        started = time.monotonic()
        restored = client.Sandbox.from_snapshot(
            snapshot.snapshot_id, node_id=args.target_node, detached=True, timeout=180)
        owned.append(restored.sandbox_id)
        handles.append(restored)
        timings["cross_node_restore_seconds"] = time.monotonic() - started
        assert restored.sandbox_id != identity and restored.files.read(path) == marker
        restored.files.write(path, "private-restored")
        assert source.files.read(path) == marker
    except BaseException as error:
        failure = error
    finally:
        errors = []
        for identity in reversed(owned):
            try:
                client.Sandbox.kill(identity)
            except Exception as error:
                errors.append(error)
        for handle in handles:
            try:
                handle.close()
            except Exception as error:
                errors.append(error)
        if snapshot is not None:
            try:
                delete_snapshot(client, snapshot.snapshot_id)
            except Exception as error:
                errors.append(error)
        if failure is not None:
            errors.insert(0, failure)
        if errors:
            raise BaseExceptionGroup("checkpoint lifecycle or cleanup failed", errors)
    result = {"result": "passed", "timings": timings,
              "checks": ["create", "execute", "files", "pause", "same-ID resume",
                         "leave-running Snapshot", "cross-node restore", "private writes",
                         "remote instance deletion", "Snapshot deletion"], "unix": time.time()}
    write_exclusive(args.result, result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
