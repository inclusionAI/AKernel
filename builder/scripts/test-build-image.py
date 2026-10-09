#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Check build-platform and release-pin propagation without Docker builds."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PROXY_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)


class BuildImageTests(unittest.TestCase):
    def test_gvisor_download_keeps_http11_and_archive_verification(self):
        dockerfile = (ROOT / "builder/node.Dockerfile").read_text()
        gvisor = dockerfile.split("AS gvisor-runtime", 1)[1].split("\nFROM ", 1)[0]
        self.assertIn("curl -fSL --http1.1 --retry", gvisor)
        self.assertIn('"${GVISOR_URL}" -o "${asset}"', gvisor)
        self.assertIn('"${GVISOR_SHA512}" /gvisor', gvisor)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="akernel-build-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in ("deploy/scripts/build-image.sh", "deploy/scripts/common.sh"):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)
        self.manifest = self.root / "src/sandboxd/third_party/runtime-versions.env"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(
            "GVISOR_RELEASE=test-release\n"
            "GVISOR_AMD64_URL=https://example.test/gvisor-amd64.tar.bz2\n"
            f"GVISOR_AMD64_SHA512={'a' * 128}\n"
            "GVISOR_ARM64_URL=https://example.test/gvisor-arm64.tar.bz2\n"
            f"GVISOR_ARM64_SHA512={'b' * 128}\n"
            "RUNC_VERSION=9.8.7\n"
            "RUNC_RELEASE_BASE_URL=https://example.test/runc\n"
            f"RUNC_AMD64_SHA256={'c' * 64}\n"
            f"RUNC_ARM64_SHA256={'d' * 64}\n"
            "FIRECRACKER_RELEASE=test-firecracker\n"
            "FIRECRACKER_AMD64_URL=https://example.test/firecracker.tgz\n"
            f"FIRECRACKER_AMD64_SHA256={'e' * 64}\n"
        )
        version = self.root / "src/sandboxd/version/VERSION"
        version.parent.mkdir()
        version.write_text("v1.0.0\n")
        distill = self.root / "builder/distill-fs-versions.env"
        distill.parent.mkdir()
        distill.write_text(
            "DISTILL_FS_RELEASE=v9.8.7\n"
            "DISTILL_FS_AMD64_URL=https://example.test/distill-amd64.tar.gz\n"
            f"DISTILL_FS_AMD64_SHA256={'1' * 64}\n"
            "DISTILL_FS_ARM64_URL=https://example.test/distill-arm64.tar.gz\n"
            f"DISTILL_FS_ARM64_SHA256={'2' * 64}\n"
        )
        self.calls = self.root / "docker-calls.jsonl"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['BUILD_TEST_CALLS'], 'a') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        )
        docker.chmod(0o755)
        git = bin_dir / "git"
        git.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *--is-inside-work-tree*) echo true ;;\n"
            "  *rev-parse*) echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;\n"
            "  *describe*) echo v1.0.0 ;;\n"
            "esac\n"
        )
        git.chmod(0o755)
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("AKERNEL_", "RRT_RUNTIME_", "OPEN_YR_CORE_"))
            and key != "RUNTIME_PROFILE"
        }
        self.environment.update(
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            BUILD_TEST_CALLS=str(self.calls),
            AKERNEL_TARGETARCH="arm64",
            AKERNEL_ENABLE_KATA="false",
            AKERNEL_ENABLE_FIRECRACKER="false",
            AKERNEL_ENABLE_RUNC="true",
        )

    def run_helper(self, *arguments, **environment):
        return subprocess.run(
            ["/bin/bash", str(self.root / "deploy/scripts/build-image.sh"),
             "--repository", "test/all-in-one", "--tag", "test", *arguments],
            env={**self.environment, **environment},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def docker_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_arm64_runc_uses_matching_pins_and_platforms(self):
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime, node = self.docker_calls()
        for call in (runtime, node):
            self.assertEqual(call[:3], ["build", "--platform", "linux/arm64"])
        self.assertIn("runtime-rrt", runtime)
        self.assertIn("RUNC_VERSION=9.8.7", node)
        self.assertIn("RUNC_RELEASE_BASE_URL=https://example.test/runc", node)
        self.assertIn(f"RUNC_SHA256={'d' * 64}", node)
        self.assertIn(f"GVISOR_SHA512={'b' * 128}", node)
        self.assertIn("GVISOR_URL=https://example.test/gvisor-arm64.tar.bz2", node)
        self.assertIn(f"DISTILL_FS_SHA256={'2' * 64}", node)
        self.assertIn("DISTILL_FS_URL=https://example.test/distill-arm64.tar.gz", node)
        self.assertIn("AKERNEL_ENABLE_RUNC=true", node)

    def test_amd64_preserves_vm_payloads_and_matching_pins(self):
        result = self.run_helper(
            AKERNEL_TARGETARCH="x86_64", AKERNEL_ENABLE_KATA="true",
            AKERNEL_ENABLE_FIRECRACKER="true",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime, node = self.docker_calls()
        for call in (runtime, node):
            self.assertEqual(call[:3], ["build", "--platform", "linux/amd64"])
        self.assertIn(f"RUNC_SHA256={'c' * 64}", node)
        self.assertIn(f"GVISOR_SHA512={'a' * 128}", node)
        self.assertIn(f"DISTILL_FS_SHA256={'1' * 64}", node)
        self.assertIn("AKERNEL_ENABLE_KATA=true", node)
        self.assertIn("AKERNEL_ENABLE_FIRECRACKER=true", node)

    def test_rrt_override_reaches_runtime_build(self):
        result = self.run_helper(
            "--rrt-runtime-url", "https://example.test/rrt", "--rrt-runtime-sha256", "f" * 64,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime = self.docker_calls()[0]
        self.assertIn("RRT_RUNTIME_URL=https://example.test/rrt", runtime)
        self.assertIn(f"RRT_RUNTIME_SHA256={'f' * 64}", runtime)

    def test_proxy_forwarding_is_off_by_default_and_explicit_false(self):
        proxies = {name: f"http://build-proxy.invalid/{name}" for name in PROXY_NAMES}
        for option in ({}, {"AKERNEL_BUILD_PROXY": "false"}):
            with self.subTest(option=option):
                result = self.run_helper(**proxies, **option)
                self.assertEqual(result.returncode, 0, result.stdout)
                for call in self.docker_calls():
                    for name in PROXY_NAMES:
                        self.assertNotIn(name, call)
                    self.assertNotIn("build-proxy.invalid", json.dumps(call))
                self.assertNotIn("build-proxy.invalid", result.stdout)
        self.assertEqual(len(self.docker_calls()), 4)

    def test_proxy_opt_in_passes_names_only_to_both_builds(self):
        proxies = {name: f"http://build-proxy.invalid/{name}" for name in PROXY_NAMES}
        result = self.run_helper(AKERNEL_BUILD_PROXY="true", **proxies)
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime, node = self.docker_calls()
        for call in (runtime, node):
            names = [call[index + 1] for index, value in enumerate(call[:-1])
                     if value == "--build-arg" and call[index + 1] in PROXY_NAMES]
            self.assertCountEqual(names, PROXY_NAMES)
            self.assertNotIn("build-proxy.invalid", json.dumps(call))
        self.assertNotIn("build-proxy.invalid", result.stdout)

    def test_invalid_proxy_boolean_fails_before_build(self):
        for value in ("", "yes", "1", "TRUE", "PRIVATE_INVALID_VALUE"):
            with self.subTest(value=value):
                result = self.run_helper(AKERNEL_BUILD_PROXY=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("AKERNEL_BUILD_PROXY must be true or false", result.stdout)
                self.assertNotIn("PRIVATE_INVALID_VALUE", result.stdout)
                self.assertEqual(self.docker_calls(), [])

    def test_build_network_uses_default_without_a_cli_override(self):
        for option in ({}, {"AKERNEL_BUILD_NETWORK": "default"}):
            with self.subTest(option=option):
                result = self.run_helper(**option)
                self.assertEqual(result.returncode, 0, result.stdout)
                for call in self.docker_calls():
                    self.assertNotIn("--network", call)
        self.assertEqual(len(self.docker_calls()), 4)

    def test_host_build_network_reaches_both_builds_with_proxy_names_only(self):
        proxies = {name: f"http://build-proxy.invalid/{name}" for name in PROXY_NAMES}
        result = self.run_helper(AKERNEL_BUILD_NETWORK="host", AKERNEL_BUILD_PROXY="true", **proxies)
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime, node = self.docker_calls()
        for call in (runtime, node):
            self.assertEqual(call.count("--network"), 1)
            self.assertEqual(call[call.index("--network") + 1], "host")
            self.assertNotIn("build-proxy.invalid", json.dumps(call))
            for name in PROXY_NAMES:
                self.assertIn(name, call)
        self.assertNotIn("build-proxy.invalid", result.stdout)

    def test_invalid_build_network_fails_before_build(self):
        for value in ("", "none", "bridge", "HOST", "PRIVATE_INVALID_VALUE"):
            with self.subTest(value=value):
                result = self.run_helper(AKERNEL_BUILD_NETWORK=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("AKERNEL_BUILD_NETWORK must be default or host", result.stdout)
                self.assertNotIn("PRIVATE_INVALID_VALUE", result.stdout)
                self.assertEqual(self.docker_calls(), [])

    def test_incomplete_rrt_override_fails_before_build(self):
        result = self.run_helper("--rrt-runtime-url", "https://example.test/rrt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be set together", result.stdout)
        self.assertEqual(self.docker_calls(), [])

    def test_arm64_rejects_unsupported_options_before_build(self):
        cases = (
            ({"AKERNEL_ENABLE_KATA": "true"}, "VM payloads are unsupported"),
            ({"AKERNEL_ENABLE_FIRECRACKER": "true"}, "VM payloads are unsupported"),
            ({"AKERNEL_ENABLE_GPU": "true"}, "NVIDIA GPU payload"),
            ({"RUNTIME_PROFILE": "python"}, "requires the rrt runtime profile"),
        )
        for environment, expected in cases:
            with self.subTest(environment=environment):
                result = self.run_helper(**environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stdout)
                self.assertEqual(self.docker_calls(), [])

    def test_missing_arm64_pin_fails_before_build(self):
        self.manifest.write_text(self.manifest.read_text().replace(
            f"RUNC_ARM64_SHA256={'d' * 64}\n", "",
        ))
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pin the linux/arm64 runc release", result.stdout)
        self.assertEqual(self.docker_calls(), [])

    def test_unsupported_architecture_fails_before_build(self):
        result = self.run_helper(AKERNEL_TARGETARCH="s390x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported image architecture", result.stdout)
        self.assertEqual(self.docker_calls(), [])


if __name__ == "__main__":
    unittest.main()
