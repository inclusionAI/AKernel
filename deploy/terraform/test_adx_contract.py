#!/usr/bin/env python3

"""Static cross-provider contract for guided Agent DX deployments."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEMORY_INPUTS = ("sandbox_memory_limit_enabled", "sandbox_memory_total_reserve")


def sandboxd_config(values):
    # ACK's Python 3.10 tests share evaluate_fixture without parsing TOML.
    import tomllib

    block = re.search(r"(?ms)^      config: \|-\n(.*?)(?=^ {0,6}\S|\Z)", values)
    if block is None:
        raise AssertionError("sandboxd TOML missing from generated cloud values")
    return tomllib.loads(textwrap.dedent(block.group(1)))


@unittest.skipUnless(shutil.which("terraform"), "Terraform is required for template evaluation")
class SandboxMemoryPolicyValidationTest(unittest.TestCase):
    def render(self, provider, inputs):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            variables = (ROOT / provider / "variables.tf").read_text()
            blocks = [
                re.search(rf'(?ms)^variable "{name}" {{.*?^}}', variables).group()
                for name in MEMORY_INPUTS
            ]
            main = (ROOT / provider / "main.tf").read_text()
            mapping = re.search(
                r'(?ms)^  core_values = templatefile\("\$\{path.module\}/values-akernel.yaml.tmpl", \{.*?^  \}\)',
                main,
            ).group()
            # Keep the production templatefile call and memory-input wiring.
            # Other deployment inputs use inert fixtures: no cloud providers,
            # credentials, node pools or Helm releases participate in the plan.
            booleans = {
                "acr_secret_enabled", "enable_kruise", "install_monitor",
                "adx_public_service_enabled", "enable_runc", "native_filestore",
                "node_home_use_csi_ephemeral", "node_secret_create", "node_image_pull_auto",
            }

            def fixture_input(match):
                key = match.group(1)
                if key in MEMORY_INPUTS:
                    return match.group()
                value = False if key in booleans else ""
                return f"    {key} = {json.dumps(value)}"

            mapping = re.sub(r"(?m)^    (\w+)\s+= .*?$", fixture_input, mapping)
            blocks.append("locals {\n" + mapping + "\n}")
            blocks.append('output "rendered" { value = local.core_values }')
            (fixture / "main.tf").write_text("\n".join(blocks))
            shutil.copy2(ROOT / provider / "values-akernel.yaml.tmpl", fixture)
            (fixture / "inputs.tfvars.json").write_text(json.dumps(inputs))
            subprocess.run(
                ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                cwd=fixture, capture_output=True, text=True, check=True,
            )
            result = subprocess.run(
                ["terraform", "plan", "-input=false", "-lock=false", "-refresh=false",
                 "-no-color", "-var-file=inputs.tfvars.json", "-out=plan"],
                cwd=fixture, capture_output=True, text=True,
            )
            if result.returncode:
                return result, None
            plan = subprocess.run(
                ["terraform", "show", "-json", "plan"],
                cwd=fixture, capture_output=True, text=True, check=True,
            )
            rendered = json.loads(plan.stdout)["planned_values"]["outputs"]["rendered"]["value"]
            return result, sandboxd_config(rendered)

    def test_cloud_defaults_and_explicit_memory_policy_overrides(self):
        for provider in ("aliyun", "huaweicloud"):
            for inputs, enabled, reserve in (
                ({}, False, "16GiB"),
                ({"sandbox_memory_limit_enabled": True}, True, "16GiB"),
                ({"sandbox_memory_limit_enabled": True, "sandbox_memory_total_reserve": "2GiB"}, True, "2GiB"),
                ({"sandbox_memory_limit_enabled": False, "sandbox_memory_total_reserve": "2GiB"}, False, "2GiB"),
            ):
                with self.subTest(provider=provider, inputs=inputs):
                    result, config = self.render(provider, inputs)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    resource = config["plugin"]["resource"]
                    self.assertEqual(resource["memory_limit"], {"enabled": enabled, "total_reserve": reserve})
                    self.assertIs(resource["memory_limit"]["enabled"], enabled)
                    self.assertEqual(resource["cgroup_root_name"], "/akernel")
                    self.assertEqual(resource["max_instance_num"], 1000)
                    self.assertIs(resource["enable_cache_in_stock_mode"], True)
                    self.assertEqual(config["plugin"]["image"]["cgroup_memory_limit"], "64GiB")

    def test_invalid_reserves_fail_before_deployment(self):
        for provider in ("aliyun", "huaweicloud"):
            for reserve in ("", "0", "0GiB", "16G", "auto", "-1GiB", "16GiB\"\nenabled=true"):
                with self.subTest(provider=provider, reserve=reserve):
                    result, _ = self.render(provider, {"sandbox_memory_total_reserve": reserve})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("sandbox_memory_total_reserve", result.stderr)


class GuidedMemoryPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        scripts = self.fixture / "deploy/scripts"
        scripts.mkdir(parents=True)
        for name in ("configure.sh", "common.sh", "shared_storage_config.py"):
            shutil.copy2(ROOT.parent / "scripts" / name, scripts)
        shutil.copy2(ROOT.parents[1] / "Makefile", self.fixture)
        policy = self.fixture / "deploy/terraform/aliyun/node-image-pull-policy.json"
        policy.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "aliyun" / policy.name, policy)
        commands = self.fixture / "bin"
        commands.mkdir()
        formatter = commands / "terraform"
        # Profile generation only calls terraform fmt; evaluated rendering and
        # validation are covered separately with the real Terraform CLI above.
        formatter.write_text('#!/bin/sh\n[ "$1" = fmt ]\n')
        formatter.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"])
        for name in ("SANDBOX_MEMORY_LIMIT_ENABLED", "SANDBOX_MEMORY_TOTAL_RESERVE", "FORCE"):
            self.environment.pop(name, None)

    def generate(self, provider, name, *settings):
        return subprocess.run(
            ["make", "config", f"VENDOR={provider}", f"ENV={name}", "NON_INTERACTIVE=1",
             "NODE_POOL_KEY_NAME=fixture", "IMAGE_REPOSITORY=example.invalid/fixture",
             "IMAGE_TAG=fixture", *settings],
            cwd=self.fixture, env=self.environment, capture_output=True, text=True,
        )

    def test_generated_profiles_default_to_opt_out_and_preserve_explicit_opt_in(self):
        for provider in ("aliyun", "huaweicloud"):
            for name, settings, enabled, reserve in (
                ("default", (), "false", "16GiB"),
                ("enabled", ("SANDBOX_MEMORY_LIMIT_ENABLED=true",), "true", "16GiB"),
                ("small", ("SANDBOX_MEMORY_LIMIT_ENABLED=true", "SANDBOX_MEMORY_TOTAL_RESERVE=2GiB"), "true", "2GiB"),
            ):
                profile_name = f"{provider}-{name}"
                with self.subTest(provider=provider, profile=name):
                    result = self.generate(provider, profile_name, *settings)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    profile = self.fixture / ".akernel" / profile_name
                    tfvars = (profile / "terraform.tfvars").read_text()
                    config = (profile / "config.env").read_text()
                    self.assertIn(f"sandbox_memory_limit_enabled = {enabled}\n", tfvars)
                    self.assertIn(f'sandbox_memory_total_reserve = "{reserve}"\n', tfvars)
                    self.assertIn(f"SANDBOX_MEMORY_LIMIT_ENABLED={enabled}\n", config)
                    self.assertIn(f"SANDBOX_MEMORY_TOTAL_RESERVE={reserve}\n", config)

    def test_invalid_input_and_existing_profiles_are_not_replaced(self):
        for provider in ("aliyun", "huaweicloud"):
            with self.subTest(provider=provider):
                result = self.generate(provider, provider)
                self.assertEqual(result.returncode, 0, result.stderr)
                profile = self.fixture / ".akernel" / provider
                original = {name: (profile / name).read_bytes() for name in ("config.env", "terraform.tfvars")}
                for settings in (
                    ("SANDBOX_MEMORY_LIMIT_ENABLED=maybe",),
                    ("SANDBOX_MEMORY_TOTAL_RESERVE=0GiB",),
                    ("SANDBOX_MEMORY_TOTAL_RESERVE=16G",),
                    ("SANDBOX_MEMORY_LIMIT_ENABLED=true",),
                ):
                    result = self.generate(provider, provider, *settings)
                    self.assertNotEqual(result.returncode, 0)
                    for name, data in original.items():
                        self.assertEqual((profile / name).read_bytes(), data)


def evaluate_fixture(main, inputs, files=None):
    """Evaluate real Terraform expressions without cloud resources or credentials."""
    with tempfile.TemporaryDirectory() as directory:
        fixture = Path(directory)
        (fixture / "main.tf").write_text(main)
        (fixture / "inputs.tfvars.json").write_text(json.dumps(inputs))
        for name, contents in (files or {}).items():
            (fixture / name).write_text(contents)
        subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                       cwd=fixture, capture_output=True, text=True, check=True)
        result = subprocess.run(
            ["terraform", "plan", "-input=false", "-lock=false", "-refresh=false", "-no-color",
             "-var-file=inputs.tfvars.json", "-out=plan"],
            cwd=fixture, capture_output=True, text=True, check=False)
        if result.returncode:
            return result, None
        plan = subprocess.run(["terraform", "show", "-json", "plan"],
                              cwd=fixture, capture_output=True, text=True, check=True)
        return result, {key: item["value"] for key, item in
                        json.loads(plan.stdout)["planned_values"]["outputs"].items()}


@unittest.skipUnless(
    shutil.which("terraform"), "Terraform is required for evaluated storage validation"
)
class CheckpointStorageValidationTest(unittest.TestCase):
    def test_s3_forwards_identity_and_local_cache_without_mount_requirements(self):
        for provider in ("aliyun", "huaweicloud"):
            value = {"kind": "s3", "alias": "checkpoints-s3", "s3": {
                "bucket": "checkpoints", "region": "us-east-1",
                "endpoint": "https://rgw.example.com", "credentials_secret": "s3-credentials"}}
            result, output = self.run_plan(provider, value)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output["s3"]["credentialsSecret"], "s3-credentials")
            self.assertEqual(output["s3"]["cacheBudgetBytes"], 53687091200)
            value["s3"]["endpoint"] = "http://rgw.example.com"
            result, _ = self.run_plan(provider, value)
            self.assertNotEqual(result.returncode, 0)
            value["s3"]["allow_http"] = True
            result, _ = self.run_plan(provider, value)
            self.assertEqual(result.returncode, 0, result.stderr)

    def run_plan(self, provider, storage, juicefs=None):
        variables = (ROOT / provider / "variables.tf").read_text()
        # Evaluate the real input validation and shared output preconditions.
        blocks = [re.search(rf'(?ms)^variable "{name}" {{.*?^}}', variables).group()
                  for name in ("checkpoint_storage", "juicefs")]
        shared = (ROOT / "shared-storage/main.tf").read_text()
        blocks.append(re.search(r"(?ms)^locals {.*?^}", shared).group())
        blocks.append((ROOT / "shared-storage/outputs.tf").read_text())
        result, outputs = evaluate_fixture("\n".join(blocks),
                                           {"checkpoint_storage": storage, "juicefs": juicefs or {}})
        return result, outputs["helm_values"] if outputs else None

    def test_alias_validation_in_both_provider_roots_and_effective_modes(self):
        for provider in ("aliyun", "huaweicloud"):
            for alias in ("local", "team_cache", "", "cache/path", "缓存"):
                for managed in (False, True):
                    with self.subTest(provider=provider, alias=alias, managed=managed):
                        storage = {"alias": alias}
                        juicefs = (
                            {
                                "enabled": True,
                                "ceph_storage_class": "block",
                                "postgres_storage_class": "filesystem",
                            }
                            if managed
                            else {}
                        )
                        if not managed:
                            storage.update(
                                kind="distributed_fs", host_path="/srv/shared"
                            )
                        result, _ = self.run_plan(provider, storage, juicefs)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn("alias", result.stderr)

    def test_valid_shared_alias_and_local_reserved_name(self):
        for provider in ("aliyun", "huaweicloud"):
            for alias in ("akernel-checkpoints", "Team9", "-cache", "LOCAL"):
                with self.subTest(provider=provider, alias=alias):
                    result, _ = self.run_plan(
                        provider,
                        {
                            "kind": "distributed_fs",
                            "alias": alias,
                            "host_path": "/srv/shared",
                        },
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
            result, value = self.run_plan(provider, {"kind": "local", "alias": "local"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(value["nodeSelector"], {})

    def test_managed_consumer_selector_includes_values_overlays(self):
        with tempfile.TemporaryDirectory() as directory:
            overlay = Path(directory) / "values.yaml"
            overlay.write_text(
                "juicefs:\n  nodeSelector:\n    pool: fast\n    disk: local\n"
            )
            for provider in ("aliyun", "huaweicloud"):
                with self.subTest(provider=provider):
                    result, value = self.run_plan(
                        provider,
                        {},
                        {
                            "enabled": True,
                            "ceph_storage_class": "block",
                            "postgres_storage_class": "filesystem",
                            "client_node_selector": {"pool": "slow"},
                            "values_files": [str(overlay)],
                        },
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        value["nodeSelector"],
                        {"kubernetes.io/os": "linux", "pool": "fast", "disk": "local"},
                    )
                    self.assertEqual(value["kind"], "distributed_fs")

    def test_managed_clients_cannot_target_non_linux_nodes(self):
        for provider in ("aliyun", "huaweicloud"):
            result, _ = self.run_plan(
                provider,
                {},
                {
                    "enabled": True,
                    "ceph_storage_class": "block",
                    "postgres_storage_class": "filesystem",
                    "client_node_selector": {"kubernetes.io/os": "windows"},
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Linux", result.stderr)


class AdxTerraformContractTest(unittest.TestCase):
    def test_cloud_memory_policy_is_explicitly_wired_and_defaults_off(self):
        for provider in ("aliyun", "huaweicloud"):
            with self.subTest(provider=provider):
                values = (ROOT / provider / "values-akernel.yaml.tmpl").read_text()
                main = (ROOT / provider / "main.tf").read_text()
                variables = (ROOT / provider / "variables.tf").read_text()
                for name, default in zip(MEMORY_INPUTS, ("false", '"16GiB"')):
                    self.assertRegex(main, rf"{name}\s*= var\.{name}")
                    variable = re.search(rf'(?ms)^variable "{name}" {{.*?^}}', variables).group()
                    self.assertRegex(variable, rf"default\s*= {default}")
                self.assertIn("enabled = ${sandbox_memory_limit_enabled}", values)
                self.assertIn("total_reserve = ${jsonencode(sandbox_memory_total_reserve)}", values)
                config = sandboxd_config(values.replace("${sandbox_memory_limit_enabled}", "false").replace(
                    "${jsonencode(sandbox_memory_total_reserve)}", '"16GiB"'))
                resource = config["plugin"]["resource"]
                self.assertEqual(
                    resource["memory_limit"],
                    {"enabled": False, "total_reserve": "16GiB"},
                )
                self.assertIs(resource["memory_limit"]["enabled"], False)
                self.assertEqual(resource["cgroup_root_name"], "/akernel")
                self.assertEqual(resource["max_instance_num"], 1000)
                self.assertIs(resource["enable_cache_in_stock_mode"], True)
                self.assertEqual(
                    config["plugin"]["image"]["cgroup_memory_limit"], "64GiB"
                )

    def test_each_provider_exposes_adx_control_and_data_directly(self) -> None:
        for provider in ("aliyun", "huaweicloud"):
            with self.subTest(provider=provider):
                values = (ROOT / provider / "values-akernel.yaml.tmpl").read_text()
                self.assertIn("publicService:", values)
                self.assertIn("enabled: ${adx_public_service_enabled}", values)
                self.assertIn("controlPort: ${adx_control_port}", values)
                self.assertIn("dataPort: ${adx_data_port}", values)
                self.assertNotIn("traefik", values.lower())
                main = (ROOT / provider / "main.tf").read_text()
                variables = (ROOT / provider / "variables.tf").read_text()
                self.assertNotIn("traefik", main.lower())
                self.assertNotIn("traefik", variables.lower())

    def test_each_provider_prepares_identity_before_helm(self) -> None:
        for provider in ("aliyun", "huaweicloud"):
            with self.subTest(provider=provider):
                main = (ROOT / provider / "main.tf").read_text()
                self.assertIn('resource "null_resource" "ensure_adx_secret"', main)
                self.assertIn("../../scripts/ensure-adx-secret.sh", main)
                helm = main[main.index('resource "helm_release" "akernel_core"') :]
                self.assertIn("null_resource.ensure_adx_secret", helm)

    def test_each_provider_renders_the_adx_topology(self) -> None:
        for provider in ("aliyun", "huaweicloud"):
            with self.subTest(provider=provider):
                values = (ROOT / provider / "values-akernel.yaml.tmpl").read_text()
                self.assertIn("adx:", values)
                self.assertIn("  coordinator:\n    replicas: 1", values)
                self.assertIn("  ingressApi:\n    replicas: 1", values)
                self.assertIn('repository: "${master_image_repository}"', values)
                self.assertIn('tag: "${master_image_tag}"', values)
                self.assertIn(
                    'schedulePlacementPolicy: "${schedule_placement_policy}"', values
                )
                self.assertIn("existingSecret: akernel-adx-tls", values)
                self.assertIn("mode: managed", values)
                self.assertIn("appendfsync: everysec", values)
                self.assertIn("replicas: 1", values)


if __name__ == "__main__":
    unittest.main()
