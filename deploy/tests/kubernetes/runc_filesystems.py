#!/usr/bin/env python3
"""Runc filesystem acceptance against an idle, dedicated AKernel deployment.

Use the installed SDK and its AKERNEL_SERVER_ADDRESS/AKERNEL_TOKEN environment.
Cluster setup, filesystem fault injection and port forwarding belong to the
caller. Negative mode requires an empty deployment with no available workers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sdk_versions():
    result = {}
    for package in ("akernel-sdk", "adx-environment"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    return result


def load_sdk():
    from akernel_sdk import BackendOperationError, Sandbox, resources

    return SimpleNamespace(Sandbox=Sandbox, resources=resources,
                           BackendOperationError=BackendOperationError)


def error_details(error):
    return {"type": type(error).__name__,
            **{name: getattr(error, name, None) for name in
               ("code", "status_code", "scheduling_failure")}}


class Probe:
    def __init__(self, mode, node_id, schedule_timeout=8, *, sdk=None):
        self.mode, self.node_id = mode, node_id
        self.schedule_timeout, self.sdk = schedule_timeout, sdk
        self.report = {"passed": False, "mode": mode, "node_id": node_id,
                       "phase": "sdk_import", "sdk_versions": sdk_versions(),
                       "started_at": datetime.now(timezone.utc).isoformat(),
                       "sandboxes": []}

    def inventory(self):
        # Default SDK query includes Running and Paused. Fetch every page,
        # including empty intermediate pages; errors never mean an empty list.
        paginator = self.sdk.Sandbox.list(limit=100)
        tokens, instances, pages = set(), [], 0
        while paginator.has_next:
            token = paginator.next_token
            require(token not in tokens, "inventory repeated a continuation token")
            tokens.add(token)
            instances.extend({"id": item.id, "state": item.state}
                             for item in paginator.next_items())
            pages += 1
        result = {"pages": pages, "instances": instances}
        self.report[self.report["phase"]] = result
        require(pages > 0, "inventory did not fetch any pages")
        require(not instances, "dedicated deployment contains running or paused instances")

    def resources(self):
        nodes = [{"id": node.id, "status": node.status,
                  "capacity": node.capacity, "allocatable": node.allocatable}
                 for node in self.sdk.resources()]
        self.report[self.report["phase"]] = nodes
        if self.mode == "negative":
            require(not nodes, "negative probe requires an empty resource view")
        return nodes

    def create_arguments(self):
        return {"runtime": "runc", "node_id": self.node_id, "cpu": 1000,
                "memory": 256, "schedule_timeout": self.schedule_timeout}

    def negative(self):
        self.report["phase"] = "negative_create"
        try:
            sandbox = self.sdk.Sandbox(**self.create_arguments())
        except self.sdk.BackendOperationError as error:
            self.report["expected_create_failure"] = error_details(error)
            require(not getattr(error, "cleanup_errors", ()), "creation rollback failed")
            require(error.code == "DEADLINE_EXCEEDED" and error.status_code == 504,
                    "creation did not return the expected scheduling deadline")
            failure = error.scheduling_failure
            require(isinstance(failure, dict), "structured scheduling failure is missing")
            for name in ("eligibleNodes", "nodeCount"):
                require(type(failure.get(name)) is int and failure[name] == 0,
                        "scheduling failure must report zero eligible/total nodes")
            rejections = failure.get("rejections")
            require(isinstance(rejections, dict)
                    and type(rejections.get("no_workers")) is int
                    and rejections["no_workers"] >= 1, "no_workers rejection is missing")
            for name in ("maxCapacity", "maxAvailable"):
                resources = failure.get(name)
                require(isinstance(resources, dict), "scheduling resource bounds are missing")
                for resource in ("cpu_millis", "memory_bytes", "disk_bytes"):
                    value = resources.get(resource)
                    require(type(value) in (int, float) and value == 0,
                            "scheduling resource bounds must all be zero")
        else:
            # A successful construction never enters the expected-error branch.
            # Cleanup failure must also fail acceptance, retaining the known ID.
            self.report["sandboxes"].append({"id": sandbox.id, "role": "unexpected"})
            self.report["phase"] = "unexpected_create_cleanup"
            sandbox.kill()
            raise AssertionError("creation succeeded on an unavailable target")

    def positive(self):
        for role, command, expected in (
            ("write", "printf runc-ready > /tmp/runc-probe && cat /tmp/runc-probe", "runc-ready"),
            ("isolation", "test ! -e /tmp/runc-probe && printf private-write-isolated", "private-write-isolated"),
        ):
            self.report["phase"] = role + "_create"
            with self.sdk.Sandbox(**self.create_arguments()) as sandbox:
                self.report["sandboxes"].append({"id": sandbox.id, "role": role})
                self.report["phase"] = role + "_command"
                result = sandbox.commands.run(command)
                self.report[role] = {"exit_code": result.exit_code, "stdout": result.stdout}
                require(result.exit_code == 0 and result.stdout == expected,
                        "runc command/private-write verification failed")
                self.report["phase"] = role + "_cleanup"

    def run(self):
        started = time.monotonic()
        try:
            self.sdk = self.sdk or load_sdk()
            self.report["phase"] = "inventory_before"
            self.inventory()
            self.report["phase"] = "resources_before"
            nodes = self.resources()
            if self.mode == "positive":
                require(any(node["id"] == self.node_id for node in nodes),
                        "target worker is absent from the resource view")
                self.positive()
            elif self.mode == "negative":
                self.negative()
            elif self.mode != "inventory":
                raise ValueError("unknown acceptance mode")
            self.report["phase"] = "inventory_after"
            self.inventory()
            self.report["phase"] = "resources_after"
            self.resources()
            self.report.update(passed=True, phase="complete")
            return self.report
        except Exception as error:
            self.report["error"] = error_details(error)
            raise
        finally:
            self.report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.report["finished_at"] = datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("positive", "negative", "inventory"), required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--schedule-timeout", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.schedule_timeout < 1 or not args.node_id.strip():
        parser.error("node-id must be nonempty and schedule-timeout must be positive")
    probe = Probe(args.mode, args.node_id, args.schedule_timeout)
    try:
        probe.run()
    except Exception:
        pass  # Failure details and the incomplete phase are retained below.
    raw = json.dumps(probe.report, indent=2)
    token = os.environ.get("AKERNEL_TOKEN", "")
    if token:
        raw = raw.replace(json.dumps(token)[1:-1], "<redacted>")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw + "\n")
    print(raw)
    return 0 if probe.report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
