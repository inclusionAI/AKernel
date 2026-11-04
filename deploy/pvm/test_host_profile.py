#!/usr/bin/env python3
"""Check the maintained profile and Bash compatibility exports without builds."""
import hashlib
import json
from pathlib import Path
import subprocess
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


if __name__ == "__main__":
    unittest.main()
