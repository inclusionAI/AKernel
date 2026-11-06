#!/usr/bin/env python3
"""Evaluate ACK image-pull composition without provisioning a cluster."""

import base64
import gzip
import json
import re
import shutil
import textwrap
import unittest
from unittest import mock

from test_adx_contract import ROOT, evaluate_fixture

ALIYUN = ROOT / "aliyun"
MAIN = (ALIYUN / "main.tf").read_text()


@unittest.skipUnless(shutil.which("terraform"), "Terraform is required for evaluated ACK rendering")
class NodeImagePullTerraformTest(unittest.TestCase):
    def evaluate(self, **overrides):
        # Use the production locals, preconditions and affinity template. Only
        # substitute the cloud-assigned cluster ID and unrelated image inputs.
        variables = (ALIYUN / "variables.tf").read_text()
        names = ("node_image_pull_mode", "create_cluster", "node_pool_image_type", "node_pool_image_id",
                 "acr_username", "acr_password", "extra_node_pools", "region", "dragonfly_namespace", "service_cidr",
                 "sandboxd_nat_backend", "node_pool_max_user_namespaces", "node_pool_user_data", "node_pool_size",
                 "dragonfly_seed_node_pool", "dragonfly_server_node_pool")
        blocks = [re.search(rf'(?ms)^variable "{name}" {{.*?^}}', variables).group() for name in names]
        composition = re.search(r'(?ms)^locals {\n  node_image_pull_auto .*?^}', MAIN).group()
        composition = composition.replace('alicloud_cs_managed_kubernetes.ack[0].id', '"c13e2f81906a74fa781b341edc52a3cf08"')
        composition = composition.replace('${path.module}/node-image-pull', str(ALIYUN / "node-image-pull"))
        conditions = MAIN[MAIN.index('    precondition {\n      condition     = var.node_image_pull_mode'):MAIN.index('    # Skipped:')]
        original = MAIN[MAIN.index('  node_pool_bootstrap_base ='):MAIN.index('  dragonfly_seed_user_data =')]
        original = original.replace('${path.module}/../shared', str(ROOT / 'shared'))
        affinity = (ALIYUN / "values-akernel.yaml.tmpl").read_text().split('node:\n', 1)[1].split('  image:\n', 1)[0]
        pools = re.search(r'(?ms)^      POOLS = (jsonencode\(merge\(.*?^      \)\))', MAIN).group(1)
        for name in ("default_with_key", "default_with_password", "extra"):
            pools = pools.replace("alicloud_cs_kubernetes_node_pool." + name, "var.test_" + name)
        blocks.extend([
            'variable "test_default_with_key" { default = [{id = "c-test:np-key", node_pool_id = "np-key"}] }',
            'variable "test_default_with_password" { default = [{id = "c-test:np-password", node_pool_id = "np-password"}] }',
            'variable "test_extra" { default = {baremetal = {id = "c-test:np-worker", node_pool_id = "np-worker", desired_size = "1"}, storage = {id = "c-test:np-storage", node_pool_id = "np-storage", desired_size = "1"}} }',
            'variable "repository" { default = "registry-vpc.cn-hangzhou.aliyuncs.com/team/all-in-one" }',
            'variable "credential_host" { default = "registry-vpc.cn-hangzhou.aliyuncs.com" }',
            'locals {\n node_image_repo = var.repository\n acr_host = var.credential_host\n'
            ' acr_secret_enabled = length(var.acr_username) > 0 && length(var.acr_password) > 0\n' + original + '\n}',
            composition,
            'resource "terraform_data" "validation" {\n lifecycle {\n' + conditions + '\n}\n}',
            'output "rendered" {\n sensitive = true\n value = {\n'
            ' user_data = local.node_image_pull_user_data\n workers = local.node_image_pull_workers\n'
            ' pools = jsondecode(' + pools + ')\n'
            ' profile = local.node_image_pull_profile\n affinity = templatefile("${path.module}/affinity.tmpl"' +
            ', {node_image_pull_auto = local.node_image_pull_auto})\n}\n}',
        ])
        inputs = {"node_image_pull_mode": "auto", "region": "cn-hangzhou"}
        inputs.update(overrides)
        result, output = evaluate_fixture("\n".join(blocks), inputs, {"affinity.tmpl": affinity})
        return result, output["rendered"] if output else None

    def test_default_and_explicit_workers_preserve_scripts_and_gate(self):
        original = '#!/bin/bash\nprintf "baremetal host contract\\n"\nexit 2\n'
        result, value = self.evaluate(extra_node_pools=[
            {"name": "baremetal", "size": 1, "node_image_pull_worker": True,
             "pre_user_data": "#!/bin/bash\nexit 0\n", "user_data": original,
             "labels": {"node-role": "akernel-compute"}},
            {"name": "storage", "size": 1, "labels": {"node-role": "akernel-storage"}},
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(value["user_data"]), {"default", "extra/baremetal"})
        self.assertEqual(value["pools"], {"np-key": 3, "np-password": 3, "np-worker": 1})
        for name, wrapper in value["user_data"].items():
            blob = re.search(r"'([A-Za-z0-9+/=]{100,})'", wrapper).group(1)
            bundle = json.loads(gzip.decompress(base64.b64decode(blob)))
            profile = bundle["profile"]
            self.assertEqual(profile["registry"], "registry-vpc.cn-hangzhou.aliyuncs.com")
            self.assertEqual(profile["domain"], "dragonfly-seed-client.dragonfly-system.svc.cluster.local")
            self.assertEqual(profile["pool_marker"], value["workers"][name]["marker"])
            self.assertEqual(bundle["user_data"], value["workers"][name]["user_data"])
            self.assertIn('kernel.pid_max=4194304', bundle["bootstrap"])
            self.assertEqual(bundle["helper"], (ALIYUN / "node-image-pull.py").read_text())
            self.assertLessEqual(len(wrapper.encode()), 16384)
        self.assertEqual(value["workers"]["extra/baremetal"]["user_data"], original)
        self.assertIn('akernel.io/image-pull-ready', value["affinity"])
        script = textwrap.dedent(re.search(r'(?ms)^      import json, os, subprocess, time\n.*?(?=^      PY$)', MAIN).group())
        nodes = [{"metadata": {"labels": {"alibabacloud.com/nodepool-id": pool,
                  "akernel.io/image-pull-profile": "ack-dragonfly-v1", "akernel.io/image-pull-ready": "true"}},
                  "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
                 for pool, size in value["pools"].items() for _ in range(size)]
        for state in ("healthy", "missing", "not-ready", "not-initialized"):
            with self.subTest(state=state):
                sample = json.loads(json.dumps(nodes))
                if state == "missing": sample.pop()
                if state == "not-ready": sample[-1]["status"]["conditions"][0]["status"] = "False"
                if state == "not-initialized": sample[-1]["metadata"]["labels"]["akernel.io/image-pull-ready"] = "false"
                with mock.patch.dict('os.environ', POOLS=json.dumps(value["pools"])), \
                     mock.patch('subprocess.run', return_value=mock.Mock(returncode=0, stdout=json.dumps({"items": sample}))), \
                     mock.patch('time.monotonic', side_effect=[0, 1, 721]), mock.patch('time.sleep'):
                    if state == "healthy":
                        exec(compile(script, '<readiness>', 'exec'), {})
                    else:
                        with self.assertRaisesRegex(SystemExit, 'bootstrap is not ready'):
                            exec(compile(script, '<readiness>', 'exec'), {})

    def test_direct_is_unchanged_and_auto_rejects_unsupported_inputs(self):
        result, direct = self.evaluate(node_image_pull_mode="direct", node_pool_image_type="custom", repository="docker.io/team/image")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(direct["user_data"], {})
        self.assertNotIn('image-pull-', direct["affinity"])
        self.assertTrue(direct["affinity"].endswith('            - dragonfly-server\n'))
        for inputs in (
            {"create_cluster": False}, {"node_pool_image_type": "AliyunLinux4ContainerOptimized"},
            {"node_pool_image_id": "custom-image-id"}, {"repository": "docker.io/team/image"},
            {"dragonfly_namespace": "a" * 64},
            {"acr_username": "test"}, {"acr_username": "test", "acr_password": "dummy", "credential_host": "other.example.com"},
            {"extra_node_pools": [{"name": "control", "size": 1, "node_image_pull_worker": True, "labels": {"node-role": "akernel-control-plane"}}]},
            {"dragonfly_seed_node_pool": {"node_label_key": "dedicated", "node_label_value": "df"},
             "extra_node_pools": [{"name": "seed", "size": 1, "node_image_pull_worker": True, "labels": {"dedicated": "df"}}]},
        ):
            with self.subTest(inputs=inputs):
                result, _ = self.evaluate(**inputs)
                self.assertNotEqual(result.returncode, 0)
        result, _ = self.evaluate(node_pool_image_id="aliyun_3_x64_20G_container_optimized_alibase_20250101.vhd")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_core_value_overrides_are_preserved_in_both_modes(self):
        variables = (ALIYUN / "variables.tf").read_text()
        blocks = [re.search(r'(?ms)^variable "core_values_override_files" {.*?^}', variables).group(),
                  'locals { core_values = "base: true\\n" }']
        releases = ("akernel_core", "akernel_core_dragonfly")
        for name in releases:
            block = re.search(rf'(?ms)^resource "helm_release" "{name}" {{.*?^}}', MAIN).group()
            values = re.search(r'(?m)^\s+values\s*=\s*(.+)$', block).group(1)
            blocks.append(f'output "{name}" {{ value = {values} }}')
        files = {"first.yaml": "setting: first\n", "second.yaml": "setting: second\n"}
        for paths in ([], list(files)):
            with self.subTest(paths=paths):
                result, output = evaluate_fixture("\n".join(blocks),
                                                  {"core_values_override_files": paths}, files)
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in releases:
                    self.assertEqual(output[name], ["base: true\n"] + [files[path] for path in paths])

    def test_pool_scoping_and_core_order_preserve_existing_state(self):
        for name in ("default_with_key", "default_with_password", "extra"):
            block = re.search(rf'(?ms)^resource "alicloud_cs_kubernetes_node_pool" "{name}" {{.*?^}}', MAIN).group()
            self.assertIn('update_nodes', block)
            self.assertRegex(block, r'update_nodes\s*= false')
            self.assertIn('base64encode(', block)
        profile = re.search(r'(?ms)^locals {\n  node_image_pull_auto .*?^}', MAIN).group()
        self.assertNotIn('alicloud_cs_kubernetes_node_pool.', profile)
        core = re.search(r'(?ms)^resource "helm_release" "akernel_core" {.*?^}', MAIN).group()
        self.assertIn('module.shared_storage', core)
        self.assertNotIn('null_resource.node_image_pull_ready', core)
        self.assertNotIn('helm_release.dragonfly', core)
        self.assertRegex(core, r'count\s*= local.node_image_pull_auto \? 0 : 1')
        accelerated = re.search(r'(?ms)^resource "helm_release" "akernel_core_dragonfly" {.*?^}', MAIN).group()
        self.assertIn('module.shared_storage', accelerated)
        self.assertIn('null_resource.node_image_pull_ready', accelerated)
        self.assertRegex(accelerated, r'count\s*= local.node_image_pull_auto \? 1 : 0')
        for release in (core, accelerated):
            self.assertIn('local.core_values', release)
            self.assertIn('local.core_chart_hash', release)
        self.assertRegex(MAIN, r'moved {\s+from = helm_release.akernel_core\s+to\s+= helm_release.akernel_core\[0\]\s+}')
        barrier = re.search(r'(?ms)^resource "null_resource" "node_image_pull_ready" {.*?^}', MAIN).group()
        self.assertIn('helm_release.dragonfly', barrier)
        self.assertIn('count = local.node_image_pull_auto ? 1 : 0', barrier)


if __name__ == "__main__":
    unittest.main()
