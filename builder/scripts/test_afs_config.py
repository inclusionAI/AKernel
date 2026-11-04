#!/usr/bin/env python3
"""AFS mount readiness and shared checkpoint profile regression tests."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOST = load("afs_host", ROOT / "deploy/standalone/validate-afs.py")
CONFIG = load("afs_config", ROOT / "builder/scripts/prepare-adx-config.py")


class AfsConfigTest(unittest.TestCase):
    def assert_adx_valid(self, target):
        binary = os.environ.get("AKERNEL_TEST_ADXCTL")
        if binary:
            subprocess.run([binary, "--config", str(target), "validate"], check=True)

    def test_trace_preserves_explicit_services_and_only_changes_present_roles(self):
        for profile in ("standalone-managed-redis", "node", "coordinator", "ingress-api"):
            source = ROOT / f"src/agent-dx/build/config/examples/deployment-{profile}.yaml"
            original = yaml.safe_load(source.read_text())
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ENABLE_TRACE": "true"}):
                target = Path(directory) / "result.yaml"
                CONFIG.configure(source, target, None, "unused", pvm=True)
                result = yaml.safe_load(target.read_text())
                self.assert_adx_valid(target)
                self.assertNotIn("service_overrides", result)
                self.assertEqual([(v["id"], v["role"]) for v in result["services"]], [(v["id"], v["role"]) for v in original["services"]])
                for service in result["services"]:
                    role = service["role"]
                    if role in ("coordinator", "adxlet", "apiserver", "ingress"):
                        self.assertEqual(service["env"]["ADX_TRACE_ENABLED"], "true")
                    if role in ("coordinator", "adxlet"):
                        self.assertEqual(service["config"]["rpc_timeout_seconds"], 90)
                    if role == "adxlet":
                        self.assertEqual(service["config"]["execd_env"]["ADX_TRACE_ENABLED"], "true")

    def test_trace_preserves_profile_role_boundaries(self):
        expected = {"standalone": {"coordinator", "apiserver", "adxlet", "ingress"},
                    "standalone-external-redis": {"coordinator", "apiserver", "adxlet", "ingress"},
                    "node": {"adxlet"}, "coordinator": {"coordinator"},
                    "ingress-api": {"apiserver", "ingress"}}
        for profile, roles in expected.items():
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ENABLE_TRACE": "true"}):
                source = Path(directory) / "source.yaml"
                original = {"schema_version": 1, "profile": profile}
                if profile == "node":
                    original["service_overrides"] = {"adxlet": {"config": {"node_id": "worker-1", "execd_env": {"PRESERVED": "yes"}}}}
                source.write_text(yaml.safe_dump(original))
                target = Path(directory) / "result.yaml"
                CONFIG.configure(source, target, None, "unused", pvm=True)
                result = yaml.safe_load(target.read_text())
                self.assert_adx_valid(target)
                self.assertEqual(set(result["service_overrides"]), roles)
                if profile == "node":
                    self.assertEqual(result["service_overrides"]["adxlet"]["config"]["execd_env"]["PRESERVED"], "yes")

    def test_no_options_preserves_custom_input_exactly(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ENABLE_TRACE": "false"}):
            for original in ({"schema_version": 1, "profile": "coordinator"},
                             {"schema_version": 1, "services": [{"id": "worker", "role": "adxlet", "config": {"node_id": "worker"}}]}):
                source = Path(directory) / "source.yaml"
                source.write_text(yaml.safe_dump(original))
                CONFIG.configure(source, source, None, "unused")
                self.assertEqual(yaml.safe_load(source.read_text()), original)

    def test_explicit_null_config_and_multiple_workers_are_patched_in_place(self):
        original = {"services": [
            {"id": "first", "role": "adxlet", "config": None},
            {"id": "second", "role": "adxlet"},
            {"id": "redis", "role": "redis", "config": None},
        ]}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ENABLE_TRACE": "true"}):
            source = Path(directory) / "source.yaml"
            source.write_text(yaml.safe_dump(original))
            CONFIG.configure(source, source, None, "unused", pvm=True)
            result = yaml.safe_load(source.read_text())
            self.assertNotIn("service_overrides", result)
            for service in result["services"][:2]:
                self.assertEqual(service["config"]["rpc_timeout_seconds"], 90)
                self.assertEqual(service["config"]["execd_env"]["ADX_TRACE_ENABLED"], "true")
            self.assertEqual(result["services"][2], original["services"][2])

    def test_profile_selectors_expand_values_and_defaults_without_rewriting(self):
        for value, expected in ((None, "node"), ("", "node"), ("coordinator", "coordinator"),
                                ("ingress-api", "ingress-api")):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source.yaml"
                target = Path(directory) / "result.yaml"
                cfg = {"schema_version": 1, "profile": "${REVIEW_PROFILE:-node}"}
                if expected == "node":
                    cfg["service_overrides"] = {"adxlet": {"config": {"node_id": "worker"}}}
                source.write_text(yaml.safe_dump(cfg))
                env = {"ENABLE_TRACE": "true"}
                if value is not None:
                    env["REVIEW_PROFILE"] = value
                with patch.dict(os.environ, env):
                    if value is None:
                        os.environ.pop("REVIEW_PROFILE", None)
                    CONFIG.configure(source, target, None, "unused", pvm=True)
                    self.assert_adx_valid(target)
                result = yaml.safe_load(target.read_text())
                self.assertEqual(result["profile"], cfg["profile"])
                expected_roles = {"node": {"adxlet"}, "coordinator": {"coordinator"},
                                  "ingress-api": {"apiserver", "ingress"}}[expected]
                self.assertEqual(set(result["service_overrides"]), expected_roles)

    def test_explicit_role_selectors_expand_once_and_preserve_other_templates(self):
        for value in (None, "", "adxlet"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                cfg = yaml.safe_load((ROOT / "src/agent-dx/build/config/examples/deployment-node.yaml").read_text())
                service = cfg["services"][0]
                service["role"] = "${REVIEW_ROLE:-adxlet}"
                service["config"]["node_id"] = "${REVIEW_NODE_ID:-worker}"
                service["env"]["KEEP_LITERAL"] = "$${REVIEW_LITERAL}"
                source = Path(directory) / "source.yaml"
                target = Path(directory) / "result.yaml"
                source.write_text(yaml.safe_dump(cfg))
                env = {"ENABLE_TRACE": "true", "REVIEW_LITERAL": "not-expanded"}
                if value is not None:
                    env["REVIEW_ROLE"] = value
                with patch.dict(os.environ, env):
                    if value is None:
                        os.environ.pop("REVIEW_ROLE", None)
                    CONFIG.configure(source, target, None, "unused", pvm=True)
                    self.assert_adx_valid(target)
                result = yaml.safe_load(target.read_text())["services"][0]
                self.assertEqual(result["role"], service["role"])
                self.assertEqual(result["config"]["node_id"], service["config"]["node_id"])
                self.assertEqual(result["env"]["KEEP_LITERAL"], "$${REVIEW_LITERAL}")
                self.assertEqual(result["env"]["ADX_TRACE_ENABLED"], "true")
                self.assertEqual(result["config"]["rpc_timeout_seconds"], 90)
                self.assertEqual(result["config"]["execd_env"]["ADX_TRACE_ENABLED"], "true")

    def test_bound_and_concatenated_profile_selectors_match_adx(self):
        for selector in ("${REVIEW_PROFILE}", "${REVIEW_PREFIX:-n}${REVIEW_SUFFIX:-ode}"):
            with self.subTest(selector=selector), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source.yaml"
                target = Path(directory) / "result.yaml"
                cfg = {"schema_version": 1, "profile": selector,
                       "service_overrides": {"adxlet": {"config": {"node_id": "worker"}}}}
                source.write_text(yaml.safe_dump(cfg))
                with patch.dict(os.environ, {"ENABLE_TRACE": "true", "REVIEW_PROFILE": "node",
                                              "REVIEW_PREFIX": "n", "REVIEW_SUFFIX": "ode"}):
                    CONFIG.configure(source, target, None, "unused")
                    self.assert_adx_valid(target)
                result = yaml.safe_load(target.read_text())
                self.assertEqual(result["profile"], selector)
                self.assertEqual(result["service_overrides"]["adxlet"]["env"]["ADX_TRACE_ENABLED"], "true")

    def test_unbound_invalid_and_nonrecursive_selectors_fail_before_writing(self):
        for selector in ("${REVIEW_UNBOUND}", "${9_INVALID:-node}", "${REVIEW_UNBOUND",
                         "${REVIEW_NESTED}", "$${REVIEW_OTHER}", "$REVIEW_OTHER"):
            with self.subTest(selector=selector), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source.yaml"
                target = Path(directory) / "result.yaml"
                source.write_text(yaml.safe_dump({"schema_version": 1, "profile": selector}))
                original = source.read_text()
                with patch.dict(os.environ, {"ENABLE_TRACE": "true", "REVIEW_NESTED": "${REVIEW_OTHER}",
                                              "REVIEW_OTHER": "node"}):
                    os.environ.pop("REVIEW_UNBOUND", None)
                    with self.assertRaises(ValueError):
                        CONFIG.configure(source, target, None, "unused")
                self.assertFalse(target.exists())
                self.assertEqual(source.read_text(), original)

    def test_host_requires_an_actual_writable_dfs_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            for output, valid in (("afs-dfs fuse rw,nosuid\n", True),
                                  ("/dev/root ext4 rw\n", False),
                                  ("afs-ownerfs fuse rw\n", False),
                                  ("afs-dfs fuse ro\n", False)):
                with self.subTest(output=output), patch.object(HOST.subprocess, "run") as run:
                    run.return_value.stdout = output
                    if valid:
                        self.assertEqual(HOST.validate(directory, "acceptance/checkpoints"), str(Path(directory).resolve()))
                    else:
                        with self.assertRaises(ValueError):
                            HOST.validate(directory, "acceptance/checkpoints")
            with patch.object(HOST.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "findmnt")):
                with self.assertRaises(subprocess.CalledProcessError):
                    HOST.validate(directory, "acceptance/checkpoints")

    def test_invalid_prefix_is_rejected_before_mount_query(self):
        with tempfile.TemporaryDirectory() as directory:
            for prefix in ("../data", "/data", "data//checkpoints", "data;exit"):
                with self.subTest(prefix=prefix), patch.object(HOST.subprocess, "run") as run:
                    with self.assertRaises(ValueError):
                        HOST.validate(directory, prefix)
                    run.assert_not_called()

    def test_profile_shares_checkpoint_store_and_preserves_local_runtime_paths(self):
        for pvm in (False, True):
            with self.subTest(pvm=pvm), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "afs.yaml"
                source = ROOT / "builder/config/adx-standalone.yaml"
                original = yaml.safe_load(source.read_text())
                with patch.object(CONFIG.subprocess, "run") as run:
                    run.return_value.stdout = "afs-dfs fuse rw\n"
                    CONFIG.configure(source, target, Path("/mnt/akernel-shared"), "test/checkpoints", pvm=pvm)
                profile = yaml.safe_load(target.read_text())
                overrides = profile["service_overrides"]
                storage = overrides["adxlet"]["config"]["checkpoint_storage"]
                self.assertEqual(storage, overrides["coordinator"]["config"]["checkpoint_reclaimers"][0])
                self.assertEqual(storage["kind"], "distributed_fs")
                self.assertIsNone(overrides["adxlet"]["config"]["checkpoint_dir"])
                self.assertEqual(profile["runtime_profile"], original["runtime_profile"])
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_pvm_only_changes_rpc_budgets_and_supports_in_place_generation(self):
        source = ROOT / "builder/config/adx-standalone.yaml"
        original = yaml.safe_load(source.read_text())
        for pvm in (False, True):
            with self.subTest(pvm=pvm), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "profile.yaml"
                target.write_text(source.read_text())
                with patch.dict(os.environ, {"ENABLE_TRACE": "false"}):
                    CONFIG.configure(target, target, None, "unused", pvm=pvm)
                result = yaml.safe_load(target.read_text())
                overrides = result["service_overrides"]
                self.assertEqual(overrides["coordinator"]["config"]["autoscaling_listen"], "127.0.0.1:19092")
                self.assertEqual(overrides["apiserver"]["config"]["metrics_listen"], "127.0.0.1:19093")
                if pvm:
                    for role in ("coordinator", "adxlet"):
                        self.assertEqual(overrides[role]["config"].pop("rpc_timeout_seconds"), 90)
                self.assertEqual(result, original)

    def test_standalone_trace_flag_configures_processes_and_guest_without_afs(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ENABLE_TRACE": "true"}):
            target = Path(directory) / "trace.yaml"
            with patch.object(CONFIG.subprocess, "run") as run:
                CONFIG.configure(ROOT / "builder/config/adx-standalone.yaml", target, None, "unused")
                run.assert_not_called()
            overrides = yaml.safe_load(target.read_text())["service_overrides"]
            for role in ("coordinator", "apiserver", "adxlet", "ingress"):
                self.assertEqual(overrides[role]["env"]["ADX_TRACE_ENABLED"], "true")
                self.assertEqual(overrides[role]["env"]["ADX_TRACE_SAMPLE_RATIO"], "0.1")
            node = overrides["adxlet"]["config"]
            self.assertEqual(node["execd_env"]["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://10.88.0.1:4318")
            self.assertEqual(node["execd_env"]["ADX_EXECD_CONTROL_SOCKET_PATH"], "/run/akernel")
            self.assertNotIn("checkpoint_storage", node)

    def test_container_rechecks_mount_before_enabling_shared_storage(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(CONFIG.subprocess, "run") as run:
            run.return_value.stdout = "/dev/root ext4 rw\n"
            target = Path(directory) / "afs.yaml"
            with self.assertRaises(ValueError):
                CONFIG.configure(ROOT / "builder/config/adx-standalone.yaml", target,
                                 Path("/mnt/akernel-shared"), "test/checkpoints")
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
