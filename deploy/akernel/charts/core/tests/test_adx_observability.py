#!/usr/bin/env python3
"""Collection contracts for local node telemetry and isolated ADX roles."""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[5]
CHART = ROOT / "deploy/akernel/charts/core"


class ObservabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        result = subprocess.run(
            ["helm", "template", "akernel", str(CHART), "-n", "akernel",
             "--set", "monitoring.prometheusEndpoint=prometheus.akernel-monitor:9090",
             "--set", "monitoring.lokiEndpoint=loki.akernel-monitor:3100",
             "--set", "monitoring.tempoEndpoint=tempo.akernel-monitor:4317"],
            check=True, capture_output=True, text=True,
        )
        cls.resources = {
            (r["kind"], r["metadata"]["name"]): r
            for r in yaml.safe_load_all(result.stdout) if r
        }

    def test_node_scrapes_only_local_adx_targets(self) -> None:
        config = yaml.safe_load((CHART / "files/otel-collector/config.yaml").read_text())
        jobs = config["receivers"]["prometheus/adx"]["config"]["scrape_configs"]
        targets = {j["job_name"]: j["static_configs"][0]["targets"] for j in jobs}
        self.assertEqual(targets, {"adxlet": ["127.0.0.1:19091"],
                                   "adx-relay": ["127.0.0.1:18443"],
                                   "otel-collector": ["127.0.0.1:18889"]})
        self.assertIn("prometheus/adx", config["service"]["pipelines"]["metrics"]["receivers"])
        dockerfile = (ROOT / "builder/node.Dockerfile").read_text()
        self.assertIn("COPY ./deploy/akernel/charts/core/files/otel-collector/config.yaml "
                      "/etc/akernel/otel_config.yaml", dockerfile)

    def test_standalone_collects_all_local_control_and_data_metrics(self) -> None:
        config = yaml.safe_load((ROOT / "builder/config/otel-collector-standalone.yaml").read_text())
        jobs = config["receivers"]["prometheus/adx"]["config"]["scrape_configs"]
        targets = {job["job_name"]: job["static_configs"][0]["targets"] for job in jobs}
        self.assertEqual(targets, {
            "adxlet": ["127.0.0.1:19091"], "adx-relay": ["127.0.0.1:18443"],
            "adx-coordinator": ["127.0.0.1:19090"], "adx-apiserver": ["127.0.0.1:19093"],
            "adx-ingress": ["127.0.0.1:18080"], "otel-collector": ["127.0.0.1:18889"],
        })

    def test_standalone_overlay_only_replaces_scrape_jobs(self) -> None:
        overlay = yaml.safe_load((ROOT / "builder/config/otel-collector-standalone.yaml").read_text())
        self.assertEqual(set(overlay), {"receivers"})
        self.assertEqual(set(overlay["receivers"]), {"prometheus/adx"})
        self.assertEqual(set(overlay["receivers"]["prometheus/adx"]), {"config"})
        self.assertEqual(set(overlay["receivers"]["prometheus/adx"]["config"]), {"scrape_configs"})

    def test_ingress_collector_only_adds_api_scrape_to_control(self) -> None:
        data = self.resources[("ConfigMap", "akernel-otel-collector-config")]["data"]
        control = yaml.safe_load(data["control.yaml"])
        ingress = yaml.safe_load(data["ingress-api.yaml"])
        jobs = ingress["receivers"]["prometheus/adx"]["config"]["scrape_configs"]
        api = jobs.pop()
        self.assertEqual(api["job_name"], "adx-apiserver")
        self.assertEqual(api["static_configs"][0]["targets"], ["127.0.0.1:19093"])
        self.assertEqual(ingress, control)
        self.assertFalse(any(j["job_name"] == "adx-apiserver" for j in jobs))

    def test_adxlet_samples_the_sandboxd_resource_socket_for_both_clouds(self) -> None:
        configs = self.resources[("ConfigMap", "akernel-adx-config")]["data"]
        node = yaml.safe_load(configs["node.yaml"])
        adxlet = next(s for s in node["services"] if s["role"] == "adxlet")
        source = adxlet["config"]["resource_source"]
        self.assertEqual(source["kind"], "sandboxd")
        self.assertGreater(source["valid_for_seconds"], 0)
        self.assertNotIn("disk_path", source)
        sandboxd_configs = {
            "default": next(r["data"]["sandboxd_config.toml"]
                            for r in self.resources.values()
                            if "sandboxd_config.toml" in r.get("data", {})),
            **{provider: (ROOT / "deploy/terraform" / provider /
                          "values-akernel.yaml.tmpl").read_text()
               for provider in ("aliyun", "huaweicloud")},
        }
        for provider, config in sandboxd_configs.items():
            with self.subTest(provider=provider):
                section = config.split("[plugin.node_resource]", 1)[1]
                socket = re.search(r'^\s*sock_path\s*=\s*"([^"]+)"', section, re.MULTILINE)
                self.assertIsNotNone(socket)
                self.assertEqual(source["socket"], socket.group(1))

    def test_node_logs_include_runtime_and_structured_adx_records(self) -> None:
        config = yaml.safe_load((CHART / "files/otel-collector/config.yaml").read_text())
        receivers = config["receivers"]
        self.assertIn("filelog/runtime", config["service"]["pipelines"]["logs"]["receivers"])
        self.assertTrue(any(op["type"] == "json_parser" for op in receivers["filelog/adx"]["operators"]))
        attrs = config["processors"]["resource"]["attributes"]
        self.assertTrue(any(a["key"] == "adx_env" for a in attrs))

    def test_no_metrics_service_or_standalone_collector_deployment(self) -> None:
        names = [name for kind, name in self.resources if kind in ("Service", "Deployment")]
        self.assertFalse(any("metrics" in name or "collector" in name for name in names))
        node = self.resources[("DaemonSet", "akernel-node")]["spec"]["template"]["spec"]
        self.assertEqual([c["name"] for c in node["containers"]], ["akernel-node"])

    def test_control_roles_collect_locally_without_exposing_metrics_ports(self) -> None:
        for role in ("coordinator", "ingress-api"):
            spec = self.resources[("Deployment", f"akernel-adx-{role}")]["spec"]["template"]["spec"]
            collector = next(c for c in spec["containers"] if c["name"] == "otel-collector")
            self.assertEqual(collector["command"], ["/usr/local/bin/otelcol-contrib"])
            env = {e["name"]: e for e in collector["env"]}
            self.assertIn("POD_NAME", env)
            self.assertIn("NODE_NAME", env)
            mounts = {m["name"]: m for m in collector["volumeMounts"]}
            self.assertTrue(mounts["adx-logs"]["readOnly"])

    def test_deployment_keeps_native_metrics_and_trace_at_local_collector(self) -> None:
        data = self.resources[("ConfigMap", "akernel-adx-config")]["data"]
        for role in ("coordinator", "ingress-api", "node"):
            cfg = yaml.safe_load(data[f"{role}.yaml"])
            self.assertTrue(cfg["logging"]["line_records"])
            for service in cfg["services"]:
                self.assertEqual(service["env"]["ADX_LOG_FORMAT"], "json")
                self.assertEqual(service["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://127.0.0.1:4318")

    def test_control_collector_does_not_bind_the_apiserver_port(self) -> None:
        config = yaml.safe_load((CHART / "files/otel-collector/control.yaml").read_text())
        prometheus = config["service"]["telemetry"]["metrics"]["readers"][0]["pull"]["exporter"]["prometheus"]
        self.assertEqual(prometheus["host"], "127.0.0.1")
        self.assertNotEqual(prometheus["port"], 18888)

    def test_loki_indexes_dashboard_environment_and_component(self) -> None:
        config = yaml.safe_load((ROOT / "deploy/akernel/charts/monitor/files/loki/local-config.yaml").read_text())
        indexed = config["limits_config"]["otlp_config"]["resource_attributes"]["attributes_config"][0]["attributes"]
        self.assertIn("adx_env", indexed)
        self.assertIn("component_name", indexed)

    def test_custom_prometheus_claim_and_running_count_rule(self) -> None:
        result = subprocess.run(
            ["helm", "template", "akernel-monitor", str(ROOT / "deploy/akernel/charts/monitor"),
             "-n", "akernel-monitor", "--set", "prometheusServer.persistence.claimName=existing-data"],
            check=True, capture_output=True, text=True,
        )
        resources = {(r["kind"], r["metadata"]["name"]): r
                     for r in yaml.safe_load_all(result.stdout) if r}
        self.assertIn(("PersistentVolumeClaim", "existing-data"), resources)
        volumes = resources[("StatefulSet", "prometheus")]["spec"]["template"]["spec"]["volumes"]
        self.assertEqual(next(v for v in volumes if v["name"] == "prometheus-storage")["persistentVolumeClaim"]["claimName"], "existing-data")
        cm = resources[("ConfigMap", "prometheus-config")]["data"]
        self.assertIn("/etc/prometheus/adx-rules.yaml", cm["prometheus.yml"])
        rules = yaml.safe_load(cm["adx-rules.yaml"])["groups"][0]["rules"]
        self.assertEqual(rules[0]["record"], "sandbox_running")
        self.assertIn('state="Running"', rules[0]["expr"])

    def test_primary_collectors_exclude_instance_resource_samples(self) -> None:
        configs = self.resources[("ConfigMap", "akernel-otel-collector-config")]["data"]
        for name, content in configs.items():
            with self.subTest(name=name):
                config = yaml.safe_load(content)
                pipeline = config["service"]["pipelines"]["metrics"]
                self.assertIn("filter/summary_metrics", pipeline["processors"])
                conditions = config["processors"]["filter/summary_metrics"]["metrics"]["datapoint"]
                for identity in ("environment_id", "sandbox_id"):
                    self.assertIn(f'attributes["{identity}"] != nil', conditions)
                    self.assertIn(f'resource.attributes["{identity}"] != nil', conditions)
                for signal in ("logs", "traces"):
                    self.assertNotIn("filter/summary_metrics",
                                     config["service"]["pipelines"][signal]["processors"])

    def test_existing_detail_dashboard_url_uses_shared_prometheus(self) -> None:
        dashboards = ROOT / "deploy/akernel/charts/monitor/dashboards"
        alias = json.loads((dashboards / "sandbox-lifecycle-dashboard.json").read_text())
        native = json.loads((dashboards / "adx-environments.json").read_text())
        self.assertEqual(alias["uid"], "sandbox-metrics")
        self.assertEqual(alias["panels"], native["panels"])
        self.assertEqual(alias["templating"], native["templating"])

    def test_latest_master_metrics_contract(self) -> None:
        data = self.resources[("ConfigMap", "akernel-adx-config")]["data"]
        api = yaml.safe_load(data["ingress-api.yaml"])["services"][0]
        self.assertEqual(api["config"]["metrics_listen"], "127.0.0.1:19093")
        node = yaml.safe_load(data["node.yaml"])["services"][0]
        self.assertEqual(node["config"]["metrics_interval_seconds"], 15)
        config = yaml.safe_load(self.resources[("ConfigMap", "akernel-otel-collector-config")]["data"]["ingress-api.yaml"])
        jobs = config["receivers"]["prometheus/adx"]["config"]["scrape_configs"]
        self.assertTrue(any(j["job_name"] == "adx-apiserver" for j in jobs))
        self.assertTrue(any(j["job_name"] == "otel-collector" for j in jobs))

    def test_metric_sampling_rejects_disabled_or_invalid_intervals(self) -> None:
        for interval in ("0", "-1", "1.5", "bad", "3601"):
            with self.subTest(interval=interval):
                result = subprocess.run(
                    ["helm", "template", "akernel", str(CHART), "--set-string",
                     f"adx.node.metricsIntervalSeconds={interval}"],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("metricsIntervalSeconds must be an integer", result.stderr)

    def test_default_detail_dashboards_resolve_existing_datasources(self) -> None:
        result = subprocess.run(
            ["helm", "template", "monitor", str(ROOT / "deploy/akernel/charts/monitor")],
            check=True, capture_output=True, text=True,
        )
        resources = {(r["kind"], r["metadata"]["name"]): r
                     for r in yaml.safe_load_all(result.stdout) if r}
        data = resources[("ConfigMap", "grafana-datasources")]["data"]
        sources = {d["uid"] for content in data.values()
                   for d in yaml.safe_load(content)["datasources"]}
        dashboards = resources[("ConfigMap", "grafana-dashboards")]["data"]
        for name in ("adx-environments.json", "sandbox-lifecycle-dashboard.json"):
            dashboard = json.loads(dashboards[name])
            for panel in dashboard["panels"]:
                self.assertEqual(panel["datasource"]["uid"], "prometheus", name)
                self.assertIn(panel["datasource"]["uid"], sources, name)
            for variable in dashboard["templating"]["list"]:
                if "datasource" in variable:
                    self.assertIn(variable["datasource"]["uid"], sources, name)
        config = yaml.safe_load(resources[("ConfigMap", "prometheus-config")]["data"]["prometheus.yml"])
        self.assertFalse(any(j.get("metrics_path") == "/metrics/environments"
                             for j in config["scrape_configs"]))

    def test_optional_details_share_existing_prometheus_and_pod_settings(self) -> None:
        selector = ("affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution."
                    "nodeSelectorTerms[0].matchExpressions[0]")
        args = ["helm", "template", "monitor", str(ROOT / "deploy/akernel/charts/monitor"),
                "--set", "imagePullSecrets[0].name=registry-auth",
                "--set", "prometheusServer.image.repository=registry.example/prometheus",
                "--set", "tolerations[0].key=monitoring",
                "--set", "tolerations[0].operator=Exists",
                "--set", f"{selector}.key=workload",
                "--set", f"{selector}.operator=In",
                "--set", f"{selector}.values[0]=monitoring"]

        def render(extra: list[str]) -> dict:
            result = subprocess.run(args + extra, check=True, capture_output=True, text=True)
            return {(r["kind"], r["metadata"]["name"]): r
                    for r in yaml.safe_load_all(result.stdout) if r}
        baseline = render([])
        enabled = render(["--set", "environmentDetails.enabled=true",
                          "--set", "environmentDetails.environment=prod",
                          "--set", "environmentDetails.namespaces[0]=workers",
                          "--set", "environmentDetails.scrapeInterval=45s",
                          "--set", "prometheusServer.config.clusterName=cluster-west"])
        self.assertEqual(set(enabled), set(baseline))
        pod = enabled[("StatefulSet", "prometheus")]["spec"]["template"]["spec"]
        self.assertEqual(pod, baseline[("StatefulSet", "prometheus")]["spec"]["template"]["spec"])
        self.assertEqual(pod["imagePullSecrets"], [{"name": "registry-auth"}])
        self.assertEqual(pod["tolerations"], [{"key": "monitoring", "operator": "Exists"}])
        self.assertIn("nodeAffinity", pod["affinity"])
        config = yaml.safe_load(enabled[("ConfigMap", "prometheus-config")]["data"]["prometheus.yml"])
        jobs = [j for j in config["scrape_configs"] if j.get("metrics_path") == "/metrics/environments"]
        self.assertEqual({j["job_name"] for j in jobs}, {"adx-coordinator-environments", "adx-node-environments"})
        for job in jobs:
            self.assertEqual(job["scrape_interval"], "45s")
            self.assertEqual(job["sample_limit"], 200000)
            self.assertEqual(job["kubernetes_sd_configs"][0]["namespaces"]["names"], ["workers"])
            labels = {r["target_label"]: r["replacement"]
                      for r in job["relabel_configs"] if "replacement" in r}
            self.assertEqual(labels["adx_env"], "prod")
            self.assertEqual(labels["cluster"], "cluster-west")
        rules = yaml.safe_load(enabled[("ConfigMap", "prometheus-config")]["data"]["adx-rules.yaml"])
        records = {r["record"] for g in rules["groups"] for r in g["rules"]}
        self.assertIn("adx:environment_created_qps:rate1m", records)
        self.assertNotIn("adx:environment_create_request_qps:rate1m", records)

    def test_dashboard_uids_and_environment_contract(self) -> None:
        dashboard_dir = ROOT / "deploy/akernel/charts/monitor/dashboards"
        for name in ("schedule", "data-plane", "process-resources", "environment-logs", "environments"):
            p = dashboard_dir / f"adx-{name}.json"
            dashboard = json.loads(p.read_text())
            self.assertEqual(dashboard["uid"], f"adx-{name}")
            self.assertIn("adx_env", p.read_text())

    def test_sandbox_lifecycle_dashboard_uses_summary_metrics_and_scoped_queries(self) -> None:
        dashboard_dir = ROOT / "deploy/akernel/charts/monitor/dashboards"
        dashboard = json.loads((dashboard_dir / "adx-sandbox-lifecycle.json").read_text())
        self.assertEqual(dashboard["uid"], "adx-sandbox-lifecycle")
        variables = {v["name"] for v in dashboard["templating"]["list"]}
        self.assertTrue({"cluster", "adx_env"}.issubset(variables))
        panels = {p["title"]: p for p in dashboard["panels"]}
        p95 = panels["Successful New-Create Call P95"]["targets"][0]["expr"]
        self.assertIn("adx_environment_create_success_call_duration_seconds_bucket", p95)
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                self.assertEqual(panel["datasource"]["uid"], "prometheus")
                self.assertIn('cluster=~"$cluster"', target["expr"])
                self.assertIn('adx_env=~"$adx_env"', target["expr"])
        rendered = subprocess.run(
            ["helm", "template", "monitor", str(ROOT / "deploy/akernel/charts/monitor")],
            check=True, capture_output=True, text=True,
        )
        resources = {(r["kind"], r["metadata"]["name"]): r
                     for r in yaml.safe_load_all(rendered.stdout) if r}
        provisioned = resources[("ConfigMap", "grafana-dashboards")]["data"]
        self.assertEqual(json.loads(provisioned["adx-sandbox-lifecycle.json"]), dashboard)

    def test_cluster_identity_reaches_all_role_collectors(self) -> None:
        rendered = subprocess.run(
            ["helm", "template", "akernel", str(CHART),
             "--set", "monitoring.clusterName=cluster-west",
             "--set", "monitoring.akernelEnv=prod",
             "--set", "monitoring.prometheusEndpoint=prometheus:9090"],
            check=True, capture_output=True, text=True,
        )
        resources = {(r["kind"], r["metadata"]["name"]): r
                     for r in yaml.safe_load_all(rendered.stdout) if r}
        for kind, name, container_name in (
            ("DaemonSet", "akernel-node", "akernel-node"),
            ("Deployment", "akernel-adx-coordinator", "otel-collector"),
            ("Deployment", "akernel-adx-ingress-api", "otel-collector"),
        ):
            containers = resources[(kind, name)]["spec"]["template"]["spec"]["containers"]
            container = next(c for c in containers if c["name"] == container_name)
            env = {e["name"]: e.get("value") for e in container["env"]}
            self.assertEqual(env.get("AKERNEL_CLUSTER_NAME"), "cluster-west", name)
            self.assertEqual(env.get("AKERNEL_ENV"), "prod", name)
        for name, content in resources[("ConfigMap", "akernel-otel-collector-config")]["data"].items():
            attributes = yaml.safe_load(content)["processors"]["resource"]["attributes"]
            identity = {a["key"]: a for a in attributes}
            self.assertEqual(identity["cluster"]["value"], "${env:AKERNEL_CLUSTER_NAME:-standalone}", name)
            self.assertEqual(identity["cluster"]["action"], "upsert", name)

    def test_standalone_imports_cluster_identity_for_systemd_collector(self) -> None:
        launcher = (ROOT / "deploy/standalone/start.sh").read_text()
        wrapper = (ROOT / "builder/scripts/otel-collector-service.sh").read_text()
        self.assertIn("AKERNEL_CLUSTER_NAME", launcher)
        self.assertIn("AKERNEL_CLUSTER_NAME=*", wrapper)

    def test_cloud_templates_use_cluster_identity_and_collector_host_ports(self) -> None:
        for provider in ("aliyun", "huaweicloud"):
            with self.subTest(provider=provider):
                directory = ROOT / "deploy/terraform" / provider
                core = (directory / "values-akernel.yaml.tmpl").read_text()
                monitor = (directory / "values-monitor.yaml.tmpl").read_text()
                self.assertIn('clusterName: "${cluster_name}"', core)
                self.assertIn('clusterName: "${cluster_name}"', monitor)
                self.assertIn('tempoEndpoint: "tempo.${monitor_namespace}.svc:4317"', core)
                for key in ("prometheusEndpoint", "lokiEndpoint", "tempoEndpoint"):
                    value = re.search(rf'{key}:\s*"([^"]+)"', core).group(1)
                    self.assertNotIn("://", value)


if __name__ == "__main__":
    unittest.main()
