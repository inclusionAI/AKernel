#!/usr/bin/env python3

"""Rendered-contract tests for the default ADX Kubernetes deployment."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]


def render() -> tuple[list[dict], str]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "akernel",
            str(CHART),
            "--namespace",
            "akernel-system",
            "--set",
            "adx.tls.existingSecret=adx-test-tls",
            "--set",
            "traefik.enabled=true",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [item for item in yaml.safe_load_all(result.stdout) if item], result.stdout


class AdxChartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resources, cls.rendered = render()

    def resource(self, kind: str, name: str) -> dict:
        for resource in self.resources:
            if resource.get("kind") == kind and resource["metadata"]["name"] == name:
                return resource
        self.fail(f"missing {kind}/{name}")

    def test_uses_current_adx_role_names(self) -> None:
        config = self.resource("ConfigMap", "akernel-adx-config")["data"]
        self.assertIn("coordinator.yaml", config)
        self.assertIn("ingress-api.yaml", config)
        self.assertIn("role: coordinator", config["coordinator.yaml"])
        self.assertIn("role: apiserver", config["ingress-api.yaml"])
        self.assertIn("role: ingress", config["ingress-api.yaml"])
        self.assertIn("role: adxlet", config["node.yaml"])
        self.assertIn("ADX_EXECD_CONTROL_SOCKET_PATH", config["node.yaml"])
        self.assertIn("ADX_DATA_PLANE_RELAY_BIND", config["node.yaml"])
        self.assertIn(
            "ADX_DATA_PLANE_ALLOWED_INGRESS_CIDRS", config["node.yaml"]
        )
        self.assertIn("/__adx/usr/local/bin/adx-execd", config["node.yaml"])
        self.assertNotIn("role: master", self.rendered)
        self.assertNotIn("role: node-manager", self.rendered)
        self.assertNotIn("role: api-server", self.rendered)
        self.assertNotIn("role: edge", self.rendered)

    def test_default_replaces_legacy_control_plane(self) -> None:
        self.resource("StatefulSet", "akernel-adx-redis")
        self.resource("Deployment", "akernel-adx-coordinator")
        self.resource("Service", "akernel-adx-coordinator")
        self.resource("Deployment", "akernel-adx-ingress-api")
        self.resource("Service", "akernel-adx-ingress-api")
        self.resource("DaemonSet", "akernel-node")
        names = {item["metadata"]["name"] for item in self.resources}
        self.assertNotIn("akernel-master", names)
        self.assertNotIn("akernel-frontend", names)
        self.assertNotIn("akernel-etcd", names)

    def test_coordinator_and_ingress_api_preserve_control_image_override(self) -> None:
        result = subprocess.run(
            [
                "helm",
                "template",
                "akernel",
                str(CHART),
                "--set",
                "coordinator.image.repository=example.test/control,"
                "coordinator.image.tag=release",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        resources = [item for item in yaml.safe_load_all(result.stdout) if item]
        for name in ("akernel-adx-coordinator", "akernel-adx-ingress-api"):
            deployment = next(item for item in resources
                              if item["kind"] == "Deployment"
                              and item["metadata"]["name"] == name)
            image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
            self.assertEqual(image, "example.test/control:release")

    def test_coordinator_and_ingress_api_have_independent_adxctl_parents(self) -> None:
        config = self.resource("ConfigMap", "akernel-adx-config")["data"]
        coordinator = yaml.safe_load(config["coordinator.yaml"])
        ingress_api = yaml.safe_load(config["ingress-api.yaml"])
        self.assertEqual(
            [item["role"] for item in coordinator["services"]], ["coordinator"]
        )
        self.assertEqual(
            [item["role"] for item in ingress_api["services"]], ["apiserver", "ingress"]
        )
        expected = {
            "akernel-adx-coordinator": "/etc/akernel/adx-coordinator.yaml",
            "akernel-adx-ingress-api": "/etc/akernel/adx-ingress-api.yaml",
        }
        for name, path in expected.items():
            container = self.resource("Deployment", name)["spec"]["template"]["spec"]
            container = container["containers"][0]
            self.assertEqual(container["command"], ["/opt/adx/current/bin/adxctl"])
            self.assertEqual(container["args"], ["--config", path, "run"])

    def test_node_migrates_only_retired_adx_supervisor_state(self) -> None:
        pod = self.resource("DaemonSet", "akernel-node")["spec"]["template"]["spec"]
        migration = next(
            item for item in pod["initContainers"] if item["name"] == "migrate-adx-state"
        )
        command = migration["command"][2]
        self.assertIn("/home/akernel/adx/run/control", command)
        self.assertIn("/home/akernel/adx/run/config-[0-9]*", command)
        self.assertNotIn("/home/akernel/adx/checkpoints", command)
        self.assertNotIn("/home/akernel/adx/degraded", command)
        self.assertEqual(
            migration["volumeMounts"],
            [{"mountPath": "/home/akernel", "name": "home-disk"}],
        )

    def test_node_collector_reads_only_the_node_supervisor_logs(self) -> None:
        result = subprocess.run(
            [
                "helm",
                "template",
                "akernel",
                str(CHART),
                "--set",
                "monitoring.lokiEndpoint=loki:3100",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        resources = [item for item in yaml.safe_load_all(result.stdout) if item]
        config = next(
            item
            for item in resources
            if item["kind"] == "ConfigMap"
            and item["metadata"]["name"] == "akernel-otel-collector-config"
        )
        collector = config["data"]["otel_config.yaml"]
        self.assertIn("/home/akernel/adx/run/node/logs/*.log", collector)
        self.assertNotIn("/home/akernel/adx/run/control", collector)
        self.assertNotIn("/home/akernel/adx/run/logs/*.log", collector)

    def test_retired_templates_are_removed(self) -> None:
        for directory in ("etcd", "frontend", "master"):
            self.assertFalse((CHART / "templates" / directory).exists())
        self.assertNotIn("ETCD_ADDRESS", self.rendered)
        self.assertNotIn("YR_IMAGE_PROCESS_CONFIG", self.rendered)
        static = self.resource("ConfigMap", "traefik-static")["data"]["traefik.yml"]
        self.assertIn("file:", static)

    def test_internal_rpc_uses_network_identity_without_node_certificates(self) -> None:
        config = self.resource("ConfigMap", "akernel-adx-config")["data"]
        self.assertNotIn("node-pool:", config["coordinator.yaml"])
        self.assertIn("mode: network", config["coordinator.yaml"])
        self.assertIn("internal_security: network", config["ingress-api.yaml"])
        self.assertIn("mode: network", config["node.yaml"])
        self.assertNotIn(
            ".der",
            config["coordinator.yaml"]
            + config["ingress-api.yaml"]
            + config["node.yaml"],
        )
        self.assertIn(
            "state_dir: /home/akernel/adx/run/coordinator", config["coordinator.yaml"]
        )
        self.assertIn(
            "state_dir: /home/akernel/adx/run/ingress-api", config["ingress-api.yaml"]
        )
        self.assertIn("state_dir: /home/akernel/adx/run/node", config["node.yaml"])
        self.assertIn("node_id: ${NODE_NAME}", config["node.yaml"])
        self.assertIn("advertised_address: ${INSTANCE_IP}:19001", config["node.yaml"])
        daemonset = self.resource("DaemonSet", "akernel-node")
        container = daemonset["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        self.assertNotIn("AKERNEL_CONTROL_PLANE", env)
        self.assertEqual(env["AKERNEL_ADX_CONFIG"], "/etc/akernel/adx-node.yaml")

        volumes = {
            item["name"]: item
            for item in daemonset["spec"]["template"]["spec"]["volumes"]
        }
        self.assertNotIn("adx-credentials", volumes)
        coordinator = self.resource("Deployment", "akernel-adx-coordinator")
        coordinator_secret = next(
            v["secret"] for v in coordinator["spec"]["template"]["spec"]["volumes"]
            if v["name"] == "credentials"
        )
        self.assertEqual(
            {i["key"] for i in coordinator_secret["items"]}, {"admin-key"}
        )
        ingress_api = self.resource("Deployment", "akernel-adx-ingress-api")
        ingress_secret = next(
            v["secret"] for v in ingress_api["spec"]["template"]["spec"]["volumes"]
            if v["name"] == "credentials"
        )
        self.assertEqual(
            {i["key"] for i in ingress_secret["items"]}, {"public.pem", "public.key"}
        )

    def test_ingress_api_keeps_control_and_data_ports_separate(self) -> None:
        dynamic = self.resource("ConfigMap", "traefik-dynamic")["data"]["config.yml"]
        self.assertIn('url: "https://akernel-adx-ingress-api:8443"', dynamic)
        self.assertIn('url: "http://akernel-adx-ingress-api:8080"', dynamic)
        self.assertIn("- websecure", dynamic)
        self.assertIn("- web", dynamic)
        self.assertNotIn("akernel-frontend:8888", dynamic)

        service = self.resource("Service", "akernel-adx-ingress-api")
        ports = {port["name"]: port["port"] for port in service["spec"]["ports"]}
        self.assertEqual(ports["control"], 8443)
        self.assertEqual(ports["data"], 8080)

    def test_adx_keeps_both_ports_when_legacy_single_entry_is_disabled(self) -> None:
        result = subprocess.run(
            [
                "helm",
                "template",
                "akernel",
                str(CHART),
                "--namespace",
                "akernel-system",
                "--set",
                "adx.tls.existingSecret=adx-test-tls",
                "--set",
                "traefik.enabled=true",
                "--set",
                "traefik.enableWebEntrypoint=false",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        resources = [item for item in yaml.safe_load_all(result.stdout) if item]
        traefik = next(
            item
            for item in resources
            if item["kind"] == "Service" and item["metadata"]["name"] == "traefik"
        )
        ports = {port["name"] for port in traefik["spec"]["ports"]}
        self.assertIn("websecure", ports)
        self.assertIn("web", ports)

    def test_redis_is_single_member_aof_with_persistent_storage(self) -> None:
        redis = self.resource("StatefulSet", "akernel-adx-redis")
        self.assertEqual(redis["spec"]["replicas"], 1)
        container = redis["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("--appendonly", container["args"])
        self.assertIn("everysec", container["args"])
        claims = redis["spec"]["volumeClaimTemplates"]
        self.assertEqual(claims[0]["metadata"]["name"], "data")

    def test_managed_redis_accepts_only_control_and_node_pods(self) -> None:
        redis = self.resource("StatefulSet", "akernel-adx-redis")
        args = redis["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("--protected-mode", args)
        self.assertEqual(args[args.index("--protected-mode") + 1], "no")
        policy = self.resource("NetworkPolicy", "akernel-adx-redis")
        self.assertEqual(policy["spec"]["podSelector"]["matchLabels"], {"app": "akernel-adx-redis"})
        rule = policy["spec"]["ingress"][0]
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": 6379}])
        self.assertEqual(
            rule["from"],
            [{"podSelector": {"matchExpressions": [
                {"key": "app", "operator": "In", "values": [
                    "akernel-adx-coordinator", "akernel-adx-ingress-api", "node"
                ]}
            ]}}],
        )

    def test_managed_redis_requires_a_separate_secret(self) -> None:
        import base64

        secret = self.resource("Secret", "akernel-adx-redis-auth")
        password = base64.b64decode(secret["data"]["password"]).decode()
        self.assertRegex(password, r"^[A-Za-z0-9]{64}$")
        redis = self.resource("StatefulSet", "akernel-adx-redis")["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("--requirepass", " ".join(redis["command"]))
        self.assertEqual(redis["env"][0]["valueFrom"]["secretKeyRef"],
                         {"name": "akernel-adx-redis-auth", "key": "password"})
        for kind, name in (
            ("Deployment", "akernel-adx-coordinator"),
            ("Deployment", "akernel-adx-ingress-api"),
            ("DaemonSet", "akernel-node"),
        ):
            env = self.resource(kind, name)["spec"]["template"]["spec"]["containers"][0]["env"]
            auth = next(i for i, item in enumerate(env) if item["name"] == "ADX_REDIS_PASSWORD")
            url = next(i for i, item in enumerate(env) if item["name"] == "ADX_REDIS_URL")
            self.assertLess(auth, url)
            self.assertEqual(env[auth]["valueFrom"]["secretKeyRef"],
                             {"name": "akernel-adx-redis-auth", "key": "password"})
            self.assertIn(":$(ADX_REDIS_PASSWORD)@", env[url]["value"])

    def test_coordinator_and_ingress_api_have_independent_probes(self) -> None:
        coordinator = self.resource("Deployment", "akernel-adx-coordinator")
        container = coordinator["spec"]["template"]["spec"]["containers"][0]
        for probe in ("readinessProbe", "livenessProbe"):
            command = " ".join(container[probe]["exec"]["command"])
            self.assertIn("/dev/tcp/127.0.0.1/19000", command)
            self.assertNotIn("18080", command)
        ingress_api = self.resource("Deployment", "akernel-adx-ingress-api")
        container = ingress_api["spec"]["template"]["spec"]["containers"][0]
        for probe in ("readinessProbe", "livenessProbe"):
            self.assertEqual(
                container[probe]["httpGet"], {"path": "/healthz", "port": "health"}
            )

    def test_control_generated_state_is_reset_on_container_restart(self) -> None:
        for name in ("akernel-adx-coordinator", "akernel-adx-ingress-api"):
            pod = self.resource("Deployment", name)["spec"]["template"]["spec"]
            mounts = pod["containers"][0]["volumeMounts"]
            self.assertNotIn("/home/akernel", [item["mountPath"] for item in mounts])

    def test_external_redis_uses_a_secret_and_omits_managed_redis(self) -> None:
        result = subprocess.run(
            [
                "helm",
                "template",
                "akernel",
                str(CHART),
                "--namespace",
                "akernel-system",
                "--set",
                "adx.redis.mode=external",
                "--set",
                "adx.redis.external.existingSecret=external-redis",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        resources = [item for item in yaml.safe_load_all(result.stdout) if item]
        names = {item["metadata"]["name"] for item in resources}
        self.assertNotIn("akernel-adx-redis", names)
        deployments = [
            item for item in resources
            if item["kind"] == "Deployment"
            and item["metadata"]["name"]
            in {"akernel-adx-coordinator", "akernel-adx-ingress-api"}
        ]
        self.assertEqual(len(deployments), 2)
        node = next(item for item in resources if item["kind"] == "DaemonSet")
        for workload in (*deployments, node):
            environment = workload["spec"]["template"]["spec"]["containers"][0]["env"]
            redis = next(
                item for item in environment if item["name"] == "ADX_REDIS_URL"
            )
            self.assertEqual(
                redis["valueFrom"]["secretKeyRef"],
                {"name": "external-redis", "key": "redis-url"},
            )

    def test_external_redis_requires_a_secret(self) -> None:
        result = subprocess.run(
            [
                "helm",
                "template",
                "akernel",
                str(CHART),
                "--set",
                "adx.redis.mode=external",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("adx.redis.external.existingSecret is required", result.stderr)

    def test_adx_mode_does_not_render_unused_legacy_control_secrets(self) -> None:
        names = {item["metadata"]["name"] for item in self.resources}
        self.assertNotIn("akernel-component-tls", names)
        self.assertNotIn("akernel-master-secret", names)

    def test_multiple_coordinator_replicas_are_rejected(self) -> None:
        result = subprocess.run(
            [
                "helm",
                "template",
                "akernel",
                str(CHART),
                "--set",
                "adx.coordinator.replicas=2",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("adx.coordinator.replicas must be 1", result.stderr)


if __name__ == "__main__":
    unittest.main()
