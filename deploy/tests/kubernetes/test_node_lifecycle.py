"""Lifecycle probe contracts, including authentication against an isolated Redis."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.parse
import uuid

# SDK objects are not used by these probe contracts.
with patch.dict(sys.modules, adx_environment=Mock()):
    spec = importlib.util.spec_from_file_location("node_lifecycle", Path(__file__).with_name("node_lifecycle.py"))
    lifecycle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lifecycle)


class SharedStorageTest(unittest.TestCase):
    def acceptance(self, path):
        acceptance = lifecycle.Acceptance(argparse.Namespace(shared_storage_path=path))
        acceptance.nodes = ["node-a", "node-b"]
        acceptance.node_pod = lambda node: {"metadata": {"name": node + "-pod"}}
        acceptance.execute = Mock()
        return acceptance

    def test_local_profile_skips_shared_storage(self):
        acceptance = self.acceptance(None)
        acceptance.check_shared_storage()
        acceptance.execute.assert_not_called()

    def test_custom_shared_path_is_passed_as_data_on_each_node(self):
        path = "/custom mount/checkpoints'"
        acceptance = self.acceptance(path)
        acceptance.check_shared_storage()
        self.assertEqual(acceptance.execute.call_count, 2)
        for call in acceptance.execute.call_args_list:
            args = call.args
            self.assertEqual(args[-1], path)
            with tempfile.TemporaryDirectory() as root:
                directory = Path(root) / "custom mount'"
                directory.mkdir()
                subprocess.run([sys.executable, "-c", args[-2], str(directory)], check=True)

    def test_missing_selected_mount_fails_acceptance(self):
        acceptance = self.acceptance("/missing")
        acceptance.execute.side_effect = RuntimeError("stat failed")
        with self.assertRaisesRegex(RuntimeError, "stat failed"):
            acceptance.check_shared_storage()


class RedisProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "akernel-lifecycle-redis-" + uuid.uuid4().hex[:12]
        subprocess.run(["docker", "run", "-d", "--rm", "--name", cls.name,
                        "-p", "127.0.0.1::6379", "redis:7.4-alpine"], check=True, stdout=subprocess.DEVNULL)
        cls.addClassCleanup(lambda: subprocess.run(["docker", "rm", "-f", cls.name], check=True, stdout=subprocess.DEVNULL))
        raw = subprocess.check_output(["docker", "port", cls.name, "6379/tcp"], text=True)
        cls.port = int(raw.strip().rsplit(":", 1)[1])
        for _ in range(50):
            if subprocess.run(["docker", "exec", cls.name, "redis-cli", "ping"], capture_output=True).stdout.strip() == b"PONG":
                break
            time.sleep(.1)
        else:
            raise RuntimeError("isolated Redis did not become ready")

    def cli(self, *args):
        result = subprocess.run(["docker", "exec", self.name, "redis-cli", *args], check=True, capture_output=True, text=True)
        self.assertNotIn("ERR", result.stdout)
        return result.stdout

    def test_real_redis_authentication_modes(self):
        # Test-only credentials; the account name and password both require URL decoding.
        username, password = "probe@user", "pass:/@word"
        self.cli("ACL", "SETUSER", username, "on", ">" + password, "~*", "+@all")
        self.cli("SELECT", "3")
        self.cli("-n", "3", "HSET", "adx:{fixture}:control:v1", "header", '{"epoch":9}')
        def probe(userinfo):
            output = io.StringIO()
            url = f"redis://{userinfo}127.0.0.1:{self.port}/3"
            with patch.dict(os.environ, ADX_REDIS_URL=url), patch("builtins.open", return_value=io.StringIO("namespace: fixture\n")), contextlib.redirect_stdout(output):
                exec(lifecycle.REDIS_PROBE, {})
            self.assertEqual(json.loads(output.getvalue())["epoch"], 9)
        probe("")
        self.cli("ACL", "SETUSER", "default", ">legacy-password")
        probe(":legacy-password@")
        self.cli("-a", "legacy-password", "ACL", "SETUSER", "default", "off")
        probe(urllib.parse.quote(username, safe="") + ":" + urllib.parse.quote(password, safe="") + "@")
        with self.assertRaisesRegex(RuntimeError, "Redis read rejected"):
            probe(urllib.parse.quote(username, safe="") + ":wrong@")


if __name__ == "__main__":
    unittest.main()
