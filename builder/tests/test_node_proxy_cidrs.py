"""Check the startup ACL against the actual sandboxd TOML and overrides."""
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class NodeProxyCIDRsTest(unittest.TestCase):
    def resolve(self, config, override=""):
        source = (Path(__file__).resolve().parents[1] / "scripts/yr_node_bootstrap.sh").read_text()
        function = "resolve_node_proxy_target_cidrs() {" + source.split(
            "resolve_node_proxy_target_cidrs() {", 1
        )[1].split('\nYR_NODE_IP=', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "python3").symlink_to(sys.executable)
            config_path = root / "sandboxd.toml"
            if config is not None:
                config_path.write_text(config)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                       SANDBOXD_CONFIG_PATH=str(config_path),
                       NODE_PROXY_ALLOWED_TARGET_CIDRS=override)
            return subprocess.run(["bash", "-c", function + "\nresolve_node_proxy_target_cidrs"],
                                  env=env, text=True, capture_output=True, timeout=5)

    def test_final_config_and_host_bits(self):
        result = self.resolve("[plugin.network]\nip_range = '10.99.7.1/20' # custom pool\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "10.99.0.0/20")

    def test_explicit_override_without_config(self):
        result = self.resolve(None, "192.168.42.5/24, fd00:1234::1/64")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "192.168.42.0/24,fd00:1234::/64")

    def test_invalid_or_missing_pool_fails_closed(self):
        for config in (None, "[plugin.network]\n", "[plugin.network]\nip_range='bad'\n",
                       "[plugin.network]\nip_range=''\n", "[plugin.network]\nip_range=3\n",
                       "[plugin.network]\nip_range='10.0.0.1/16'\nip_range='10.1.0.1/16'\n"):
            with self.subTest(config=config):
                result = self.resolve(config)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_invalid_override_fails_closed(self):
        result = self.resolve("[plugin.network]\nip_range='10.88.0.1/16'\n", "bad")
        self.assertNotEqual(result.returncode, 0)


@unittest.skipUnless(os.environ.get("TERRAFORM") or shutil.which("terraform"), "Terraform not installed")
class EdgeCIDRsTest(unittest.TestCase):
    def evaluate(self, cloud, **overrides):
        values = dict(create_cluster=True, node_proxy_allowed_edge_cidrs="",
                      container_network_type="eni", eni_subnet_cidr="192.168.16.0/20",
                      pod_cidr="10.244.0.0/16", network_addon="terway-eniip",
                      pod_vswitch_ids=[], existing_vswitch_ids=[],
                      vswitch_cidrs=["172.20.0.0/24", "172.20.1.0/24", "172.20.0.0/24"])
        values.update(overrides)
        repo = Path(__file__).resolve().parents[2]
        source = (repo / "deploy/terraform" / cloud / "main.tf").read_text()
        names = ["derived_edge_cidrs", "node_proxy_edge_cidrs"]
        if cloud == "aliyun":
            names.append("use_terway_network")
        expressions = [re.search(r"^  " + name + r"\s*=.*$", source, re.MULTILINE).group(0)
                       for name in names]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tf").write_text("locals {\n" + "\n".join(expressions) + "\n}\n")
            (root / "variables.tf.json").write_text(json.dumps({
                "variable": {name: {"default": value} for name, value in values.items()}
            }))
            result = subprocess.run([os.environ.get("TERRAFORM", "terraform"), "console", "-no-color"],
                                    cwd=root, input="local.node_proxy_edge_cidrs\n",
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_managed_networks(self):
        self.assertEqual(self.evaluate("huaweicloud"), "192.168.16.0/20")
        self.assertEqual(self.evaluate("huaweicloud", container_network_type="overlay_l2"), "10.244.0.0/16")
        self.assertEqual(self.evaluate("aliyun"), "172.20.0.0/24,172.20.1.0/24")
        self.assertEqual(self.evaluate("aliyun", network_addon="flannel"), "10.244.0.0/16")

    def test_imported_networks_require_override(self):
        for cloud in ("huaweicloud", "aliyun"):
            self.assertEqual(self.evaluate(cloud, create_cluster=False), "")
            self.assertEqual(self.evaluate(cloud, create_cluster=False,
                                           node_proxy_allowed_edge_cidrs="192.0.2.0/24"), "192.0.2.0/24")
        self.assertEqual(self.evaluate("aliyun", pod_vswitch_ids=["existing-pod-subnet"]), "")
        self.assertEqual(self.evaluate("aliyun", existing_vswitch_ids=["existing-node-subnet"]), "")

    def test_snat_override_wins(self):
        for cloud in ("huaweicloud", "aliyun"):
            self.assertEqual(self.evaluate(cloud, node_proxy_allowed_edge_cidrs="192.0.2.0/24"), "192.0.2.0/24")


if __name__ == "__main__":
    unittest.main()
