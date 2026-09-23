#!/usr/bin/env python3

"""Contract tests for the Kubernetes Agent DX identity helper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("ensure-adx-secret.sh")


class EnsureAdxSecretTest(unittest.TestCase):
    def test_creates_all_required_secret_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = root / "calls.jsonl"
            captured = root / "secret"
            kubectl = root / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(args) + '\\n')
if 'get' in args:
    raise SystemExit(1)
capture = pathlib.Path(os.environ['CAPTURE'])
capture.mkdir(exist_ok=True)
for arg in args:
    if arg.startswith('--from-file='):
        key, filename = arg.removeprefix('--from-file=').split('=', 1)
        path = pathlib.Path(filename)
        if not path.is_file() or path.stat().st_size == 0:
            raise SystemExit(3)
        shutil.copyfile(path, capture / key)
""",
                encoding="utf-8",
            )
            kubectl.chmod(0o755)
            environment = dict(
                os.environ,
                KUBECTL=str(kubectl),
                CALLS=str(calls),
                CAPTURE=str(captured),
            )

            subprocess.run(
                [SCRIPT, "--namespace", "test-ns", "--name", "test-adx"],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )

            invocations = [json.loads(line) for line in calls.read_text().splitlines()]
            create = next(args for args in invocations if "generic" in args)
            keys = {
                argument.removeprefix("--from-file=").split("=", 1)[0]
                for argument in create
                if argument.startswith("--from-file=")
            }
            self.assertEqual(keys, {"public.pem", "public.key", "admin-key"})
            subprocess.run(
                ["openssl", "verify", "-CAfile", captured / "public.pem", captured / "public.pem"],
                check=True, capture_output=True, text=True,
            )

            certificate = subprocess.run(
                ["openssl", "x509", "-in", captured / "public.pem", "-text", "-noout"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("DNS:adx.internal", certificate)


if __name__ == "__main__":
    unittest.main()
