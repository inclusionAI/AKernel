#!/usr/bin/env python3
"""Memory-only admission defaults survive standalone configuration preparation."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "prepare_adx_config_pressure", ROOT / "builder/scripts/prepare-adx-config.py"
)
CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG)
DEFAULT_PRESSURE = {"memory_enabled": True, "disk_enabled": False}


class PressureConfigTest(unittest.TestCase):
    def test_standalone_enables_only_memory_pressure(self):
        deployment = yaml.safe_load((ROOT / "builder/config/adx-standalone.yaml").read_text())
        node = deployment["service_overrides"]["adxlet"]["config"]
        self.assertEqual(node.get("pressure"), DEFAULT_PRESSURE)

    def test_preparation_preserves_memory_only_defaults(self):
        for pvm in (False, True):
            for trace in (False, True):
                with self.subTest(pvm=pvm, trace=trace), tempfile.TemporaryDirectory() as directory:
                    destination = Path(directory) / "prepared.yaml"
                    with patch.dict(os.environ, {"ENABLE_TRACE": str(trace).lower()}):
                        CONFIG.configure(
                            ROOT / "builder/config/adx-standalone.yaml",
                            destination, None, "unused", pvm=pvm,
                        )
                    prepared = yaml.safe_load(destination.read_text())
                    node = prepared["service_overrides"]["adxlet"]["config"]
                    self.assertEqual(node.get("pressure"), DEFAULT_PRESSURE)
                    self.assertEqual(node["resource_source"], {"disk_path": "/home/akernel"})

    def test_ascend_replaces_auto_source_and_composes_with_existing_overlays(self):
        source = ROOT / "builder/config/adx-standalone.yaml"
        release_dump = {
            "services": [
                {"role": "coordinator", "config": {"rpc_timeout_seconds": 90}},
                {"role": "adxlet", "config": {
                    "rpc_timeout_seconds": 90,
                    "resource_source": {
                        "kind": "auto", "disk_path": "/home/akernel", "valid_for_seconds": 42,
                    },
                    "pressure": DEFAULT_PRESSURE,
                    "execd_env": {"LITERAL": "${REMAIN_LITERAL}"},
                }},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "prepared.yaml"
            with (
                patch.dict(os.environ, {"ENABLE_TRACE": "true"}),
                patch.object(CONFIG.subprocess, "run") as run,
            ):
                run.side_effect = [
                    type("Result", (), {"stdout": yaml.safe_dump(release_dump)})(),
                    type("Result", (), {"stdout": "afs-dfs fuse.afs rw"})(),
                ]
                CONFIG.configure(
                    source, destination, Path("/mnt/akernel-shared"), "checkpoints/test",
                    ascend=True,
                )
            self.assertEqual(run.call_args_list[0].args[0][-2:], ["config", "dump"])
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            coordinator, adxlet = yaml.safe_load(destination.read_text())["services"]
            node = adxlet["config"]
            self.assertEqual(node["resource_source"], {
                "kind": "sandboxd", "socket": "/var/run/resource.sock", "valid_for_seconds": 42,
            })
            self.assertEqual(node["pressure"], DEFAULT_PRESSURE)
            self.assertEqual(node["rpc_timeout_seconds"], 90)
            self.assertEqual(node["execd_env"]["LITERAL"], "$${REMAIN_LITERAL}")
            self.assertEqual(node["execd_env"]["ADX_TRACE_ENABLED"], "true")
            self.assertEqual(node["checkpoint_storage"], coordinator["config"]["checkpoint_reclaimers"][0])

    def test_preparation_preserves_operator_pressure_policy(self):
        policy = {
            "memory_enabled": False,
            "disk_enabled": False,
            "thresholds": {"memory_high_percent": 95, "memory_low_percent": 85},
        }
        deployment = yaml.safe_load((ROOT / "builder/config/adx-standalone.yaml").read_text())
        deployment["service_overrides"]["adxlet"]["config"]["pressure"] = policy
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "source.yaml", Path(directory) / "prepared.yaml"
            source.write_text(yaml.safe_dump(deployment))
            with patch.dict(os.environ, {"ENABLE_TRACE": "true"}):
                CONFIG.configure(source, destination, None, "unused", pvm=True)
            prepared = yaml.safe_load(destination.read_text())
        self.assertEqual(prepared["service_overrides"]["adxlet"]["config"]["pressure"], policy)


if __name__ == "__main__":
    unittest.main()
