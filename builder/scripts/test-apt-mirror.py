#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Exercise APT URI changes on isolated source files without host writes."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().with_name("configure-apt-mirror.sh")
MIRROR_NAMES = (
    "AKERNEL_APT_UBUNTU_MIRROR", "AKERNEL_APT_UBUNTU_PORTS_MIRROR",
    "AKERNEL_APT_DEBIAN_MIRROR", "AKERNEL_APT_DEBIAN_SECURITY_MIRROR",
)
SIGNING_KEY = "Signed-By: /usr/share/keyrings/distribution-archive-keyring.gpg\n"
THIRD_PARTY = "deb [signed-by=/usr/share/keyrings/vendor.gpg] https://vendor.example.test/packages stable main\n"


class AptMirrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="akernel-apt-mirror-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "root with spaces"
        self.sources = self.root / "etc/apt/sources.list.d"
        self.sources.mkdir(parents=True)
        self.os_release = self.root / "etc/os-release"
        self.os_release.write_text("ID=ubuntu\nVERSION_CODENAME=noble\n")
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        dpkg = self.bin / "dpkg"
        dpkg.write_text('#!/bin/sh\n[ "$1" = --print-architecture ] || exit 91\nprintf "%s\\n" "$FIXTURE_ARCH"\n')
        dpkg.chmod(0o755)
        self.environment = {name: value for name, value in os.environ.items()
                            if not name.startswith("AKERNEL_") and name not in ("BASH_ENV", "ENV")}
        self.environment.update(PATH=f"{self.bin}:/usr/bin:/bin", FIXTURE_ARCH="amd64")
        self.third_party = self.sources / "vendor.list"
        self.third_party.write_text(THIRD_PARTY)

    def run_script(self, **values):
        result = subprocess.run(
            ["/bin/sh", str(SCRIPT), str(self.root)], env=self.environment | values,
            cwd=self.directory, capture_output=True, text=True, timeout=10,
        )
        self.assertNotIn("PRIVATE_INVALID_VALUE", result.stdout + result.stderr)
        return result

    def write_source(self, content, name="distribution.sources"):
        path = self.sources / name
        path.write_text(content)
        return path

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.third_party.read_text(), THIRD_PARTY)
        self.assertEqual(list(self.sources.glob("*.??????")), [])

    def test_official_default_preserves_every_source_byte(self):
        source = self.write_source(
            "# retain comments and whitespace\nTypes: deb deb-src\n"
            "URIs: http://archive.ubuntu.com/ubuntu\n"
            "Suites: noble noble-updates noble-security\nComponents: main universe\n"
            + SIGNING_KEY
        )
        original = source.read_bytes()
        original_mode = source.stat().st_mode
        # Without overrides, custom base distributions need no inspection.
        self.os_release.write_text("ID=custom-base\n")
        self.assert_success(self.run_script())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(source.stat().st_mode, original_mode)

    def test_ubuntu_amd64_deb822_changes_only_official_uris(self):
        content = (
            "Types: deb deb-src\n"
            "URIs: http://archive.ubuntu.com/ubuntu https://us.archive.ubuntu.com/ubuntu\n"
            "Suites: noble noble-updates noble-backports\n"
            "Components: main restricted universe multiverse\n" + SIGNING_KEY + "\n"
            "Types: deb\nURIs: https://security.ubuntu.com/ubuntu\n"
            "Suites: noble-security\nComponents: main restricted universe multiverse\n" + SIGNING_KEY
        )
        source = self.write_source(content)
        mirror = "http://mirror.example.test/ubuntu"
        result = self.run_script(AKERNEL_APT_UBUNTU_MIRROR=mirror,
                                 AKERNEL_APT_UBUNTU_PORTS_MIRROR="http://unused.example.test/ports")
        self.assert_success(result)
        expected = content
        for official in ("http://archive.ubuntu.com/ubuntu", "https://us.archive.ubuntu.com/ubuntu",
                         "https://security.ubuntu.com/ubuntu"):
            expected = expected.replace(official, mirror)
        self.assertEqual(source.read_text(), expected)

    def test_ubuntu_arm64_deb822_selects_ports_mirror(self):
        content = (
            "Types: deb\nURIs: http://ports.ubuntu.com/ubuntu-ports\n"
            "Suites: noble noble-updates noble-backports noble-security\n"
            "Components: main restricted universe multiverse\n" + SIGNING_KEY
        )
        source = self.write_source(content)
        mirror = "http://mirror.example.test/ubuntu-ports/"
        self.assert_success(self.run_script(
            FIXTURE_ARCH="arm64", AKERNEL_APT_UBUNTU_MIRROR="http://unused.example.test/ubuntu",
            AKERNEL_APT_UBUNTU_PORTS_MIRROR=mirror,
        ))
        self.assertEqual(source.read_text(), content.replace("http://ports.ubuntu.com/ubuntu-ports", mirror.rstrip("/")))

    def test_ubuntu_legacy_lists_preserve_options_suites_and_components(self):
        content = (
            "# legacy source layout\n"
            "deb [arch=amd64 signed-by=/usr/share/keyrings/ubuntu.gpg] http://gb.archive.ubuntu.com/ubuntu noble main restricted\n"
            "deb-src https://security.ubuntu.com/ubuntu noble-security universe multiverse\n"
        )
        source = self.root / "etc/apt/sources.list"
        source.write_text(content)
        extra = self.write_source("deb http://archive.ubuntu.com/ubuntu noble-updates main\n", "extra.list")
        mirror = "http://mirror.example.test/ubuntu"
        self.assert_success(self.run_script(AKERNEL_APT_UBUNTU_MIRROR=mirror))
        self.assertEqual(source.read_text(), content.replace("http://gb.archive.ubuntu.com/ubuntu", mirror)
                         .replace("https://security.ubuntu.com/ubuntu", mirror))
        self.assertEqual(extra.read_text(), f"deb {mirror} noble-updates main\n")

    def test_debian_deb822_handles_both_security_hosts_without_archive_prefix_collision(self):
        self.os_release.write_text("ID=debian\nVERSION_CODENAME=bookworm\n")
        content = (
            "Types: deb\nURIs: https://deb.debian.org/debian\n"
            "Suites: bookworm bookworm-updates\nComponents: main contrib non-free-firmware\n" + SIGNING_KEY + "\n"
            "Types: deb\n"
            "URIs: http://deb.debian.org/debian-security https://security.debian.org/debian-security\n"
            "Suites: bookworm-security\nComponents: main contrib non-free-firmware\n" + SIGNING_KEY
        )
        source = self.write_source(content)
        archive = "http://mirror.example.test/debian"
        security = "http://mirror.example.test/debian-security"
        self.assert_success(self.run_script(AKERNEL_APT_DEBIAN_MIRROR=archive,
                                            AKERNEL_APT_DEBIAN_SECURITY_MIRROR=security))
        expected = content.replace("https://deb.debian.org/debian\n", archive + "\n")
        for official in ("http://deb.debian.org/debian-security", "https://security.debian.org/debian-security"):
            expected = expected.replace(official, security)
        self.assertEqual(source.read_text(), expected)

    def test_debian_legacy_security_path_and_lists_keep_signing_options(self):
        self.os_release.write_text("ID=debian\n")
        content = (
            "deb http://deb.debian.org/debian bookworm main\n"
            "deb [signed-by=/usr/share/keyrings/debian.gpg] http://security.debian.org/debian bookworm-security main\n"
        )
        source = self.write_source(content, "debian.list")
        archive = "http://mirror.example.test/debian"
        security = "http://mirror.example.test/debian-security"
        self.assert_success(self.run_script(AKERNEL_APT_DEBIAN_MIRROR=archive,
                                            AKERNEL_APT_DEBIAN_SECURITY_MIRROR=security))
        self.assertEqual(source.read_text(), content.replace("http://deb.debian.org/debian", archive)
                         .replace("http://security.debian.org/debian", security))

    def test_debian_partial_override_keeps_other_official_repository(self):
        self.os_release.write_text("ID=debian\n")
        content = (
            "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: bookworm\nComponents: main\n" + SIGNING_KEY + "\n"
            "Types: deb\nURIs: http://deb.debian.org/debian-security\nSuites: bookworm-security\nComponents: main\n" + SIGNING_KEY
        )
        source = self.write_source(content)
        cases = (
            ("AKERNEL_APT_DEBIAN_MIRROR", "http://deb.debian.org/debian\n", "http://mirror.example.test/archive\n"),
            ("AKERNEL_APT_DEBIAN_SECURITY_MIRROR", "http://deb.debian.org/debian-security", "http://mirror.example.test/security"),
        )
        for variable, official, replacement in cases:
            with self.subTest(variable=variable):
                source.write_text(content)
                self.assert_success(self.run_script(**{variable: replacement.rstrip("\n")}))
                self.assertEqual(source.read_text(), content.replace(official, replacement))

    def test_ubuntu_partial_override_does_not_fall_back_between_architectures(self):
        content = "Types: deb\nURIs: http://ports.ubuntu.com/ubuntu-ports\nSuites: noble\nComponents: main\n" + SIGNING_KEY
        source = self.write_source(content)
        self.assert_success(self.run_script(FIXTURE_ARCH="arm64", AKERNEL_APT_UBUNTU_MIRROR="http://mirror.example.test/ubuntu"))
        self.assertEqual(source.read_text(), content)

    def test_unsupported_distribution_or_architecture_fails_without_source_changes(self):
        source = self.write_source("deb http://archive.ubuntu.com/ubuntu noble main\n", "ubuntu.list")
        original = source.read_bytes()
        for distribution, architecture in (("alpine", "amd64"), ("ubuntu", "riscv64")):
            with self.subTest(distribution=distribution, architecture=architecture):
                self.os_release.write_text(f"ID={distribution}\n")
                result = self.run_script(FIXTURE_ARCH=architecture, AKERNEL_APT_UBUNTU_MIRROR="http://mirror.example.test/ubuntu")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(self.third_party.read_text(), THIRD_PARTY)

    def test_invalid_mirror_url_fails_before_any_source_change_or_value_disclosure(self):
        source = self.write_source("deb http://archive.ubuntu.com/ubuntu noble main\n", "ubuntu.list")
        original = source.read_bytes()
        invalid = (
            "PRIVATE_INVALID_VALUE", "file:///tmp/PRIVATE_INVALID_VALUE",
            "https://user:PRIVATE_INVALID_VALUE@mirror.example.test/ubuntu",
            "https://mirror.example.test/ubuntu?token=PRIVATE_INVALID_VALUE",
            "https://mirror.example.test/ubuntu#PRIVATE_INVALID_VALUE",
            "https://mirror.example.test/PRIVATE_INVALID_VALUE\nINJECTED=yes",
            "http://:443", "https://mirror.example.test:notaport/ubuntu",
            "http://mirror.example.test/PRIVATE_INVALID_VALUE|replacement",
            "http://mirror.example.test/PRIVATE_INVALID_VALUE&replacement",
        )
        for variable in MIRROR_NAMES:
            for value in invalid:
                with self.subTest(variable=variable, value=value):
                    result = self.run_script(**{variable: value})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("APT mirror", result.stdout + result.stderr)
                    self.assertEqual(source.read_bytes(), original)
                    self.assertEqual(self.third_party.read_text(), THIRD_PARTY)


if __name__ == "__main__":
    unittest.main()
