# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
"""Exercise the launcher's config generation without starting containers."""

import configparser
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


STANDALONE_DIR = Path(__file__).resolve().parent
DIRECT_MARKER = "# AKERNEL_DIRECT_RESOLVER"
DIRECT_PATH = '"/home/akernel/sandboxd/config/direct-resolv.conf"'


class StartConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config_dir = self.directory / "config"
        self.config_dir.mkdir()
        self.template = self.config_dir / "sandboxd_config.toml"
        self.template.write_text(
            (STANDALONE_DIR / "config/sandboxd_config.toml").read_text()
        )
        self.data_dir = self.directory / "data"
        self.resolver_output = self.data_dir / "sandboxd/config/direct-resolv.conf"
        self.resolver_output.parent.mkdir(parents=True)
        self.config_output = self.data_dir / "sandboxd/config.toml"
        self.source = self.directory / "approved resolver.conf"
        self.resolver_content = (
            "nameserver 192.0.2.53\nsearch corp.example\noptions ndots:2\n"
        )
        self.source.write_text(self.resolver_content)
        script = (STANDALONE_DIR / "start.sh").read_text()
        # Extract only the real generator; the launcher's main sequence must
        # never run in an unprivileged unit test.
        function = re.search(
            r"^configure_network\(\) \{\n.*?^\}", script, re.MULTILINE | re.DOTALL
        )
        self.assertIsNotNone(function)
        self.function = function.group()

    def generate(self, **overrides):
        environment = dict(os.environ)
        environment.pop("BASH_ENV", None)
        environment.update(
            SCRIPT_DIR=str(STANDALONE_DIR),
            CONFIG_DIR=str(self.config_dir),
            DATA_DIR=str(self.data_dir),
            SANDBOXD_CONFIG_FILE=str(self.config_output),
            AKERNEL_NAT_BACKEND="iptables",
            AKERNEL_ENABLE_RUNC="true",
            AKERNEL_FIRECRACKER_BACKEND="kvm",
            AKERNEL_RUNC_RESOLV_CONF=str(self.source),
            AKERNEL_CHUNK_DB_SIZE="",
        )
        environment.update(overrides)
        return subprocess.run(
            [
                "bash",
                "-e",
                "-c",
                "log_info() { :; }; log_warn() { :; }; "
                'log_error() { printf "%s\\n" "$1" >&2; };\n'
                + self.function
                + "\nconfigure_network\n",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def read_sections(self):
        # The template uses simple section/key assignments. Inspect their
        # placement without requiring a TOML dependency on Python 3.10.
        config = configparser.ConfigParser(interpolation=None)
        config.read_string("[root]\n" + self.config_output.read_text())
        return config

    def assert_rejected_without_replacing_config(self, message):
        self.config_output.write_text("existing config\n")
        result = self.generate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertEqual(self.config_output.read_text(), "existing config\n")
        self.assertFalse(self.resolver_output.exists())

    def test_pvm_selection_preserves_direct_resolver_configuration(self):
        result = self.generate(AKERNEL_FIRECRACKER_BACKEND="pvm")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.read_sections()
        self.assertIn("plugin.runtime.firecracker_pvm", config)
        self.assertNotIn("plugin.runtime.firecracker", config)
        self.assertIn("firecracker-pvm", config["plugin.runtime.runtime_binary"])
        self.assertNotIn("firecracker", config["plugin.runtime.runtime_binary"])
        self.assertNotIn("kata", config["plugin.runtime.runtime_binary"])
        self.assertEqual(self.resolver_output.read_text(), self.resolver_content)

    def test_direct_source_is_under_runtime_and_node_source_is_preserved(self):
        for backend in ("iptables", "bpfnat"):
            with self.subTest(backend=backend):
                result = self.generate(AKERNEL_NAT_BACKEND=backend)
                self.assertEqual(result.returncode, 0, result.stderr)
                config = self.read_sections()
                self.assertEqual(
                    config["plugin.network"]["nat_backend"], f'"{backend}"'
                )
                self.assertEqual(config["plugin.network"]["enable_network_acl"], "true")
                self.assertEqual(
                    config["plugin.runtime"]["resolv_conf_path"], '"/etc/resolv.conf"'
                )
                self.assertEqual(
                    config["plugin.runtime"]["direct_resolv_conf_path"], DIRECT_PATH
                )
                self.assertNotIn("resolv_conf_path", config["plugin.runtime.runc"])
                self.assertNotIn(
                    "direct_resolv_conf_path", config["plugin.runtime.runc"]
                )
                self.assertEqual(
                    config["plugin.runtime.runtime_binary"]["runc"],
                    '"/usr/local/bin/runc"',
                )
                self.assertEqual(
                    self.resolver_output.read_text(), self.resolver_content
                )
                self.assertEqual(self.resolver_output.stat().st_mode & 0o777, 0o644)

    def test_runc_disabled_does_not_read_or_generate_a_direct_source(self):
        result = self.generate(
            AKERNEL_ENABLE_RUNC="false",
            AKERNEL_RUNC_RESOLV_CONF=str(self.directory / "missing.conf"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.read_sections()
        self.assertNotIn("direct_resolv_conf_path", config["plugin.runtime"])
        self.assertNotIn("runc", config["plugin.runtime.runtime_binary"])
        self.assertEqual(
            config["plugin.runtime"]["resolv_conf_path"], '"/etc/resolv.conf"'
        )
        self.assertFalse(self.resolver_output.exists())

    def test_custom_node_resolver_is_not_replaced(self):
        self.template.write_text(
            self.template.read_text().replace(
                'resolv_conf_path="/etc/resolv.conf"',
                'resolv_conf_path="/etc/node-resolv.conf"',
            )
        )
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.read_sections()
        self.assertEqual(
            config["plugin.runtime"]["resolv_conf_path"], '"/etc/node-resolv.conf"'
        )
        self.assertEqual(
            config["plugin.runtime"]["direct_resolv_conf_path"], DIRECT_PATH
        )

    def test_missing_direct_marker_is_rejected(self):
        self.template.write_text(self.template.read_text().replace(DIRECT_MARKER, ""))
        self.assert_rejected_without_replacing_config("# AKERNEL_DIRECT_RESOLVER")

    def test_misplaced_direct_marker_is_rejected(self):
        self.template.write_text(
            self.template.read_text().replace(DIRECT_MARKER, "")
            + "\n"
            + DIRECT_MARKER
            + "\n"
        )
        self.assert_rejected_without_replacing_config("under [plugin.runtime]")

    def test_duplicate_direct_marker_is_rejected(self):
        self.template.write_text(
            self.template.read_text().replace(
                DIRECT_MARKER, f"{DIRECT_MARKER}\n{DIRECT_MARKER}"
            )
        )
        self.assert_rejected_without_replacing_config("exactly one")

    def test_missing_runtime_marker_is_rejected(self):
        self.template.write_text(
            self.template.read_text().replace("# AKERNEL_RUNTIME_RUNC", "")
        )
        self.assert_rejected_without_replacing_config("# AKERNEL_RUNTIME_RUNC")

    def test_invalid_explicit_source_is_rejected(self):
        self.source.write_text("nameserver 127.0.0.11\n")
        self.assert_rejected_without_replacing_config("another network namespace")

    def test_missing_explicit_source_is_rejected(self):
        self.source.unlink()
        self.assert_rejected_without_replacing_config("absolute regular file")


if __name__ == "__main__":
    unittest.main()
