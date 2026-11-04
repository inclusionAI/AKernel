#!/usr/bin/env python3

"""Static cross-provider contract for guided Agent DX deployments."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


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
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            variables = (ROOT / provider / "variables.tf").read_text()
            # Evaluate the real provider input validation and shared module
            # output preconditions without cloud resources or credentials.
            blocks = [
                re.search(rf'(?ms)^variable "{name}" {{.*?^}}', variables).group()
                for name in ("checkpoint_storage", "juicefs")
            ]
            shared = (ROOT / "shared-storage/main.tf").read_text()
            blocks.append(re.search(r"(?ms)^locals {.*?^}", shared).group())
            blocks.append((ROOT / "shared-storage/outputs.tf").read_text())
            (fixture / "main.tf").write_text("\n".join(blocks))
            (fixture / "inputs.tfvars.json").write_text(
                json.dumps(
                    {
                        "checkpoint_storage": storage,
                        "juicefs": juicefs or {},
                    }
                )
            )
            subprocess.run(
                ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                cwd=fixture,
                capture_output=True,
                text=True,
                check=True,
            )
            result = subprocess.run(
                [
                    "terraform",
                    "plan",
                    "-input=false",
                    "-lock=false",
                    "-refresh=false",
                    "-no-color",
                    "-var-file=inputs.tfvars.json",
                    "-out=plan",
                ],
                cwd=fixture,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                return result, None
            plan = subprocess.run(
                ["terraform", "show", "-json", "plan"],
                cwd=fixture,
                capture_output=True,
                text=True,
                check=True,
            )
            return result, json.loads(plan.stdout)["planned_values"]["outputs"][
                "helm_values"
            ]["value"]

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
