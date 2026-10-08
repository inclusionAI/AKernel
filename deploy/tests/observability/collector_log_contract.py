#!/usr/bin/env python3
"""Run the deployed Collector binary against isolated ADX filelog fixtures.

No workload process is restarted. The selected pod must contain otelcol-contrib.
"""

from __future__ import annotations
import argparse
import json
import subprocess
import uuid
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--namespace", default="akernel")
    parser.add_argument(
        "--source-ref", help="Read configurations at this Git ref for a red test"
    )
    args = parser.parse_args()
    base = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "-n",
        args.namespace,
        "exec",
        "-i",
        args.pod,
        "--",
    ]

    def run(cmd, data=None):
        r = subprocess.run(base + cmd, input=data, text=True, capture_output=True)
        if r.returncode:
            raise RuntimeError(r.stdout + r.stderr)
        return r.stdout

    root = "/tmp/adx-log-contract-" + uuid.uuid4().hex
    run(["mkdir", "-p", root])
    try:
        for label, path in [
            ("node", "builder/config/otel-collector-config.yaml"),
            ("control", "deploy/akernel/charts/core/files/otel-collector/control.yaml"),
        ]:
            content = (
                subprocess.check_output(
                    ["git", "show", args.source_ref + ":" + path], cwd=ROOT, text=True
                )
                if args.source_ref
                else (ROOT / path).read_text()
            )
            original = yaml.safe_load(content)
            receiver = original["receivers"]["filelog/adx"]
            receiver["include"] = [root + "/" + label + "/coordinator.log"]
            receiver.pop("storage", None)
            cfg = {
                "receivers": {"filelog/adx": receiver},
                "processors": {
                    k: v
                    for k, v in original["processors"].items()
                    if k.startswith("transform/adx")
                },
                "exporters": {"file": {"path": root + "/" + label + "/output.json"}},
                "service": {
                    "pipelines": {
                        "logs": {
                            "receivers": ["filelog/adx"],
                            "processors": [
                                k
                                for k in original["service"]["pipelines"]["logs"][
                                    "processors"
                                ]
                                if k.startswith("transform/adx")
                            ],
                            "exporters": ["file"],
                        }
                    }
                },
            }
            run(["mkdir", "-p", root + "/" + label])
            run(
                ["sh", "-c", 'cat > "$1"', "sh", root + "/" + label + "/config.yaml"],
                yaml.safe_dump(cfg, sort_keys=False),
            )
            run(
                [
                    "/usr/local/bin/otelcol-contrib",
                    "validate",
                    "--config=" + root + "/" + label + "/config.yaml",
                ]
            )
            samples = [
                {
                    "timestamp": "2026-10-08T05:00:00.123456Z",
                    "level": "INFO",
                    "target": "adx::state",
                    "fields": {
                        "message": "environment operation completed",
                        "environment_id": "fixture-env",
                        "operation_id": "fixture-op",
                        "trace_id": "1" * 32,
                        "span_id": "2" * 16,
                        "result": True,
                    },
                },
                "plain runtime diagnostic",
                "{bad json",
                {"level": "WARN", "fields": {"environment_id": "no-message"}},
                {
                    "timestamp": "2026-10-08T05:00:01.000000Z",
                    "level": "ERROR",
                    "target": "adx::state",
                    "fields": {"message": "", "error": "fixture-error"},
                },
                {
                    "level": "WARN",
                    "fields": {
                        "event": "environment_operation_completed",
                        "environment_id": "event-only",
                    },
                },
            ]
            run(
                [
                    "sh",
                    "-c",
                    'cat > "$1"',
                    "sh",
                    root + "/" + label + "/coordinator.log",
                ],
                "\n".join(json.dumps(s) if isinstance(s, dict) else s for s in samples)
                + "\n",
            )
            proc = subprocess.Popen(
                base
                + [
                    "sh",
                    "-c",
                    '/usr/local/bin/otelcol-contrib --config="$1" > "$2" 2>&1 & p=$!; sleep 5; kill -TERM "$p"; wait "$p"',
                    "sh",
                    root + "/" + label + "/config.yaml",
                    root + "/" + label + "/collector.log",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate(timeout=20)
            if proc.returncode:
                raise AssertionError(
                    run(["cat", root + "/" + label + "/collector.log"]) + stderr
                )
            docs = [
                json.loads(line)
                for line in run(
                    ["cat", root + "/" + label + "/output.json"]
                ).splitlines()
            ]
            records = [
                record
                for doc in docs
                for resource in doc["resourceLogs"]
                for scope in resource["scopeLogs"]
                for record in scope["logRecords"]
            ]
            assert len(records) == 6, (label, len(records))
            primary = records[0]
            assert primary["body"] == {
                "stringValue": "environment operation completed"
            }, (label, primary["body"])
            attrs = {a["key"]: a["value"] for a in primary["attributes"]}
            for key, value in [
                ("environment_id", "fixture-env"),
                ("operation_id", "fixture-op"),
                ("trace_id", "1" * 32),
                ("span_id", "2" * 16),
                ("target", "adx::state"),
            ]:
                assert attrs[key] == {"stringValue": value}, (key, attrs)
            assert primary["severityText"] == "INFO" and primary["severityNumber"] == 9
            assert primary["timeUnixNano"] == "1791435600123456000", primary[
                "timeUnixNano"
            ]
            assert records[1]["body"] == {"stringValue": "plain runtime diagnostic"}
            assert records[2]["body"] == {"stringValue": "{bad json"}
            assert "kvlistValue" in records[3]["body"], (
                "JSON without a message must keep the full body"
            )
            assert (
                records[4]["body"] == {"stringValue": ""}
                and records[4]["severityNumber"] == 17
            )
            assert records[5]["body"] == {
                "stringValue": "environment_operation_completed"
            }, records[5]["body"]
            print(
                "PASS",
                label,
                "message, metadata, severity, source timestamp, plain/malformed/missing/empty message; 6/6 records",
                flush=True,
            )
    finally:
        # Only remove the unique scratch directory created by this invocation.
        run(["rm", "-rf", root])


if __name__ == "__main__":
    main()
