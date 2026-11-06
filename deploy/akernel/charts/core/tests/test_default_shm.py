"""Default OCI shared memory works for non-root database processes."""

import json
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[5]
CHART = ROOT / "deploy/akernel/charts/core"


class DefaultSharedMemoryTest(unittest.TestCase):
    def assert_shared_memory(self, spec):
        mounts = spec["mounts"]
        shm = [mount for mount in mounts if mount["destination"] == "/dev/shm"]
        self.assertEqual(len(shm), 1)
        self.assertEqual(shm[0]["type"], "tmpfs")
        self.assertEqual(shm[0]["source"], "shm")
        self.assertTrue(
            {"mode=1777", "size=65536k", "nosuid", "nodev", "noexec"}.issubset(
                shm[0]["options"]
            )
        )
        self.assertLess(
            next(i for i, mount in enumerate(mounts) if mount["destination"] == "/dev"),
            mounts.index(shm[0]),
        )

    def test_standalone_default_has_private_writable_shared_memory(self):
        self.assert_shared_memory(
            json.loads((ROOT / "deploy/standalone/config/config.json").read_text())
        )

    def test_rendered_helm_default_has_private_writable_shared_memory(self):
        rendered = subprocess.check_output(
            [
                "helm",
                "template",
                "akernel",
                str(CHART),
                "--set",
                "adx.tls.existingSecret=adx-test-tls",
            ],
            text=True,
        )
        config = next(
            resource
            for resource in yaml.safe_load_all(rendered)
            if resource
            and resource["kind"] == "ConfigMap"
            and resource["metadata"]["name"] == "akernel-node-config"
        )
        self.assert_shared_memory(json.loads(config["data"]["config.json"]))


if __name__ == "__main__":
    unittest.main()
