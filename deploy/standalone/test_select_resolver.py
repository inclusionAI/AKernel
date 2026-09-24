# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the standalone runc resolver selection."""

import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "select_resolver", Path(__file__).with_name("select-resolver.py")
)
assert SPEC and SPEC.loader
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


class SelectResolverTest(unittest.TestCase):
    def test_preserves_host_resolver_options(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "upstream.conf"
            content = b"nameserver 10.0.0.53\nsearch corp.example\noptions ndots:2\n"
            source.write_bytes(content)
            output = Path(directory) / "generated.conf"
            resolver.write_resolver(output, resolver.select_resolver(source))
            self.assertEqual(output.read_bytes(), content)

    def test_rejects_container_local_resolver(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "docker.conf"
            source.write_text("nameserver 127.0.0.11\n")
            with self.assertRaisesRegex(ValueError, "another network namespace"):
                resolver.select_resolver(source)

    def test_rejects_mapped_loopback_and_scoped_link_local(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.conf"
            for address in (
                "::ffff:127.0.0.11",
                "::ffff:169.254.1.1",
                "fe80::1%eth0",
            ):
                with self.subTest(address=address):
                    source.write_text(f"nameserver {address}\n")
                    with self.assertRaisesRegex(ValueError, "another network namespace"):
                        resolver.select_resolver(source)

    def test_falls_back_from_systemd_stub(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / "stub.conf"
            upstream = Path(directory) / "upstream.conf"
            stub.write_text("nameserver 127.0.0.53\n")
            upstream.write_text("nameserver 100.100.2.136\n")
            self.assertEqual(
                resolver.select_resolver(None, (stub, upstream)), upstream.read_bytes()
            )

    def test_rejects_missing_nameserver(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "empty.conf"
            source.write_text("search corp.example\n")
            with self.assertRaisesRegex(ValueError, "no nameserver"):
                resolver.select_resolver(source)

    def test_explicit_source_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.conf"
            fallback = Path(directory) / "valid.conf"
            source.write_text("nameserver 127.0.0.11\n")
            fallback.write_text("nameserver 10.0.0.53\n")
            with self.assertRaisesRegex(ValueError, "another network namespace"):
                resolver.select_resolver(source, (fallback,))

    def test_rejects_mixed_local_and_upstream_nameservers(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mixed.conf"
            source.write_text("nameserver 10.0.0.53\nnameserver 127.0.0.11\n")
            with self.assertRaisesRegex(ValueError, "another network namespace"):
                resolver.select_resolver(source)


if __name__ == "__main__":
    unittest.main()
