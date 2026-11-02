# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

"""The node fault must target the owned OCI environment exactly once."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run import WRAPPER


class RuntimeDelayScopeTest(unittest.TestCase):
    def run_wrapper(self, root, bundle, command):
        code = WRAPPER.replace("/home/akernel/initialization-qualification", str(root))
        with patch(
            "sys.argv", ["runsc", command, "--bundle", str(bundle), "physical-uuid"]
        ):
            with patch("os.execv") as execute, patch("time.sleep") as delay:
                exec(compile(code, "node-runtime-fixture", "exec"), {})
        execute.assert_called_once_with(
            "/usr/local/bin/runsc.qualification-real",
            [
                "/usr/local/bin/runsc.qualification-real",
                command,
                "--bundle",
                str(bundle),
                "physical-uuid",
            ],
        )
        return delay

    def test_other_environment_and_runtime_operations_do_not_consume_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "config.json").write_text(
                json.dumps(
                    {
                        "process": {"env": ["ADX_ENVIRONMENT_ID=another-environment"]},
                    }
                )
            )
            (root / "armed.json").write_text(
                json.dumps(
                    {
                        "case": "test",
                        "environment_id": "owned",
                        "delay": 35,
                    }
                )
            )
            self.run_wrapper(root, bundle, "create").assert_not_called()
            self.run_wrapper(root, bundle, "boot").assert_not_called()
            self.assertTrue((root / "armed.json").exists())
            self.assertFalse((root / "entered.json").exists())

    def test_owned_environment_consumes_fault_only_once_despite_physical_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "config.json").write_text(
                json.dumps(
                    {
                        "process": {
                            "env": ["ADX_ENVIRONMENT_ID=owned", "SECRET=not-logged"]
                        },
                    }
                )
            )
            (root / "armed.json").write_text(
                json.dumps(
                    {
                        "case": "test",
                        "environment_id": "owned",
                        "delay": 35,
                    }
                )
            )
            self.run_wrapper(root, bundle, "create").assert_called_once_with(35)
            self.run_wrapper(root, bundle, "create").assert_not_called()
            event = (root / "entered.json").read_text()
            self.assertEqual(json.loads(event)["environment_id"], "owned")
            self.assertNotIn("not-logged", event)


if __name__ == "__main__":
    unittest.main()
