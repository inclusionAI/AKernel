#!/usr/bin/env python3
"""Exercise fleet metric filtering with the real Collector and rendered Helm config."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[3]
CORE = ROOT / "deploy/akernel/charts/core"
MONITOR = ROOT / "deploy/akernel/charts/monitor"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    rendered = subprocess.check_output(["helm", "template", "monitor", str(MONITOR)], text=True)
    objects = {(r["kind"], r["metadata"]["name"]): r for r in yaml.safe_load_all(rendered) if r}
    prom = yaml.safe_load(objects["ConfigMap", "prometheus-config"]["data"]["prometheus.yml"])
    flags = objects["StatefulSet", "prometheus"]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--storage.tsdb.retention.time=7d" in flags, "default retention must be seven days"
    assert next(j for j in prom["scrape_configs"] if j["job_name"] == "prometheus")["scrape_interval"] == "30s"
    rules = objects["ConfigMap", "prometheus-config"]["data"]["adx-rules.yaml"]
    assert "adx:environment_create_request_qps:rate1m" not in rules
    assert "adx:environment_created_qps:rate1m" in rules

    diagnostic = ["otelcol_processor_batch_batch_send_size", "otelcol_processor_batch_batch_send_size_bytes",
                  "http_client_duration_milliseconds", "http_server_duration_milliseconds",
                  "http.client.duration_milliseconds", "http.server.duration_milliseconds"]
    for side in ("client", "server"):
        for suffix in ("duration_milliseconds", "request.size_bytes", "response.size_bytes",
                       "requests_per_rpc", "responses_per_rpc"):
            dotted = "rpc." + side + "." + suffix
            diagnostic.extend((dotted, dotted.replace(".", "_")))

    def histogram(name: str) -> str:
        if "." in name:
            # Prometheus UTF-8 negotiation keeps dots in Collector instrument
            # names; remote_write later normalizes them to underscores.
            return (f'# TYPE "{name}" histogram\n'
                    f'{{"{name}_bucket",le="0.1"}} 2\n'
                    f'{{"{name}_bucket",le="+Inf"}} 5\n'
                    f'{{"{name}_sum"}} 0.9\n{{"{name}_count"}} 5\n')
        return (f"# TYPE {name} histogram\n{name}_bucket{{le=\"0.1\"}} 2\n"
                f"{name}_bucket{{le=\"+Inf\"}} 5\n{name}_sum 0.9\n{name}_count 5\n")
    health = ["otelcol_exporter_queue_size", "otelcol_exporter_queue_capacity",
              "otelcol_receiver_refused_metric_points_total", "otelcol_exporter_enqueue_failed_spans_total",
              "otelcol_exporter_send_failed_log_records_total", "adx_environment_created_total"]
    native = "data_plane_ingress_http_request_duration_seconds"
    exposition = "".join(histogram(n) for n in diagnostic + [native])
    exposition += "".join(f"# TYPE {name} gauge\n{name} 7\n" for name in health)
    exposition += '# TYPE adx_environment_memory_usage_bytes gauge\nadx_environment_memory_usage_bytes{environment_id="must-not-duplicate"} 64\n'
    (output / "metrics").write_text(exposition)
    (output / "serve.py").write_text(
        "from functools import partial\n"
        "from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer\n"
        "class Handler(SimpleHTTPRequestHandler):\n"
        " def guess_type(self, path):\n"
        "  return 'text/plain; version=0.0.4; charset=utf-8; escaping=allow-utf-8'\n"
        "ThreadingHTTPServer(('0.0.0.0', 8000), partial(Handler, directory='/fixtures')).serve_forever()\n"
    )
    name = "adx-metric-policy-" + uuid.uuid4().hex[:10]
    containers: list[str] = []
    network = False
    checks = []

    def docker(*args: str) -> str:
        return subprocess.check_output(["docker", *args], text=True)

    try:
        docker("network", "create", "--internal", name)
        network = True
        server = name + "-source"
        docker("run", "-d", "--name", server, "--network", name, "--network-alias", "source",
               "-v", str(output) + ":/fixtures:ro", "python:3.12-slim", "python3", "/fixtures/serve.py")
        containers.append(server)
        node = yaml.safe_load((CORE / "files/otel-collector/config.yaml").read_text())
        control = yaml.safe_load((CORE / "files/otel-collector/control.yaml").read_text())
        standalone = yaml.safe_load((ROOT / "builder/config/otel-collector-standalone.yaml").read_text())
        for role, config in (("node", node), ("control", control), ("standalone", standalone)):
            jobs = config["receivers"]["prometheus/adx"]["config"]["scrape_configs"]
            selected = next(j for j in jobs if j["job_name"] == "otel-collector")
            assert selected["scrape_interval"] == "30s"
            assert all(j["scrape_interval"] == "15s" for j in jobs if j["job_name"] != "otel-collector")
            self_job = json.loads(json.dumps(selected))
            self_job.update(scrape_interval="1s", scrape_timeout="1s")
            self_job["static_configs"] = [{"targets": ["source:8000"]}]
            application = {"job_name": "application", "scrape_interval": "1s", "scrape_timeout": "1s",
                           "static_configs": [{"targets": ["source:8000"]}]}
            folder = output / role
            folder.mkdir(exist_ok=True)
            folder.chmod(0o777)
            cfg = {"receivers": {"prometheus": {"config": {"scrape_configs": [self_job, application]}}},
                   "processors": {"filter/summary_metrics": node["processors"]["filter/summary_metrics"]},
                   "exporters": {"file": {"path": "/result/output.json"}},
                   "service": {"pipelines": {"metrics": {"receivers": ["prometheus"],
                                "processors": ["filter/summary_metrics"], "exporters": ["file"]}}}}
            (folder / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
            container = name + "-" + role
            docker("run", "-d", "--name", container, "--network", name,
                   "-v", str(folder) + ":/result", "otel/opentelemetry-collector-contrib:0.120.0",
                   "--config=/result/config.yaml")
            containers.append(container)
            found: dict[str, dict[str, dict]] = {}
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                target = folder / "output.json"
                if target.exists():
                    for line in target.read_text().splitlines():
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        for rm in data.get("resourceMetrics", []):
                            attrs = {a["key"]: a["value"].get("stringValue", "") for a in rm.get("resource", {}).get("attributes", [])}
                            job = attrs.get("service.name", "")
                            for sm in rm.get("scopeMetrics", []):
                                for metric in sm.get("metrics", []):
                                    found.setdefault(job, {})[metric["name"]] = metric
                if all(job in found and any(n.startswith(native.removesuffix("_seconds")) for n in found[job]) for job in ("otel-collector", "application")):
                    break
                time.sleep(1)
            (folder / "collector.log").write_text(docker("logs", container))
            (folder / "observed-names.json").write_text(json.dumps({k: sorted(v) for k, v in found.items()}, indent=2))
            assert "otel-collector" in found and "application" in found, found.keys()
            for base in diagnostic:
                assert not any(n.startswith(base.removesuffix("_milliseconds")) for n in found["otel-collector"]), (role, base)
                assert any(n.startswith(base.removesuffix("_milliseconds")) for n in found["application"]), (role, base, found["application"].keys())
            for job in ("otel-collector", "application"):
                for metric in health:
                    assert any(n == metric or n == metric.removesuffix("_total") for n in found[job]), (role, job, metric)
                metric = next(v for n, v in found[job].items() if n.startswith(native.removesuffix("_seconds")))
                point = metric["histogram"]["dataPoints"][0]
                assert int(point["count"]) == 5 and list(map(int, point["bucketCounts"])) == [2, 3], point
                assert not any(n.startswith("adx_environment_memory_usage") for n in found[job])
            checks.append(role + ": scoped diagnostic filter, health/native histogram retained, summary excludes Environment IDs")
            docker("stop", container)
        (output / "result.json").write_text(json.dumps({"passed": True, "checks": checks}, indent=2) + "\n")
        print("PASS seven-day default and three real Collector collection policies")
    finally:
        for container in reversed(containers):
            subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if network:
            subprocess.run(["docker", "network", "rm", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
