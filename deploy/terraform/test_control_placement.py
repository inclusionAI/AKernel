#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Offline regression coverage for legacy, new and reused control placement."""

import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = os.environ.get("AKERNEL_TEST_TERRAFORM") or shutil.which("terraform")
CHART = ROOT / "deploy/akernel/charts/core"
CONTROLS = ("akernel-adx-redis", "akernel-adx-coordinator", "akernel-adx-ingress-api")
REUSE = {"node_selector": {"node-role": "existing-control"},
         "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "master", "effect": "NoSchedule"}]}


def render(values, chart=CHART):
    result = subprocess.run(
        ["helm", "template", "akernel", str(chart), "-f", "-"],
        input=yaml.safe_dump(values), text=True, capture_output=True, check=True,
    )
    return {item["metadata"]["name"]: item["spec"]["template"]["spec"]
            for item in yaml.safe_load_all(result.stdout)
            if item and item.get("kind") in ("Deployment", "StatefulSet", "DaemonSet")}


class ControlPlacementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TERRAFORM or not shutil.which("helm"):
            raise RuntimeError("Placement tests require Terraform and Helm")

    def evaluate(self, expression, config="", variables=None):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "main.tf").write_text(config)
            (Path(directory) / "terraform.tfvars.json").write_text(json.dumps(variables or {}))
            result = subprocess.run(
                [TERRAFORM, "console", "-no-color"], cwd=directory,
                input="jsonencode(" + expression + ")\n", text=True, capture_output=True, check=True,
            )
        return json.loads(json.loads(result.stdout))

    def pool(self, vendor, settings):
        module = ROOT / "deploy/terraform" / vendor
        main = (module / "main.tf").read_text()
        variable = re.search(r'(?ms)^variable "control_node_pool" \{.*?^\}', (module / "variables.tf").read_text())[0]
        placement = re.search(r"(?ms)^  control_placement = \{.*?^  \}", main)[0]
        pool = re.search(r"(?ms)^  control_pool = .*?^  \}\] : \[\]", main)[0]
        config = variable + "\nlocals {\n" + placement + "\n" + pool + "\n}\n"
        return self.evaluate("{pools=local.control_pool,placement=local.control_placement}", config,
                             {"control_node_pool": settings})

    def cloud_pods(self, vendor, placement):
        module = ROOT / "deploy/terraform" / vendor
        source = (module / "main.tf").read_text()
        arguments = source.split("core_values = templatefile(", 1)[1].split("\n  })", 1)[0]
        values = {key: "1" for key in re.findall(r"^\s+(\w+)\s*=", arguments, re.M)}
        values.update(
            control_placement=placement, acr_secret_enabled=False, adx_service_annotations={},
            registry_auths={"auths": {}}, oss_auths={}, install_monitor=False, enable_kruise=False,
            enable_runc=False, node_secret_create=False, node_home_use_csi_ephemeral=False,
            schedule_placement_policy="spread", sandboxd_nat_backend="iptables",
            adx_control_port=443, adx_data_port=80, adx_public_service_enabled=False, adx_service_type="ClusterIP",
        )
        expression = "templatefile(" + json.dumps(str(module / "values-akernel.yaml.tmpl"))
        expression += ",jsondecode(" + json.dumps(json.dumps(values)) + "))"
        return render(yaml.safe_load(self.evaluate(expression)))

    def assert_placement(self, pods, placement):
        for name in CONTROLS:
            pod = pods[name]
            self.assertEqual(pod.get("nodeSelector", {}), placement["nodeSelector"], name)
            self.assertEqual(pod.get("tolerations", []), placement["tolerations"], name)
            self.assertNotIn("akernel.io/image-pull-ready", json.dumps(pod))
        if placement["nodeSelector"]:
            key, value = next(iter(placement["nodeSelector"].items()))
            terms = pods["akernel-node"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
            for term in terms:
                self.assertIn(dict(key=key, operator="NotIn", values=[value]), term["matchExpressions"])

    def test_legacy_helm_default_retains_original_placement(self):
        pods = render({})
        self.assert_placement(pods, {"nodeSelector": {}, "tolerations": []})
        self.assertNotIn("affinity", pods["akernel-node"])

    def test_reused_legacy_helm_defaults_preserve_original_placement(self):
        affinity = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [dict(key="zone", operator="In", values=["a"])]}],
        }}, "podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}}
        with tempfile.TemporaryDirectory() as temporary:
            chart = Path(temporary) / "core"
            shutil.copytree(CHART, chart)
            defaults = yaml.safe_load((chart / "values.yaml").read_text())
            # Helm --reuse-values replaces new chart defaults with those from the old release.
            defaults["adx"].pop("controlPlane")
            defaults["node"]["affinity"] = affinity
            (chart / "values.yaml").write_text(yaml.safe_dump(defaults))
            pods = render({}, chart)
        for name in CONTROLS:
            self.assertNotIn("nodeSelector", pods[name], name)
            self.assertNotIn("tolerations", pods[name], name)
        self.assertEqual(pods["akernel-node"]["affinity"], affinity)

    def test_cloud_legacy_does_not_add_nodes_or_change_placement(self):
        for vendor in ("aliyun", "huaweicloud"):
            with self.subTest(vendor=vendor):
                result = self.pool(vendor, {})
                self.assertEqual(result["pools"], [])
                pods = self.cloud_pods(vendor, result["placement"])
                self.assert_placement(pods, result["placement"])
                worker = pods["akernel-node"]
                if vendor == "aliyun":
                    expressions = worker["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
                    self.assertEqual(expressions, [dict(key="node-role", operator="NotIn", values=["dragonfly-seed", "dragonfly-server"])])
                else:
                    self.assertNotIn("affinity", worker)

    def test_new_control_pool_settings_and_resource_isolation(self):
        for vendor in ("aliyun", "huaweicloud"):
            field = "instance_types" if vendor == "aliyun" else "flavor_id"
            instance = ["ecs.example"] if vendor == "aliyun" else "example-flavor"
            for settings in ({"enabled": True}, {"enabled": True, "name": "app-control", "size": 2, field: instance}):
                with self.subTest(vendor=vendor, settings=settings):
                    result = self.pool(vendor, settings)
                    self.assertEqual(len(result["pools"]), 1)
                    pool = result["pools"][0]
                    self.assertEqual(pool["name"], settings.get("name", "control"))
                    self.assertEqual(pool["size"], settings.get("size", 1))
                    self.assertEqual(pool[field], settings.get(field))
                    self.assertEqual(pool["labels"], {"node-role": "control"})
                    if vendor == "aliyun":
                        self.assertFalse(pool["data_disk_enabled"])
                        self.assertFalse(pool["use_akernel_data_disk"])
                        self.assertIsNone(pool["pod_pids_limit"])
                    else:
                        self.assertTrue(pool["data_disk_enabled"])
                    pods = self.cloud_pods(vendor, result["placement"])
                    self.assert_placement(pods, result["placement"])
                    worker = pods["akernel-node"]
                    terms = worker["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
                    for term in terms:
                        self.assertIn(dict(key="node-role", operator="NotIn", values=["dragonfly-seed", "dragonfly-server"]),
                                      term["matchExpressions"])

    def test_reuse_preserves_existing_labels_and_taints_and_creates_no_pool(self):
        for vendor in ("aliyun", "huaweicloud"):
            for selector in (REUSE["node_selector"], {"ack.aliyun.com/nodepool-id": "np-existing"}):
                with self.subTest(vendor=vendor, selector=selector):
                    result = self.pool(vendor, dict(REUSE, node_selector=selector))
                    self.assertEqual(result["pools"], [])
                    self.assertEqual(result["placement"], {"nodeSelector": selector, "tolerations": REUSE["tolerations"]})
                    self.assert_placement(self.cloud_pods(vendor, result["placement"]), result["placement"])

    def test_worker_exclusion_applies_to_every_or_term_and_preserves_other_affinity(self):
        placement = {"nodeSelector": {"node-role": "master"}, "tolerations": []}
        required = {"nodeSelectorTerms": [
            {"matchExpressions": [dict(key="zone", operator="In", values=["a"])]},
            {"matchExpressions": [dict(key="zone", operator="In", values=["b"])]},
        ]}
        affinity = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": required},
                    "podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}}
        pods = render({"adx": {"controlPlane": placement}, "node": {"affinity": affinity}})
        self.assert_placement(pods, placement)
        self.assertEqual(pods["akernel-node"]["affinity"]["podAntiAffinity"], affinity["podAntiAffinity"])
        terms = pods["akernel-node"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        self.assertEqual([term["matchExpressions"][0]["values"] for term in terms], [["a"], ["b"]])

    def test_empty_required_affinity_is_not_broadened(self):
        for terms in ([], [{}], [{"matchExpressions": []}]):
            required = {"nodeSelectorTerms": terms}
            pods = render({"adx": {"controlPlane": {"nodeSelector": {"node-role": "master"}}},
                           "node": {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": required}}}})
            actual = pods["akernel-node"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
            self.assertEqual(actual, required)

    def test_multiple_pool_labels_are_rejected(self):
        with self.assertRaises(subprocess.CalledProcessError):
            render({"adx": {"controlPlane": {"nodeSelector": {"node-role": "master", "zone": "a"}}}})

    def test_existing_extra_pool_configuration_is_not_rewritten(self):
        main = (ROOT / "deploy/terraform/aliyun/main.tf").read_text()
        expression = re.search(r"(?ms)^  akernel_extra_node_pools = \[.*?^  \}\)\]", main)[0]
        variables = 'variable "extra_node_pools" {}\nvariable "node_pool_pids_limit" { default=1620780 }\n'
        value = [{"name": "existing-control", "labels": {"node-role": "control"}}]
        actual = self.evaluate("local.akernel_extra_node_pools", variables + "\nlocals {\n" + expression + "\n}",
                               {"extra_node_pools": value})[0]
        self.assertTrue(actual["use_akernel_data_disk"])
        self.assertEqual(actual["pod_pids_limit"], 1620780)

    def test_extra_disk_template_renders_valid_shell(self):
        template = ROOT / "deploy/terraform/shared/data-disk-mount.sh.tftpl"
        arguments = {"mount_path": "/home/akernel", "fs_type": "ext4", "min_size_gb": 400,
                     "storage_marker_path": "/home/akernel/.akernel-storage-ready"}
        expression = "templatefile(" + json.dumps(str(template)) + "," + json.dumps(arguments) + ")"
        script = self.evaluate(expression)
        subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, check=True)
        self.assertNotIn("$$", script)

    def test_extra_disk_skips_continue_to_pid_setup_and_errors_propagate(self):
        module = ROOT / "deploy/terraform/huaweicloud"
        source = (module / "main.tf").read_text()
        expression = re.search(
            r"(?ms)^  node_pool_bootstrap_script = (.*?^  \]\) : local.node_pool_bootstrap_base)", source,
        )[1].replace("${path.module}/../shared/", str(ROOT / "deploy/terraform/shared") + "/")
        variables = (module / "variables.tf").read_text()
        names = ("node_pool_extra_data_volume_enabled", "node_pool_extra_data_volume_size",
                 "node_pool_extra_data_volume_mount_path", "node_pool_extra_data_volume_fs_type")
        config = "\n".join(re.search(r'(?ms)^variable "' + name + r'" \{.*?^\}', variables)[0]
                           for name in names)
        # Keep the production mount expression; replace host-wide setup with a harmless base.
        config += '\nlocals {\n node_pool_bootstrap_base = "set -euo pipefail"\n'
        config += " node_pool_bootstrap_script = " + expression + "\n}\n"
        script = self.evaluate("local.node_pool_bootstrap_script", config,
                               {"node_pool_extra_data_volume_enabled": True})
        # Stubs prevent access to real disks; mkdir is a fail-closed guard before any writes.
        guard = 'mkdir() { return 99; }\n[() { if [[ "$1" == "-b" ]]; then return 1; fi; builtin [ "$@"; }\n'
        cases = (
            ("already-mounted", "mountpoint() { return 0; }\n", 0),
            ("no-available-disk", "mountpoint() { return 1; }\nfindmnt() { return 0; }\n", 0),
            ("disk-probe-failure", "mountpoint() { return 1; }\nfindmnt() { return 7; }\n", 7),
        )
        for name, stubs, expected_status in cases:
            with self.subTest(case=name):
                result = subprocess.run(["bash"], input=guard + stubs + script +
                                        '\nprintf "__PID_SETUP_REACHED__\\n"\n',
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertEqual("__PID_SETUP_REACHED__" in result.stdout, expected_status == 0)

    def test_profile_generation_and_regeneration_preserve_control_pool_choice(self):
        env = dict(os.environ, PATH=str(Path(TERRAFORM).resolve().parent) + os.pathsep + os.environ["PATH"])
        for vendor in ("aliyun", "huaweicloud"):
            with self.subTest(vendor=vendor):
                profile = ROOT / ".akernel" / ("offline-control-" + uuid.uuid4().hex)
                command = ["bash", str(ROOT / "deploy/scripts/configure.sh"), "--non-interactive", "--vendor", vendor,
                           "--env", profile.name, "--node-pool-key-name", "offline-key", "--image-tag", "offline",
                           "--grafana-admin-password", "offline-fixture"]
                reader = [sys.executable, str(ROOT / "deploy/scripts/control-pool-profile.py"), str(profile)]
                try:
                    subprocess.run(command, env=env, check=True, capture_output=True, text=True)
                    self.assertTrue(json.loads(subprocess.check_output(reader, env=env, text=True))["enabled"])
                    cases = (
                        ("string-true", "control_node_pool = ", {"enabled": "true"}, {"enabled": True}),
                        ("string-one", "control_node_pool = ", {"enabled": "1"}, {"enabled": True}),
                        ("omitted", "control_node_pool = ", REUSE, REUSE),
                        ("comment", "control_node_pool /* reuse application control pool */ = ", REUSE, REUSE),
                        ("string-false", "control_node_pool = ", dict(REUSE, enabled="false"),
                         dict(REUSE, enabled=False)),
                        ("string-zero", "control_node_pool = ", dict(REUSE, enabled="0"),
                         dict(REUSE, enabled=False)),
                        ("null", "control_node_pool = ", dict(REUSE, enabled=None), dict(REUSE, enabled=False)),
                    )
                    for name, assignment, settings, expected in cases:
                        with self.subTest(case=name):
                            (profile / "terraform.tfvars").write_text(assignment + json.dumps(settings))
                            subprocess.run(command + ["--force"], env=env, check=True, capture_output=True, text=True)
                            self.assertEqual(json.loads(subprocess.check_output(reader, env=env, text=True)), expected)
                    before = (profile / "terraform.tfvars").read_bytes()
                    refused = subprocess.run(command + ["--force", "--control-node-pool-enabled", "true"], env=env,
                                             capture_output=True, text=True)
                    self.assertNotEqual(refused.returncode, 0)
                    self.assertEqual((profile / "terraform.tfvars").read_bytes(), before)
                    for enabled in ("true", "1", True, "invalid", 1, 0):
                        with self.subTest(rejected_enabled=enabled):
                            (profile / "terraform.tfvars").write_text(
                                "control_node_pool = " + json.dumps(dict(REUSE, enabled=enabled)))
                            before = (profile / "terraform.tfvars").read_bytes()
                            refused = subprocess.run(command + ["--force"], env=env,
                                                     capture_output=True, text=True)
                            self.assertNotEqual(refused.returncode, 0)
                            self.assertEqual((profile / "terraform.tfvars").read_bytes(), before)
                    (profile / "terraform.tfvars").unlink()
                    self.assertEqual(json.loads(subprocess.check_output(reader, env=env, text=True)),
                                     dict(REUSE, enabled=False))
                finally:
                    shutil.rmtree(profile, ignore_errors=True)

    def test_old_profiles_and_state_only_recovery_stay_legacy(self):
        env = dict(os.environ, PATH=str(Path(TERRAFORM).resolve().parent) + os.pathsep + os.environ["PATH"])
        for name, text in (("terraform.tfvars", "create_cluster=true\n"), ("config.env", "VENDOR=aliyun\n"),
                           ("terraform.tfstate.backup", "{}"), ("backend-marker", "existing backend")):
            with self.subTest(marker=name), tempfile.TemporaryDirectory() as temporary:
                (Path(temporary) / name).write_text(text)
                result = subprocess.check_output([sys.executable, str(ROOT / "deploy/scripts/control-pool-profile.py"),
                                                  temporary], env=env, text=True)
                self.assertEqual(json.loads(result), {"enabled": False})

    def test_tfvars_omission_overrides_stale_config_metadata(self):
        env = dict(os.environ, PATH=str(Path(TERRAFORM).resolve().parent) + os.pathsep + os.environ["PATH"])
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary)
            (profile / "terraform.tfvars").write_text("create_cluster=true\n")
            encoded = base64.b64encode(json.dumps({"enabled": True}).encode()).decode()
            (profile / "config.env").write_text("CONTROL_NODE_POOL_B64=" + encoded + "\n")
            result = subprocess.check_output([sys.executable, str(ROOT / "deploy/scripts/control-pool-profile.py"),
                                              temporary], env=env, text=True)
            self.assertEqual(json.loads(result), {"enabled": False})

    def test_monitoring_does_not_inherit_application_control_placement(self):
        values = {"core": {"adx": {"controlPlane": {"nodeSelector": {"node-role": "master"}}}}}
        result = subprocess.run(["helm", "template", "akernel", str(ROOT / "deploy/akernel"), "-f", "-"],
                                input=yaml.safe_dump(values), text=True, capture_output=True, check=True)
        monitors = []
        for item in yaml.safe_load_all(result.stdout):
            if item and item.get("kind") in ("Deployment", "StatefulSet") and any(
                    component in item["metadata"]["name"] for component in ("prometheus", "grafana", "loki", "tempo")):
                pod = item["spec"]["template"]["spec"]
                self.assertNotIn("nodeSelector", pod)
                self.assertNotIn("affinity", pod)
                self.assertNotIn("tolerations", pod)
                monitors.append(item["metadata"]["name"])
        self.assertEqual(len(monitors), 4)


if __name__ == "__main__":
    unittest.main()
