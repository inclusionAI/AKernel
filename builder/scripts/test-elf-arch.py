#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Test ELF architecture validation without Docker, Linux, or readelf."""

import hashlib
import io
from pathlib import Path
import struct
import subprocess
import tarfile
import tempfile
import unittest


VALIDATOR = Path(__file__).with_name("verify-elf-arch.sh")


def elf_header(*, machine=62, elf_class=2, data=1, ident_version=1,
               header_version=1, elf_type=2, header_size=64):
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4:7] = bytes((elf_class, data, ident_version))
    byte_order = ">" if data == 2 else "<"
    struct.pack_into(byte_order + "HHI", header, 16, elf_type, machine, header_version)
    struct.pack_into(byte_order + "H", header, 52, header_size)
    return bytes(header)


class ELFArchitectureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="akernel-elf-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def validate(self, payload, architecture="amd64"):
        binary = self.directory / "binary with spaces"
        binary.write_bytes(payload)
        return subprocess.run(
            ["sh", str(VALIDATOR), str(binary), architecture],
            capture_output=True, text=True, timeout=10,
        )

    def test_supported_architectures_and_executable_types(self):
        for architecture, machine in (
            ("amd64", 62), ("x86_64", 62),
            ("arm64", 183), ("aarch64", 183),
        ):
            for elf_type in (2, 3):
                with self.subTest(architecture=architecture, elf_type=elf_type):
                    result = self.validate(
                        elf_header(machine=machine, elf_type=elf_type), architecture,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_rejects_machine_mismatch(self):
        for architecture, machine in (("arm64", 62), ("amd64", 183)):
            with self.subTest(architecture=architecture):
                result = self.validate(elf_header(machine=machine), architecture)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ELF machine mismatch", result.stderr)

    def test_rejects_invalid_headers(self):
        cases = (
            (b"", "not an ELF binary"),
            (b"not an ELF binary" + bytes(64), "not an ELF binary"),
            (b"\x7fELF", "truncated ELF header"),
            (elf_header()[:63], "truncated ELF header"),
            (elf_header(elf_class=1), "requires ELF64"),
            (elf_header(data=2), "requires little-endian ELF"),
            (elf_header(ident_version=0), "ELF identification version"),
            (elf_header(header_version=0), "ELF header version"),
            (elf_header(header_size=52), "ELF64 header size"),
            (elf_header(elf_type=1), "ET_EXEC or ET_DYN"),
            (elf_header(machine=0), "ELF machine mismatch"),
        )
        for payload, error in cases:
            with self.subTest(error=error):
                result = self.validate(payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)

    def test_accepts_header_followed_by_program_data(self):
        result = self.validate(elf_header() + bytes(1024))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_accepts_leading_dash_filenames(self):
        for filename in ("-binary", "-"):
            with self.subTest(filename=filename):
                (self.directory / filename).write_bytes(elf_header())
                result = subprocess.run(
                    ["sh", str(VALIDATOR.resolve()), filename, "amd64"],
                    cwd=self.directory, input="not an ELF on process stdin",
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_rejects_unsupported_architecture(self):
        result = self.validate(elf_header(), "s390x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported target architecture", result.stderr)

    def test_rejects_missing_file_or_directory(self):
        for binary in (self.directory / "missing", self.directory):
            with self.subTest(binary=binary):
                result = subprocess.run(
                    ["sh", str(VALIDATOR), str(binary), "amd64"],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not a regular file", result.stderr)

    def test_requires_file_and_architecture_arguments(self):
        result = subprocess.run(
            ["sh", str(VALIDATOR)], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)


class AscendAdapterInstallTest(unittest.TestCase):
    def test_checksum_and_elf_gate_installation(self):
        installer = Path(__file__).with_name("install-ascend-adapter.sh")
        bundle = "ascend-oci-adapter_v0.1.0-rc.2_linux_amd64"
        binary_path = Path("usr/local/libexec/akernel/ascend-oci-adapter")
        license_paths = (
            "licenses/ascend-oci-adapter/LICENSE",
            "licenses/mind-cluster/LICENSE",
            "licenses/mind-cluster/Third_Party_Open_Source_Software_Notice.md",
        )
        for machine, valid_checksum, error in (
            (62, True, None),
            (183, True, "ELF machine mismatch"),
            (62, False, "checksum"),
        ):
            with self.subTest(machine=machine, valid_checksum=valid_checksum):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    archive, destination = root / "release.tar.gz", root / "rootfs"
                    profile = root / "mounts.json"
                    profile.write_text("{}")
                    binary = elf_header(machine=machine)
                    with tarfile.open(archive, "w:gz") as output:
                        files = {"bin/ascend-oci-adapter": binary}
                        files.update({path: b"license" for path in license_paths})
                        for path, content in files.items():
                            member = tarfile.TarInfo(f"{bundle}/{path}")
                            member.size = len(content)
                            output.addfile(member, io.BytesIO(content))
                    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
                    result = subprocess.run(
                        ["sh", str(installer), archive.as_uri(),
                         checksum if valid_checksum else "0" * 64,
                         str(profile), str(destination)],
                        capture_output=True, text=True, timeout=10,
                    )
                    if error:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(error, result.stderr)
                        self.assertFalse(destination.exists())
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual((destination / binary_path).read_bytes(), binary)
                        self.assertEqual((destination / binary_path).stat().st_mode & 0o777, 0o755)
                        self.assertEqual(
                            (destination / "etc/akernel/ascend/mounts.json").read_text(),
                            profile.read_text(),
                        )
                        loader_config = destination / "etc/ld.so.conf.d/akernel-ascend.conf"
                        self.assertEqual(
                            loader_config.read_text().splitlines(),
                            [
                                "/usr/local/Ascend/driver/lib64/common",
                                "/usr/local/Ascend/driver/lib64/driver",
                                "/usr/local/dcmi",
                            ],
                        )
                        self.assertEqual(loader_config.stat().st_mode & 0o777, 0o644)
                        for license_path in license_paths:
                            self.assertTrue((destination / "opt/akernel" / license_path).is_file())


if __name__ == "__main__":
    unittest.main()
