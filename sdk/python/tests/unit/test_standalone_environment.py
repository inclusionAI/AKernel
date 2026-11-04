# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Keep generated standalone exports compatible with the public SDK."""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location(
    "standalone_environment_manager", ROOT / "deploy/standalone/manage.py"
)
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)


class StandaloneEnvironmentTest(unittest.TestCase):
    def test_generated_environment_imports_this_checkout_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manage.Manager(directory, {"PATH": os.environ["PATH"]})
            manager.data.mkdir(parents=True)
            manager.data.joinpath("token").write_text("fixture-private-token\n")
            manager.data.joinpath("sdk.env").write_text(
                "export AKERNEL_SERVER_ADDRESS=127.0.0.1:24443\n"
            )
            with contextlib.redirect_stdout(io.StringIO()):
                manager.sdk_environment("127.0.0.1:24443")
            result = subprocess.run(
                [
                    "bash", "-c", 'source "$1"; exec "$2" -c "$3"',
                    "sdk-env-check", str(manager.data / "sdk-env.sh"),
                    sys.executable,
                    "from akernel_sdk._backends.registry import selected_backend; "
                    "print(selected_backend())",
                ],
                env={
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": str(ROOT / "sdk/python"),
                },
                capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "adx")
            self.assertNotIn("fixture-private-token", result.stdout + result.stderr)
