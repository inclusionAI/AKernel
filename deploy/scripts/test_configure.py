#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Profile generation in temporary repositories; Terraform is a fmt-only stub."""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ACR = "registry.cn-hangzhou.aliyuncs.com/akernel/all-in-one"


class ConfigureTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="akernel-config-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for relative in (
            "Makefile", "deploy/scripts/configure.sh", "deploy/scripts/common.sh",
            "deploy/scripts/shared_storage_config.py",
            "deploy/terraform/aliyun/node-image-pull-policy.json",
        ):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        binaries = self.root / "bin"
        binaries.mkdir()
        for name, content in {
            "git": "#!/bin/sh\nprintf '%s\\n' offline-test\n",
            "terraform": "#!/bin/sh\n[ \"$1\" = fmt ] || exit 91\n",
        }.items():
            path = binaries / name
            path.write_text(content)
            path.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"])
        self.profile = self.root / ".akernel/test"

    def generate(self, *extra, vendor="aliyun", success=True):
        arguments = ["/bin/bash", str(self.root / "deploy/scripts/configure.sh"),
                     "--vendor", vendor, "--env", "test", "--non-interactive",
                     "--image-tag", "offline-test", "--grafana-admin-password", "offline-test"]
        if vendor == "huaweicloud":
            arguments += ["--node-pool-key-name", "offline-key"]
        result = subprocess.run(arguments + list(extra), cwd=self.root, env=self.environment,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return result

    def assert_mode(self, mode, vendor="aliyun"):
        config = (self.profile / "config.env").read_text()
        variables = (self.profile / "terraform.tfvars").read_text()
        self.assertIn("NODE_IMAGE_PULL_MODE=" + mode + "\n", config)
        if vendor == "aliyun":
            self.assertRegex(variables, r'(?m)^node_image_pull_mode\s*=\s*"' + mode + '"$')
        else:
            self.assertNotIn("node_image_pull_mode", variables)
        for name in ("config.env", "terraform.tfvars"):
            self.assertEqual(stat.S_IMODE((self.profile / name).stat().st_mode), 0o600)
        return variables

    def reset_profile(self):
        shutil.rmtree(self.profile, ignore_errors=True)

    def test_new_profile_modes_and_dragonfly_install_are_independent(self):
        enterprise = "example.cn-hangzhou.cr.aliyuncs.com/team/all-in-one"
        cases = [
            ("aliyun", ACR, [], "auto", "false"),
            ("aliyun", enterprise, ["--install-dragonfly", "true"], "auto", "true"),
            ("aliyun", "example.com/team/image", [], "direct", "false"),
            ("aliyun", ACR, ["--node-image-pull-mode", "direct"], "direct", "false"),
            ("huaweicloud", "swr.cn-north-4.myhuaweicloud.com/team/image", [], "direct", "false"),
        ]
        for vendor, repository, extra, mode, install in cases:
            with self.subTest(vendor=vendor, mode=mode, repository=repository):
                self.reset_profile()
                self.generate("--image-repository", repository, *extra, vendor=vendor)
                variables = self.assert_mode(mode, vendor)
                self.assertRegex(variables, r"(?m)^install_dragonfly\s*=\s*" + install + "$")
                if mode == "auto":
                    self.assertIn('acr_registry_url = "' + repository.rsplit("/", 1)[0] + '"', variables)

    def test_force_preserves_saved_mode_and_never_opts_legacy_profiles_in(self):
        cases = [
            ({"terraform.tfvars": "create_cluster = true\n"}, [], "direct"),
            ({"terraform.tfvars": 'node_image_pull_mode = "auto"\n'}, [], "auto"),
            ({"config.env": "export NODE_IMAGE_PULL_MODE='direct'\n"}, [], "direct"),
            ({"terraform.tfvars": "create_cluster = true\n"}, ["--node-image-pull-mode", "auto"], None),
            ({"terraform.tfvars": 'node_image_pull_mode = "direct"\n'}, ["--node-image-pull-mode", "auto"], None),
            ({"terraform.tfvars": 'node_image_pull_mode = "auto"\n'}, ["--node-image-pull-mode", "auto"], "auto"),
            ({"terraform.tfvars": 'node_image_pull_mode = "auto"\n'}, ["--node-image-pull-mode", "direct"], None),
        ]
        for files, extra, mode in cases:
            with self.subTest(files=files):
                self.reset_profile()
                self.profile.mkdir(parents=True)
                for name, text in files.items():
                    (self.profile / name).write_text(text)
                result = self.generate("--force", *extra, success=mode is not None)
                if mode is None:
                    self.assertIn("fresh ENV", result.stderr)
                    self.assertEqual((self.profile / "terraform.tfvars").read_text(), files["terraform.tfvars"])
                else:
                    self.assert_mode(mode)

    def test_explicit_unsupported_modes_fail_before_writing_profiles(self):
        cases = [
            ("aliyun", ["--image-repository", "example.com/team/image", "--node-image-pull-mode", "auto"], "ACR"),
            ("aliyun", ["--node-image-pull-mode", "invalid"], "auto or direct"),
            ("huaweicloud", ["--node-image-pull-mode", "auto"], "Aliyun ACK"),
        ]
        for vendor, extra, message in cases:
            with self.subTest(vendor=vendor, extra=extra):
                self.assertIn(message, self.generate(*extra, vendor=vendor, success=False).stderr)
                self.assertFalse(self.profile.exists())

    def test_saved_modes_are_read_as_data_and_cannot_be_bypassed(self):
        self.profile.mkdir(parents=True)
        (self.profile / "terraform.tfvars").write_text('node_image_pull_mode = "auto"\n')
        config = self.profile / "config.env"
        config.write_text("NODE_IMAGE_PULL_MODE=direct\n")
        self.assertIn("conflict", self.generate("--force", success=False).stderr)
        self.assertIn("conflict", self.generate("--force", "--node-image-pull-mode", "direct", success=False).stderr)
        marker = self.root / "must-not-execute"
        config.write_text("NODE_IMAGE_PULL_MODE=$(touch " + str(marker) + ")\n")
        self.assertIn("invalid saved", self.generate("--force", success=False).stderr)
        self.assertFalse(marker.exists())
        self.assertIn("invalid saved", self.generate("--force", "--node-image-pull-mode", "direct", success=False).stderr)
        self.assertFalse(marker.exists())

    def test_make_config_forwards_explicit_mode(self):
        result = subprocess.run(["make", "-s", "config", "ENV=test", "NON_INTERACTIVE=1",
                                 "NODE_IMAGE_PULL_MODE=direct", "GRAFANA_ADMIN_PASSWORD=offline-test"],
                                cwd=self.root, env=self.environment, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_mode("direct")


if __name__ == "__main__":
    unittest.main()
