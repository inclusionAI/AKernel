"""Render and check security boundaries without a cluster."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

CHART = Path(__file__).resolve().parents[1] / "chart"
HELM = shutil.which("helm") or str(Path.home() / ".local/bin/helm")


class ChartTests(unittest.TestCase):
    def values(self):
        return {"image": "registry.example/check@sha256:" + "a" * 64, "registry": {"url": "https://registry.example", "allowedAuthOrigins": ["https://auth.example"], "probeRepository": "operations/nydus", "probeDigest": "sha256:" + "b" * 64, "pullSecret": "probe-pull"}, "networkPolicy": {"enabled": True, "existingClientsReviewed": True}, "dragonfly": {"proxyLoggingVerified": True, "proxyCAFile": "/trust/dragonfly-ca.pem"}}

    def render(self, values):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "values.json"
            path.write_text(json.dumps(values))
            return subprocess.run([HELM, "template", "guard", str(CHART), "--namespace", "registry", "-f", str(path)], text=True, capture_output=True)

    def objects(self):
        result = self.render(self.values())
        self.assertEqual(result.returncode, 0, result.stderr)
        return list(yaml.safe_load_all(result.stdout))

    def test_namespace_and_pod_labels_share_one_peer_and_only_data_auth_ports(self):
        policy = next(o for o in self.objects() if o["kind"] == "NetworkPolicy")
        rule = policy["spec"]["ingress"][0]
        self.assertEqual(len(rule["from"]), 1)
        self.assertEqual(rule["from"][0], {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "dragonfly-system"}}, "podSelector": {"matchLabels": {"app": "dragonfly", "component": "seed-client"}}})
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": 5000}, {"protocol": "TCP", "port": 5001}])

    def test_exec_restricted_to_exact_five_seeds_and_no_secret_read_permission(self):
        roles = [o for o in self.objects() if o["kind"] == "Role"]
        rules = [r for role in roles for r in role["rules"]]
        self.assertFalse(any("secrets" in r["resources"] for r in rules))
        execution = next(r for r in rules if "pods/exec" in r["resources"])
        self.assertEqual(execution["resourceNames"], [f"dragonfly-seed-client-{i}" for i in range(5)])

    def test_checker_is_bounded_nonroot_and_cannot_overlap_scheduled_checks(self):
        cron = next(o for o in self.objects() if o["kind"] == "CronJob")
        self.assertEqual(cron["spec"]["schedule"], "* * * * *")
        self.assertEqual(cron["spec"]["concurrencyPolicy"], "Forbid")
        job = cron["spec"]["jobTemplate"]["spec"]
        self.assertEqual(job["activeDeadlineSeconds"], 240)
        pod = job["template"]["spec"]
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
        secret = next(v for v in pod["volumes"] if "secret" in v)
        self.assertEqual(secret["secret"]["secretName"], "probe-pull")

    def test_policy_is_opt_in_and_requires_existing_client_review(self):
        values = self.values()
        values.pop("networkPolicy")
        result = self.render(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(o and o["kind"] == "NetworkPolicy" for o in yaml.safe_load_all(result.stdout)))
        values["networkPolicy"] = {"enabled": True}
        self.assertNotEqual(self.render(values).returncode, 0)

    def test_empty_or_invalid_port_lists_are_rejected(self):
        for ports in [[], None, [0], [65536], ["all"], [-1]]:
            values = self.values()
            values["registry"]["ports"] = ports
            self.assertNotEqual(self.render(values).returncode, 0, repr(ports))

    def test_state_configmap_does_not_own_job_written_data(self):
        state = next(o for o in self.objects() if o["kind"] == "ConfigMap" and o["metadata"]["name"] == "guard-state")
        self.assertNotIn("data", state)

    def test_proxy_credential_logging_and_https_ca_are_explicit(self):
        values = self.values()
        values["dragonfly"]["proxyLoggingVerified"] = False
        self.assertNotEqual(self.render(values).returncode, 0)
        values["dragonfly"]["proxyLoggingVerified"] = True
        values["dragonfly"]["proxyCAFile"] = ""
        self.assertNotEqual(self.render(values).returncode, 0)
        values["dragonfly"]["proxyCheckEnabled"] = False
        self.assertEqual(self.render(values).returncode, 0)

    def test_chart_does_not_take_over_registry_or_storage(self):
        kinds = {o["kind"] for o in self.objects()}
        self.assertFalse(kinds.intersection({"Deployment", "PersistentVolumeClaim", "Service", "Secret"}))

    def test_empty_selectors_or_auth_origins_and_unpinned_image_fail_closed(self):
        for field in ["podSelector", "allowedAuthOrigins"]:
            values = self.values()
            values["registry"][field] = None
            self.assertNotEqual(self.render(values).returncode, 0)
        values = self.values()
        values["image"] = "registry.example/check:latest"
        self.assertNotEqual(self.render(values).returncode, 0)


if __name__ == "__main__":
    unittest.main()
