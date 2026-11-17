#!/usr/bin/env python3
"""Render runtime controls and reject unsafe or ambiguous configuration."""

import configparser
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


CHART = Path(__file__).resolve().parents[1]
DEFAULT = yaml.safe_load((CHART / "values.yaml").read_text())["node"]["config"]["sandboxd"]["config"]


def render(tuning=None, config=None):
    sandboxd = {"runtimeTuning": tuning if tuning is not None else {}}
    if config is not None:
        sandboxd["config"] = config
    with tempfile.TemporaryDirectory() as temporary:
        values = Path(temporary) / "values.yaml"
        values.write_text(yaml.safe_dump({"node": {"config": {"sandboxd": sandboxd}}}))
        return subprocess.run(["helm", "template", "akernel", str(CHART), "--set", "adx.tls.existingSecret=test-tls", "-f", str(values)], capture_output=True, text=True, timeout=20)


def config_sections(output):
    resources = list(yaml.safe_load_all(output))
    config = next(r["data"]["sandboxd_config.toml"] for r in resources if r and r["kind"] == "ConfigMap" and r["metadata"]["name"] == "akernel-node-config")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string("[root]\n" + config)
    return parser


class RuntimeTuningChartTest(unittest.TestCase):
    def test_unset_preserves_defaults(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = config_sections(result.stdout)
        self.assertNotIn("extra_args", config["plugin.runtime.runsc"])
        self.assertNotIn("firecracker_checkpoint_concurrency", config["plugin.resource"])

    def test_controls_are_in_their_own_sections(self):
        args = ["--net-raw=true", "--restore-page-prefetch=false", "--restore-map-unit=65536"]
        result = render({"firecrackerCheckpointConcurrency": 8, "firecrackerRestoreConcurrency": 32, "runscCliGomaxprocs": 2, "runscExtraArgs": args})
        self.assertEqual(result.returncode, 0, result.stderr)
        config = config_sections(result.stdout)
        self.assertEqual(config["plugin.resource"]["firecracker_checkpoint_concurrency"], "8")
        self.assertEqual(config["plugin.resource"]["firecracker_restore_concurrency"], "32")
        self.assertEqual(config["plugin.runtime.runsc"]["cli_gomaxprocs"], "2")
        self.assertEqual(json.loads(config["plugin.runtime.runsc"]["extra_args"]), args)
        self.assertEqual(config["plugin.runtime.runsc"]["background_restore"], "true")

    def test_zero_and_explicit_empty_args(self):
        result = render({"firecrackerRestoreConcurrency": 0, "runscExtraArgs": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        config = config_sections(result.stdout)
        self.assertEqual(config["plugin.resource"]["firecracker_restore_concurrency"], "0")
        self.assertEqual(config["plugin.runtime.runsc"]["extra_args"], "[]")

    def test_bad_limits_and_arg_types_reject(self):
        for key in ("firecrackerCheckpointConcurrency", "firecrackerRestoreConcurrency", "runscCliGomaxprocs"):
            for value in (-1, 1025, "9999999999999999999", "1.5", "true", "01"):
                with self.subTest(key=key, value=value):
                    result = render({key: value})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("between 0 and 1024", result.stderr)
        for value in (True, "[]", [1], {}):
            with self.subTest(args=value):
                result = render({"runscExtraArgs": value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("array of strings", result.stderr)

    def test_marker_scope_and_duplicate_keys_reject(self):
        marker = "# AKERNEL_RUNTIME_RESOURCE_TUNING"
        for config in (DEFAULT.replace(marker, ""), DEFAULT.replace(marker, marker + "\n" + marker), DEFAULT.replace(marker, "").replace("[plugin.runtime.runsc]", "[plugin.runtime.runsc]\n" + marker), DEFAULT.replace(marker, marker + "\nfirecracker_restore_concurrency=1")):
            with self.subTest(config=config):
                self.assertNotEqual(render({"firecrackerRestoreConcurrency": 32}, config).returncode, 0)

    def test_unknown_control_rejects(self):
        result = render({"firecrackerRestoreConcurency": 32})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown", result.stderr)

    def test_tuning_must_be_an_object(self):
        for value in (False, [], "enabled"):
            with self.subTest(value=value):
                result = render(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be an object", result.stderr)

    def test_nul_argument_is_rejected_before_runtime_launch(self):
        result = render({"runscExtraArgs": ["--flag=\0"]})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NUL", result.stderr)


if __name__ == "__main__":
    unittest.main()
