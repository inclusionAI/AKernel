"""Identity and display contracts for shared ADX observability backends."""
from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import yaml

ROOT = Path(__file__).resolve().parents[5]
MONITOR = ROOT / "deploy/akernel/charts/monitor"


class MultiClusterTest(unittest.TestCase):
    def test_dashboard_queries_and_navigation_preserve_cluster(self) -> None:
        paths = list((MONITOR / "dashboards").glob("adx-*.json"))
        paths.append(MONITOR / "dashboards/sandbox-lifecycle-dashboard.json")
        for path in paths:
            dashboard = json.loads(path.read_text())
            variables = {v["name"]: v for v in dashboard["templating"]["list"]}
            with self.subTest(dashboard=path.name):
                self.assertIn("cluster", variables)
                self.assertIn('$cluster', json.dumps(variables["adx_env"]))
            for panel in dashboard["panels"]:
                for target in panel.get("targets", []):
                    query = target.get("expr", target.get("query", ""))
                    if "$adx_env" not in query:
                        continue
                    with self.subTest(dashboard=path.name, panel=panel["title"], query=query):
                        if target.get("queryType") == "traceql":
                            self.assertIn('resource.cluster = "${cluster:raw}"', query)
                            self.assertIn('"${cluster:raw}" = "..*"', query)
                        else:
                            self.assertIn('cluster=~"$cluster"', query)
                        for labels in re.findall(r'\bon\s*\(([^)]*)\)', query):
                            self.assertIn("cluster", {label.strip() for label in labels.split(",")})
                        for labels in re.findall(r'\bby\s*\(([^)]*)\)', query):
                            if any(k in labels for k in ("environment_id", "node_id", "adx_env")):
                                self.assertIn("cluster", labels)
                        if "node_key" in query:
                            self.assertIn('"node_key", "/", "cluster", "adx_env", "node_id"', query)
            for link in dashboard.get("links", []):
                if "/explore?" in link.get("url", ""):
                    for value in ("cluster-a", "cluster.a", "..*"):
                        url = link["url"].replace("${cluster:percentencode}", value if value == "..*" else quote(value, safe=""))
                        url = url.replace("${adx_env:percentencode}", "shared")
                        panes = json.loads(parse_qs(urlparse(url).query)["panes"][0])
                        for pane in panes.values():
                            query = pane["queries"][0]["query"]
                            self.assertEqual(query.count("resource.cluster"), 1)
                            self.assertIn(f'resource.cluster = "{value}"', query)
                            self.assertIn(f'"{value}" = "..*"', query)
                elif link.get("url", "").startswith("/d/"):
                    self.assertTrue(link.get("includeVars"), link)

    def test_loki_cluster_is_queryable(self) -> None:
        config = yaml.safe_load((MONITOR / "files/loki/local-config.yaml").read_text())
        attrs = config["limits_config"]["otlp_config"]["resource_attributes"]["attributes_config"]
        self.assertIn("cluster", next(a["attributes"] for a in attrs if a["action"] == "index_label"))

    def test_statistics_started_at_converts_seconds_for_grafana(self) -> None:
        dashboard = json.loads((MONITOR / "dashboards/adx-sandbox-lifecycle.json").read_text())
        panel = next(p for p in dashboard["panels"] if p["title"] == "Statistics Started At")
        self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "dateTimeAsIso")
        self.assertRegex(panel["targets"][0]["expr"], r'adx_observability_started_timestamp_seconds\{[^}]+\}\s*\*\s*1000$')
        # Grafana interprets this unit as milliseconds since the Unix epoch.
        milliseconds = 1793059200 * 1000
        self.assertEqual(datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(),
                         "2026-10-27T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
