#!/usr/bin/env python3
"""Check the maintained profile and Bash compatibility exports without builds."""
import hashlib
import json
import os
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


class CiMemoryBudgetTest(unittest.TestCase):
    def measured_budget(self, value, docker_status=0):
        script = (ROOT / "deploy/pvm/run-adx-ci.sh").read_text()
        after_start = script.split(
            'bash deploy/standalone/start.sh > "${out}/standalone-start.log" 2>&1\n', 1
        )[1]
        preparation = after_start.split("export AKERNEL_TEST_NETWORK_BLOCKED_HOST=", 1)[0]
        environment = dict(os.environ)
        environment.pop("AKERNEL_TEST_EXPECT_MEMORY_BYTES", None)
        environment.update(MEASURED_MEMORY_BYTES=value, DOCKER_STATUS=str(docker_status))
        return subprocess.run(
            ["bash", "-euc", '''
docker() {
  [[ "$*" == "exec akernel-node cat /sys/fs/cgroup/akernel/memory.max" ]] || return 99
  printf '%s\\n' "${MEASURED_MEMORY_BYTES}"
  return "${DOCKER_STATUS}"
}
''' + preparation + '''
python3 -c 'import os; print(os.environ["AKERNEL_TEST_EXPECT_MEMORY_BYTES"])'
'''],
            text=True, capture_output=True, env=environment, timeout=10,
        )

    def test_pvm_sdk_tests_inherit_the_measured_parent_limit(self):
        maximum = str(6 * 1024**3 + 4096)
        result = self.measured_budget(maximum)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), maximum)

    def test_pvm_qualification_rejects_missing_or_unlimited_parent_caps(self):
        for value in ("max", "0", "-1", ""):
            with self.subTest(value=value):
                result = self.measured_budget(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("finite positive sandbox parent memory limit", result.stderr)

    def test_pvm_qualification_preserves_parent_probe_failures(self):
        result = self.measured_budget(str(6 * 1024**3), docker_status=17)
        self.assertEqual(result.returncode, 17)


if __name__ == "__main__":
    unittest.main()
