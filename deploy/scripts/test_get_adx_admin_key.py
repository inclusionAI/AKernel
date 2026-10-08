#!/usr/bin/env python3

"""Tests for reading the deployed Agent DX bootstrap administrator key."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parent


class GetAdxAdminKeyTest(unittest.TestCase):
    def test_reads_secret_and_writes_private_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "deploy/scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(SCRIPTS / "common.sh", scripts / "common.sh")
            shutil.copy2(
                SCRIPTS / "get-adx-admin-key.sh", scripts / "get-adx-admin-key.sh"
            )
            (root / "deploy/terraform/aliyun").mkdir(parents=True)
            profile = root / ".akernel/test"
            profile.mkdir(parents=True)
            (profile / "config.env").write_text("CORE_NAMESPACE=test-ns\n")
            kubeconfig = profile / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\n")

            binaries = root / "bin"
            binaries.mkdir()
            terraform = binaries / "terraform"
            terraform.write_text(
                """#!/usr/bin/env bash
case "$*" in
  *kubeconfig_path) printf '%s\\n' "${FAKE_KUBECONFIG}" ;;
  *core_namespace) printf '%s\\n' test-ns ;;
  *) exit 2 ;;
esac
"""
            )
            kubectl = binaries / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env bash
[[ "$*" == *"-n test-ns get secret akernel-adx-tls"* ]] || exit 3
printf '%s' "$FAKE_SECRET"
"""
            )
            terraform.chmod(0o755)
            kubectl.chmod(0o755)

            output = profile / "token"
            environment = dict(
                os.environ,
                PATH=f"{binaries}:{os.environ['PATH']}",
                FAKE_KUBECONFIG=str(kubeconfig),
                FAKE_SECRET="YWR4LXRlc3QtYWRtaW4ta2V5",
            )
            result = subprocess.run(
                [
                    scripts / "get-adx-admin-key.sh",
                    "--vendor",
                    "aliyun",
                    "--env",
                    "test",
                    "--write-file",
                    output,
                    "--print-export",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(result.stdout, "export AKERNEL_TOKEN=adx-test-admin-key\n")
            self.assertEqual(output.read_text(), "adx-test-admin-key\n")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            environment["FAKE_SECRET"] = base64.b64encode(b"rotated-admin-key").decode()
            refreshed = subprocess.run([scripts / "get-adx-admin-key.sh", "--vendor", "aliyun", "--env", "test", "--write-file", output, "--print-export"], check=True, capture_output=True, text=True, env=environment)
            self.assertEqual(refreshed.stdout, "export AKERNEL_TOKEN=rotated-admin-key\n")
            self.assertEqual(output.read_text(), "rotated-admin-key\n")


if __name__ == "__main__":
    unittest.main()
