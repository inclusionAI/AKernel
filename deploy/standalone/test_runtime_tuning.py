#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Check operator overrides, section ownership, and atomic rejection."""

import configparser
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("configure_runtime", ROOT / "configure-runtime.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
TEMPLATE = (ROOT / "config/sandboxd_config.toml").read_text()


def sections(text):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string("[root]\n" + text)
    return parser


class RuntimeTuningTest(unittest.TestCase):
    def test_unset_controls_preserve_template_and_nil_extra_args(self):
        self.assertEqual(MODULE.render(TEMPLATE, {}), TEMPLATE)
        self.assertNotIn("extra_args", sections(TEMPLATE)["plugin.runtime.runsc"])

    def test_controls_are_scoped_and_preserve_other_runtime_configuration(self):
        args = ["--net-raw=true", "--restore-page-prefetch=none", "--restore-map-unit=65536"]
        rendered = sections(MODULE.render(TEMPLATE, {
            "AKERNEL_FIRECRACKER_CHECKPOINT_CONCURRENCY": "8",
            "AKERNEL_FIRECRACKER_RESTORE_CONCURRENCY": "32",
            "AKERNEL_RUNSC_CLI_GOMAXPROCS": "2",
            "AKERNEL_RUNSC_EXTRA_ARGS": json.dumps(args),
        }))
        self.assertEqual(rendered["plugin.resource"]["firecracker_checkpoint_concurrency"], "8")
        self.assertEqual(rendered["plugin.resource"]["firecracker_restore_concurrency"], "32")
        self.assertEqual(rendered["plugin.runtime.runsc"]["cli_gomaxprocs"], "2")
        self.assertEqual(json.loads(rendered["plugin.runtime.runsc"]["extra_args"]), args)
        self.assertEqual(rendered["plugin.runtime.runsc"]["background_restore"], "true")
        self.assertNotIn("cli_gomaxprocs", rendered["plugin.resource"])
        self.assertEqual(dict(rendered["plugin.runtime.firecracker"]), dict(sections(TEMPLATE)["plugin.runtime.firecracker"]))

    def test_zero_and_explicit_empty_args_are_distinct_from_unset(self):
        rendered = sections(MODULE.render(TEMPLATE, {
            "AKERNEL_FIRECRACKER_RESTORE_CONCURRENCY": "0",
            "AKERNEL_RUNSC_EXTRA_ARGS": "[]",
        }))
        self.assertEqual(rendered["plugin.resource"]["firecracker_restore_concurrency"], "0")
        self.assertEqual(rendered["plugin.runtime.runsc"]["extra_args"], "[]")

    def test_integer_boundaries_and_bad_values(self):
        for name, section, key in MODULE.CONTROLS:
            for value in ("0", "1", "1024"):
                with self.subTest(name=name, value=value):
                    self.assertEqual(sections(MODULE.render(TEMPLATE, {name: value}))[section][key], value)
            for value in ("-1", "1025", "9999999999999999999999", "1.5", "1e2", "true", "01", " 2", "2\n"):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ValueError, "between 0 and 1024"):
                        MODULE.render(TEMPLATE, {name: value})

    def test_extra_args_types_and_unicode(self):
        for value in ("null", "true", '"--flag"', "{}", "[1]", '["\\u0000"]', '["\\ud800"]', "["):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.render(TEMPLATE, {"AKERNEL_RUNSC_EXTRA_ARGS": value})
        args = ["--path=/tmp/中文/😀", "--text=quote\"\t\n", "--text=\x7f"]
        output = sections(MODULE.render(TEMPLATE, {"AKERNEL_RUNSC_EXTRA_ARGS": json.dumps(args)}))
        self.assertEqual(json.loads(output["plugin.runtime.runsc"]["extra_args"]), args)

    def test_missing_duplicate_misplaced_marker_and_existing_key_reject(self):
        marker = MODULE.MARKERS["plugin.resource"]
        cases = [
            TEMPLATE.replace(marker, ""),
            TEMPLATE.replace(marker, marker + "\n" + marker),
            TEMPLATE.replace(marker, "").replace("[plugin.runtime.runsc]", "[plugin.runtime.runsc]\n" + marker),
            TEMPLATE.replace(marker, marker + "\nfirecracker_checkpoint_concurrency=1"),
        ]
        for template in cases:
            with self.subTest(template=template), self.assertRaises(ValueError):
                MODULE.render(template, {"AKERNEL_FIRECRACKER_CHECKPOINT_CONCURRENCY": "8"})

    def test_invalid_cli_keeps_existing_file_and_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(TEMPLATE)
            path.chmod(0o640)
            environment = {key: value for key, value in os.environ.items() if not key.startswith("AKERNEL_")}
            environment["AKERNEL_FIRECRACKER_RESTORE_CONCURRENCY"] = "1025"
            result = subprocess.run(["python3", str(ROOT / "configure-runtime.py"), str(path)], env=environment, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_text(), TEMPLATE)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(Path(temporary).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
