import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class AdxServiceTest(unittest.TestCase):
    def test_inherits_only_adx_container_environment_and_preserves_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "environ"
            source.write_bytes(
                b"AKERNEL_ADX_CONFIG=/etc/akernel/adx-node.yaml\0"
                b"AKERNEL_ADX_MANAGED_CREDENTIALS=external\0"
                b"ADX_REDIS_URL=redis://example/\0"
                b"NODE_NAME=node-from-pod\0INSTANCE_IP=192.0.2.10\0"
                b"UNRELATED_PRIVATE_SETTING=must-not-inherit\0"
            )
            environment = {
                "PATH": os.environ["PATH"],
                "NODE_NAME": "explicit-node",
            }
            script = Path(__file__).with_name("adx-service.sh")
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; load_container_environment "$2"; '
                 'python3 -c "import json, os; print(json.dumps(dict(os.environ)))"',
                 "_", str(script), str(source)],
                check=True, env=environment, capture_output=True, text=True,
            )
            inherited = json.loads(result.stdout)
            self.assertEqual(inherited["AKERNEL_ADX_CONFIG"], "/etc/akernel/adx-node.yaml")
            self.assertEqual(inherited["AKERNEL_ADX_MANAGED_CREDENTIALS"], "external")
            self.assertEqual(inherited["ADX_REDIS_URL"], "redis://example/")
            self.assertEqual(inherited["NODE_NAME"], "explicit-node")
            self.assertEqual(inherited["INSTANCE_IP"], "192.0.2.10")
            self.assertNotIn("UNRELATED_PRIVATE_SETTING", inherited)

    def test_only_generates_and_reuses_public_https_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "adx"
            script = Path(__file__).with_name("adx-service.sh")
            environment = dict(os.environ, AKERNEL_ADX_STATE_DIR=str(state))

            subprocess.run(["bash", "-c", 'source "$1"; ensure_public_tls "$AKERNEL_ADX_STATE_DIR"', "_", str(script)], check=True, env=environment)
            initial = {
                p.relative_to(state): p.read_bytes()
                for p in state.rglob("*")
                if p.is_file()
            }
            subprocess.run(["bash", "-c", 'source "$1"; ensure_public_tls "$AKERNEL_ADX_STATE_DIR"', "_", str(script)], check=True, env=environment)
            self.assertEqual(
                initial,
                {
                    p.relative_to(state): p.read_bytes()
                    for p in state.rglob("*")
                    if p.is_file()
                },
            )

            tls = state / "tls"
            self.assertEqual(
                {p.name for p in tls.iterdir()},
                {"ingress-public.pem", "ingress-public.key"},
            )
            subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-CAfile",
                    tls / "ingress-public.pem",
                    tls / "ingress-public.pem",
                ],
                check=True, capture_output=True, text=True,
            )
            self.assertFalse((state / "secrets/admin-key").exists())


if __name__ == "__main__":
    unittest.main()
