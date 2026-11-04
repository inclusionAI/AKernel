#!/usr/bin/env python3
"""Exercise registered/dead nodes and same-label Collector counters with promtool."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[3]
DASHBOARDS = ROOT / "deploy/akernel/charts/monitor/dashboards"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    schedule = json.loads((DASHBOARDS / "adx-schedule.json").read_text())
    lifecycle = json.loads((DASHBOARDS / "adx-sandbox-lifecycle.json").read_text())
    by_title = {p["title"]: p for d in (schedule, lifecycle) for p in d["panels"]}
    series = []
    checks = []

    def sample(name: str, labels: str, values: str) -> None:
        series.append({"series": name + "{" + labels + "}", "values": values})

    def scope(query: str, cluster: str = "a") -> str:
        # promtool does not expand Grafana macros. Keep the presentation fixture
        # at its original 5m rate window; coarse-window coverage lives in the
        # query contract, which also validates the configured scrape interval.
        scoped = (query.replace("$cluster", cluster).replace("$adx_env", "shared")
                  .replace("$__range", "2m").replace("$__rate_interval", "5m"))
        assert "$__" not in scoped, "unexpanded Grafana macro in presentation fixture"
        return scoped

    def check(query: str, expected: list[dict], when: str = "2m") -> None:
        checks.append({"expr": query, "eval_time": when, "exp_samples": expected})

    # A reachable node with admission disabled still has existing workloads.
    for cluster, node, reachable, admission, cpu, running in (
        ("a", "live-one", 1, 1, 1000, 2),
        ("a", "live-two", 1, 0, 1000, 2),
        ("a", "retired", 0, 0, 7000, 0),
        ("b", "live-one", 1, 0, 0, 0),
    ):
        labels = f'cluster="{cluster}",adx_env="shared",node_id="{node}"'
        for metric, value in (
            ("adx_coordinator_node_reachable", reachable),
            ("adx_coordinator_node_schedulable", admission),
            ("adx_coordinator_node_capacity_cpu_millis", cpu),
        ):
            sample(metric, labels, f"{value}+0x9")
        for state, value in (("Running", running), ("Paused", 0)):
            sample("adx_coordinator_environments", labels + f',state="{state}"', f"{value}+0x9")

    reachable = by_title["Reachable Nodes"]["targets"][0]["expr"]
    check(scope(reachable), [{"labels": "{}", "value": 2}])
    check(scope(reachable, "b"), [{"labels": "{}", "value": 1}])
    average = by_title["Average Running Environments per Node"]["targets"][0]["expr"]
    check(scope(average), [{"labels": "{}", "value": 2}])

    for kind, multiplier in (("log_records", 1), ("metric_points", 2), ("spans", 3)):
        # Identical labels across names reproduce the previous rate(selector) collision.
        sample("otelcol_receiver_refused_" + kind + "_total", 'cluster="a",adx_env="shared",receiver="otlp"', f"0+{multiplier}x24")
    for target, value in zip(by_title["Collector Refused / Enqueue Failed"]["targets"][:3], (0.06666666666666667, 0.13333333333333333, 0.19999999999999998), strict=True):
        check(scope(target["expr"]), [{"labels": '{cluster="a",adx_env="shared"}', "value": value}], "5m")

    labels = 'cluster="a",adx_env="shared",outcome="failure",stage="backend"'
    sample("adx_environment_create_errors_total", labels + ',reason="init"', "0 0 1+0x7")
    sample("adx_environment_create_errors_total", labels + ',reason="unused"', "0+0x9")
    errors = by_title["Creation Errors by Stage / Reason"]["targets"][0]["expr"]
    check(scope(errors), [], "15s")
    check(scope(errors), [{"labels": '{cluster="a",adx_env="shared",outcome="failure",stage="backend",reason="init"}', "value": 1}])

    assert not any(p["id"] in (25, 26, 40) for p in schedule["panels"])
    panel = by_title["Running and Paused Environments by Node"]
    assert panel["type"] == "table"
    assert all(t["instant"] and not t["range"] and t["format"] == "table" for t in panel["targets"])
    expected = []
    for cluster, node, value in (("a", "live-one", 2), ("a", "live-two", 2), ("a", "retired", 0), ("b", "live-one", 0)):
        labels = f'{cluster}/shared/{node}'
        expected.append({"labels": '{cluster="'+cluster+'",adx_env="shared",node_id="'+node+'",node_key="'+labels+'"}', "value": value})
    check(scope(panel["targets"][0]["expr"], ".+"), expected)

    plane = json.loads((DASHBOARDS / "adx-data-plane.json").read_text())
    by_id = {p["id"]: p for p in plane["panels"]}
    for cluster, value in (("a", 2), ("b", 7)):
        for cls in ("1xx", "2xx", "4xx"):
            sample("data_plane_ingress_http_responses_total", f'cluster="{cluster}",adx_env="shared",class="{cls}"', f"0 0 {value}+0x6")
        for metric, direction in (("data_plane_ingress_frontend_bytes_total", "received"), ("data_plane_ingress_frontend_bytes_total", "sent"), ("data_plane_relay_transfer_bytes_total", "to_runtime"), ("data_plane_relay_transfer_bytes_total", "from_runtime")):
            sample(metric, f'cluster="{cluster}",adx_env="shared",direction="{direction}"', "0 0 10 10 0 5 5 5 5")
    responses = by_id[2]
    assert responses["type"] == "table" and responses["targets"][0]["instant"]
    check(scope(responses["targets"][0]["expr"], ".+"), [
        {"labels": '{cluster="'+cluster+'",adx_env="shared",class="'+cls+'"}', "value": value}
        for cluster, value in (("a", 2), ("b", 7)) for cls in ("1xx", "2xx", "4xx")])
    for ident in (13, 14):
        for target in by_id[ident]["targets"]:
            check(scope(target["expr"], "a"), [{"labels": "{}", "value": 15}])
            check(scope(target["expr"], ".+"), [{"labels": "{}", "value": 30}])
    # No responses means undefined ratios, not a fabricated healthy 0 percent.
    check(scope(by_id[3]["targets"][0]["expr"], "absent"), [])

    fixture = {"rule_files": [], "evaluation_interval": "15s", "tests": [{"interval": "15s", "input_series": series, "promql_expr_test": checks}]}
    (output / "presentation.yaml").write_text(yaml.safe_dump(fixture, sort_keys=False))
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/promtool", "-v", str(output) + ":/tests:ro", "-w", "/tests", "prom/prometheus:v3.2.1", "test", "rules", "presentation.yaml"], check=True)
    print(f"PASS: {len(checks)} real query regressions for node counts, table rows and Collector names")


if __name__ == "__main__":
    main()
