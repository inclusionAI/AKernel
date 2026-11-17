#!/usr/bin/env python3
"""Evaluate provisioned ADX queries with colliding identities in two clusters.

Requires helm, PyYAML and promtool (or --docker). Writes the exact rule/input
fixtures to --output so failures can be replayed independently of this runner.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
MONITOR = ROOT / "deploy/akernel/charts/monitor"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--producer-samples", type=Path,
                        help="startup/first-event expositions emitted by ADX producer tests")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    rendered = subprocess.check_output(
        ["helm", "template", "monitor", str(MONITOR)], text=True
    )
    resources = {(r["kind"], r["metadata"]["name"]): r
                 for r in yaml.safe_load_all(rendered) if r}
    data = resources["ConfigMap", "grafana-dashboards"]["data"]
    rules = resources["ConfigMap", "prometheus-config"]["data"]["adx-rules.yaml"]
    (output / "adx-rules.yaml").write_text(rules)

    def panel(uid: str, title: str) -> str:
        dashboard = json.loads(data[uid + ".json"])
        return next(p["targets"][0]["expr"] for p in dashboard["panels"]
                    if p["title"] == title)

    def scope(expr: str, cluster: str = ".+", state: str = "Running|Paused") -> str:
        return (expr.replace("$cluster", cluster).replace("$adx_env", "shared")
                .replace("$state", state).replace("${environment_id:raw}", "same-env")
                .replace("$__rate_interval", "2m"))

    series = []
    for cluster, memory, step, state, admitted in (
        ("a", 128, 15, "Running", 1), ("b", 256, 30, "Paused", 0)
    ):
        labels = f'cluster="{cluster}",adx_env="shared"'
        identity = labels + ',environment_id="same-env",instance="node:19091",runtime_id="same-runtime"'
        node = labels + ',node_id="same-node"'
        for metric, suffix, values in (
            ("adx_environment_memory_usage_bytes", identity, f"{memory}+0x8"),
            ("adx_environment_cpu_usage_seconds_total", identity, f"0+{step}x8"),
            ("adx_environment_stats_age_seconds", identity, f"{0 if admitted else 60}+0x8"),
            ("adx_environment_info", node + f',environment_id="same-env",state="{state}"', "1+0x8"),
            ("adx_environment_create_requests_total", labels, f"0+{step}x8"),
            ("adx_environment_created_total", labels, f"0+{step}x8"),
            ("adx_coordinator_node_schedulable", node, f"{admitted}+0x8"),
            ("adx_coordinator_node_reachable", node, "1+0x8"),
            ("adx_coordinator_node_capacity_cpu_millis", node, "1000+0x8"),
            ("adx_coordinator_environments", node + ',state="Running"', f"{admitted * 3}+0x8"),
            ("adx_observability_started_timestamp_seconds", labels, "1793059200+0x8"),
        ):
            series.append({"series": metric + "{" + suffix + "}", "values": values})

    # A detail selection must stay scoped even when hundreds of other live
    # environments occupy the same cluster, node and resource endpoint.
    for index in range(500):
        labels = f'cluster="a",adx_env="shared",environment_id="unselected-{index}",instance="node:19091",runtime_id="runtime-{index}"'
        for metric, values in (("adx_environment_memory_usage_bytes", "64+0x8"),
                               ("adx_environment_cpu_usage_seconds_total", "0+15x8"),
                               ("adx_environment_stats_age_seconds", "0+0x8")):
            series.append({"series": metric + "{" + labels + "}", "values": values})
        series.append({"series": f'adx_environment_info{{cluster="a",adx_env="shared",node_id="same-node",environment_id="unselected-{index}",state="Running"}}', "values": "1+0x8"})

    # Overview histories stay aggregated when a deployment grows to 1,000 nodes.
    for index in range(1000):
        series.append({"series": f'data_plane_relay_active_streams{{cluster="a",adx_env="shared",k8s_node_name="node-{index}"}}', "values": "3+0x8"})
    series.append({"series": 'data_plane_relay_active_streams{cluster="b",adx_env="shared",k8s_node_name="node-0"}', "values": "7+0x8"})

    checks = []

    def check(expr: str, samples: list[dict]) -> None:
        checks.append({"expr": expr, "eval_time": "2m", "exp_samples": samples})

    # B is Paused but still has a last usage sample. Neither panel may show A
    # because B shares its environment/runtime/instance identifiers.
    for uid in ("adx-environments", "sandbox-lifecycle-dashboard"):
        for title in ("Environment Memory Usage", "Environment CPU Usage"):
            expr = panel(uid, title)
            for cluster in ("a", "b", ".+"):
                check(scope(expr, cluster, "Paused"), [])
            metric = "adx_environment_memory_usage_bytes" if "Memory" in title else ""
            labels = metric + '{cluster="a",adx_env="shared",environment_id="same-env",instance="node:19091",runtime_id="same-runtime"}'
            check(scope(expr, "a", "Running"), [{"labels": labels, "value": 128 if metric else 0.9999999999999999}])
            check(scope(expr, "b", "Running"), [])

    # Exercise the provisioned closed default, not only the selected-ID path.
    for uid in ("adx-environments", "sandbox-lifecycle-dashboard"):
        dashboard = json.loads(data[uid + ".json"])
        variable = next(v for v in dashboard["templating"]["list"] if v["name"] == "environment_id")
        default = variable["current"]["value"]
        assert default == variable["query"] == "__select_environment__"
        for p in dashboard["panels"]:
            expression = p["targets"][0]["expr"]
            check(scope(expression).replace("same-env", default), [])
            # Every side of a resource freshness/inventory join must apply the
            # selection, otherwise a single-ID graph still scans fleet state.
            for metric in ("adx_environment_stats_age_seconds", "adx_environment_info"):
                selected = expression.replace("${environment_id:raw}", "__selected_environment__")
                start = selected.find(metric + "{")
                if start >= 0:
                    selector = selected[start:].split("}", 1)[0]
                    assert 'environment_id=~`__selected_environment__`' in selector
    check(scope(panel("adx-data-plane", "Relay Active Streams (cluster total)")), [
        {"labels": '{cluster="a",adx_env="shared"}', "value": 3000},
        {"labels": '{cluster="b",adx_env="shared"}', "value": 7},
    ])

    for uid, model in data.items():
        dashboard = json.loads(model)
        if not (dashboard.get("uid", "").startswith("adx-") or dashboard.get("uid") == "sandbox-metrics"):
            continue
        assert dashboard.get("refresh", "") in ("", "1m")
        for p in dashboard.get("panels", []):
            if p["type"] == "timeseries":
                assert p["maxDataPoints"] <= 200 and p["interval"] == "1m"
            for t in p.get("targets", []):
                datasource = t.get("datasource", p.get("datasource", {})).get("uid")
                if datasource == "prometheus" and p["type"] in ("stat", "gauge", "table"):
                    assert t["instant"] and not t["range"]
                if datasource == "loki" and "maxLines" in t:
                    assert t["maxLines"] <= 200
                if datasource == "tempo" and "limit" in t:
                    assert t["limit"] <= 20
                if p["type"] == "timeseries" and "rate(" in t.get("expr", ""):
                    assert not re.search(r"\[\d+[smh]\]", t["expr"]), "fixed rate window leaves gaps at wide query steps"

    for record in ("adx:environment_created_qps:rate1m",):
        check(record + '{adx_env="shared"}', [
            {"labels": record + '{cluster="a",adx_env="shared"}', "value": 0.9999999999999999},
            {"labels": record + '{cluster="b",adx_env="shared"}', "value": 1.9999999999999998},
        ])
        for cluster, expected in (("a", 0.9999999999999999), ("b", 1.9999999999999998)):
            check(f'sum({record}{{cluster="{cluster}",adx_env="shared"}})',
                  [{"labels": "{}", "value": expected}])
    check('sandbox_running{adx_env="shared"}', [
        {"labels": 'sandbox_running{cluster="a",adx_env="shared",node_id="same-node"}', "value": 3},
        {"labels": 'sandbox_running{cluster="b",adx_env="shared",node_id="same-node"}', "value": 0},
    ])
    schedule = json.loads(data["adx-schedule.json"])
    cpu_nodes = next(t["expr"] for p in schedule["panels"] for t in p.get("targets", [])
                     if '"node_key"' in t.get("expr", "") and "capacity_cpu_millis" in t["expr"])
    check(scope(cpu_nodes), [
        {"labels": '{cluster="a",adx_env="shared",node_id="same-node",node_key="a/shared/same-node"}', "value": 1},
        {"labels": '{cluster="b",adx_env="shared",node_id="same-node",node_key="b/shared/same-node"}', "value": 0},
    ])
    check(scope(panel("adx-sandbox-lifecycle", "Statistics Started At"), "a"), [
        {"labels": '{cluster="a",adx_env="shared"}', "value": 1793059200000},
    ])
    # Prior zero scrapes must make both the first event and first burst visible
    # through the exact provisioned diagnostic queries. Optionally validate the
    # input against real producer expositions from ADX's Rust tests.
    lifecycle = json.loads(data["adx-sandbox-lifecycle.json"])
    for producer, metric, suffix in (
        ("apiserver", "adx_environment_create_errors_total",
         'outcome="unknown",stage="unknown",reason="transport"'),
        ("apiserver", "adx_environment_create_rejections_total", 'reason="unauthenticated"'),
        ("adxlet", "adx_environment_lifecycle_operations_total",
         'operation="pause",intent="idle",outcome="success"'),
    ):
        name = metric + "{" + suffix + "}"
        if args.producer_samples:
            for phase, count in (("before", 0), ("after", 1)):
                rows = (args.producer_samples / (producer + "-" + phase + ".prom")).read_text().splitlines()
                assert name + " " + str(count) in rows, (name, phase)
        expr = next(t["expr"] for p in lifecycle["panels"] for t in p.get("targets", [])
                    if metric + "{" in t.get("expr", ""))
        expr = scope(expr).replace("$__rate_interval", "2m").replace("$__range", "2m")
        for cluster, count in (("a", 1), ("b", 3)):
            labels = f'cluster="{cluster}",adx_env="shared",' + suffix
            series.append({"series": metric + "{" + labels + "}",
                           "values": f"0 0 {count}+0x6"})
        for when, multiplier in (("15s", 0), ("2m", 1)):
            checks.append({"expr": expr, "eval_time": when, "exp_samples": ([] if metric == "adx_environment_create_errors_total" and multiplier == 0 else [
                {"labels": '{cluster="a",adx_env="shared",' + suffix + "}", "value": multiplier},
                {"labels": '{cluster="b",adx_env="shared",' + suffix + "}", "value": 3 * multiplier},
            ])})

    # A 200-point dashboard uses wider steps on 6h/24h/7d views. Put an entire
    # burst between two evaluation points: fixed short rate windows miss it.
    sampled_tests = []
    for step in (120, 600, 3600):
        inputs, assertions = [], []
        for uid, title, metric, labels, expected in (
            ("adx-schedule", "Created Throughput", "adx_environment_created_total",
             '', '{cluster="a",adx_env="shared"}'),
            ("adx-schedule", "Create Call Outcomes", "adx_environment_create_responses_total",
             'outcome="failure"', '{outcome="failure"}'),
            ("adx-schedule", "Collector Refused / Enqueue Failed", "otelcol_receiver_refused_log_records_total",
             '', '{cluster="a",adx_env="shared"}'),
            ("adx-data-plane", "Ingress Response Header Latency P50 / P95 / P99",
             "data_plane_ingress_http_request_duration_seconds_bucket", 'le="1"', '{}'),
        ):
            expression = panel(uid, title).replace("$cluster", "a").replace("$adx_env", "shared")
            expression = expression.replace("$__rate_interval", f"{step + 30}s")
            identity = 'cluster="a",adx_env="shared"' + (',' + labels if labels else '')
            values = f"0+0x{step // 15 + step // 30} 60+0x{step // 15}"
            inputs.append({"series": metric + '{' + identity + '}', "values": values})
            if metric.endswith("_bucket"):
                inputs.append({"series": metric + '{' + identity.replace('le="1"', 'le="+Inf"') + '}', "values": values})
            assertions.append({"expr": '(' + expression + ') > bool 0', "eval_time": f"{step * 2}s",
                               "exp_samples": [{"labels": expected, "value": 1}]})
        sampled_tests.append({"interval": "15s", "input_series": inputs, "promql_expr_test": assertions})

    # Offset 15s scrapes from the evaluation grid. A datasource configured as
    # 10s misses the increment straddling the preceding query boundary.
    datasource = yaml.safe_load(resources["ConfigMap", "grafana-datasources"]["data"]["prometheus.yaml"])
    prometheus = next(d for d in datasource["datasources"] if d["type"] == "prometheus")
    scrape = int(prometheus["jsonData"]["timeInterval"].removesuffix("s"))
    expression = panel("adx-schedule", "Created Throughput").replace("$cluster", "a").replace("$adx_env", "shared")
    expression = expression.replace("$__rate_interval", f"{max(120 + scrape, 4 * scrape)}s")
    values = " ".join("_" if t % 15 != 5 else "0" if t < 120 else "60" for t in range(0, 246, 5))
    sampled_tests.append({"interval": "5s", "input_series": [{
        "series": 'adx_environment_created_total{cluster="a",adx_env="shared"}', "values": values}],
        "promql_expr_test": [{"expr": '(' + expression + ') > bool 0', "eval_time": "240s",
                             "exp_samples": [{"labels": '{cluster="a",adx_env="shared"}', "value": 1}]}]})

    fixture = {"rule_files": ["adx-rules.yaml"], "evaluation_interval": "15s",
               "tests": [{"interval": "15s", "input_series": series, "promql_expr_test": checks}, *sampled_tests]}
    (output / "queries.yaml").write_text(yaml.safe_dump(fixture, sort_keys=False))
    command = (["docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/promtool",
                "-v", str(output) + ":/tests:ro", "-w", "/tests", "prom/prometheus:v3.2.1"]
               if args.docker else ["promtool"])
    subprocess.run(command + ["test", "rules", "queries.yaml"], cwd=output, check=True)
    print(f"PASS: {len(checks) + sum(len(t['promql_expr_test']) for t in sampled_tests)} query cases from provisioned dashboards/rules")


if __name__ == "__main__":
    main()
