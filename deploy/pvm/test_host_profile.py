#!/usr/bin/env python3
"""Check the maintained profile and Bash compatibility exports without builds."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class HostProfileTest(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / "deploy/pvm/host-profile.json").read_text())

    def test_committed_build_receipt_matches_the_qualified_profile(self):
        receipt = ROOT / self.profile["build_evidence_path"]
        self.assertEqual(hashlib.sha256(receipt.read_bytes()).hexdigest(),
                         self.profile["build_evidence_sha256"])
        data = json.loads(receipt.read_text())
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["source"], self.profile["source"]["commit"])
        self.assertEqual(data["baseline"], self.profile["source"]["baseline_commit"])
        self.assertEqual(data["prepared"]["profile"], self.profile["id"])
        self.assertEqual(data["prepared"]["profile_sha256"], self.profile["target"]["profile_sha256"])
        self.assertTrue(data["modpost_passed"])
        self.assertFalse(data["debugctl_applied"])
        self.assertEqual(set(data["modules"]), {"kvm.ko", "kvm-pvm.ko"})

    def test_shell_exports_are_derived_from_the_json_profile(self):
        source, target = self.profile["source"], self.profile["target"]
        expected = {"PVM_HOST_SOURCE_REPOSITORY": source["repository"],
                    "PVM_HOST_SOURCE_COMMIT": source["commit"],
                    "PVM_HOST_BASELINE_COMMIT": source["baseline_commit"],
                    "PVM_HOST_TARGET_PROFILE": target["profile"],
                    "PVM_HOST_UPSTREAM_REPOSITORY": source["upstream_repository"],
                    "PVM_HOST_UPSTREAM_BRANCH": source["upstream_branch"],
                    "PVM_HOST_UPSTREAM_COMMIT": source["upstream_commit"],
                    "PVM_HOST_QUALIFIED_KERNEL": target["kernel_release"]}
        script = 'set -euo pipefail; source "$1"; '
        script += '; '.join('printf "%s\\n" "${' + key + '}"' for key in expected)
        result = subprocess.run(["bash", "-c", script, "profile-test",
                                 str(ROOT / "deploy/pvm/oot-host.env")],
                                text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual(result.stdout.splitlines(), list(expected.values()))


class CiMemoryPolicyTest(unittest.TestCase):
    def test_pvm_ci_keeps_parent_enforcement_disabled_even_if_inherited_enabled(self):
        script = (ROOT / "deploy/pvm/run-adx-ci.sh").read_text()
        preparation = "export AKERNEL_FIRECRACKER_BACKEND=pvm" + script.split(
            "export AKERNEL_FIRECRACKER_BACKEND=pvm", 1
        )[1].split("export AKERNEL_CONTAINER_PROXY=", 1)[0]
        result = subprocess.run(
            ["bash", "-euc", preparation + '\npython3 -c \'import os; print(os.environ["AKERNEL_PVM_MEMORY_LIMIT_ENABLED"]); assert "AKERNEL_TEST_EXPECT_MEMORY_BYTES" not in os.environ; assert "AKERNEL_TEST_EXPECT_CPU_MILLIS" not in os.environ\''],
            text=True, capture_output=True,
            env=dict(os.environ, AKERNEL_PVM_MEMORY_LIMIT_ENABLED="true",
                     AKERNEL_TEST_EXPECT_MEMORY_BYTES="6442450944",
                     AKERNEL_TEST_EXPECT_CPU_MILLIS="1000"), timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "false")

    def test_pvm_qualification_settings_do_not_claim_parent_limit_coverage(self):
        script = (ROOT / "deploy/pvm/run-adx-ci.sh").read_text()
        settings = script.split("<<'SETTINGS'\n", 1)[1].split("\nSETTINGS", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["python3", "-c", settings, directory], check=True, timeout=10)
            recorded = json.loads((Path(directory) / "qualification-settings.json").read_text())
        self.assertFalse(recorded["sandbox_parent_memory_limit_enabled"])
        self.assertFalse(recorded["sandbox_parent_memory_limit_qualified"])


if __name__ == "__main__":
    unittest.main()
