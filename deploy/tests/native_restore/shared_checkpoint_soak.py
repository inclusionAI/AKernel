#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Observe an existing native Snapshot without retaining new remote instances."""

import argparse
import json
import os
from pathlib import Path
import signal
import time

from akernel_sdk import Client
from shared_checkpoint_probe import restore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--nodes", required=True, help="comma-separated target node IDs")
    parser.add_argument("--result-directory", required=True, type=Path)
    parser.add_argument("--duration", type=int, default=86400)
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    nodes = args.nodes.split(",")
    source = json.loads(args.state.read_text())["source_node"]
    if args.duration < 1 or args.interval < 1 or not all(nodes) or source in nodes:
        raise ValueError("positive duration/interval and non-source target nodes required")
    os.umask(0o077)
    args.result_directory.mkdir(parents=True, exist_ok=False)
    client = Client(**json.loads(args.connection_file.read_text()))
    started = time.monotonic()
    started_unix = time.time()
    count = 0
    status = "running"
    def interrupted(signum, frame):
        raise InterruptedError("observation interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    (args.result_directory / "summary.json").write_text(json.dumps({
        "result": status, "iterations": 0, "started_unix": started_unix,
        "duration_seconds": args.duration}, indent=2) + "\n")
    try:
        while True:
            count += 1
            probe = argparse.Namespace(state=args.state, node_id=nodes[(count - 1) % len(nodes)],
                                       delta=7, result=args.result_directory / f"{count:05d}.json")
            restore(client, probe)
            if time.monotonic() - started >= args.duration:
                status = "passed"
                break
            time.sleep(max(0, min(args.interval, args.duration - (time.monotonic() - started))))
    except BaseException as error:
        status = "failed"
        print(json.dumps({"result": status, "iteration": count,
                          "errorType": type(error).__name__}), flush=True)
        raise
    finally:
        result = {"result": status, "iterations": count, "started_unix": started_unix,
                  "elapsed_seconds": time.monotonic() - started, "duration_seconds": args.duration}
        (args.result_directory / "summary.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
