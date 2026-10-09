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
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PROXY_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)
AMBIENT_BUILD_NAMES = (
    *PROXY_NAMES, "BASH_ENV", "ENV", "BASHOPTS", "SHELLOPTS", "CDPATH", "GLOBIGNORE",
    "RUNTIME_PROFILE", "DOCKER_DEFAULT_PLATFORM", "DOCKER_BUILDKIT",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
)
AMBIENT_BUILD_PREFIXES = (
    "AKERNEL_", "RRT_RUNTIME_", "OPEN_YR_CORE_", "GVISOR_", "RUNC_",
    "FIRECRACKER_", "DISTILL_FS_", "BASH_FUNC_",
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
        self.distill = self.root / "builder/distill-fs-versions.env"
        self.distill.parent.mkdir()
        self.distill.write_text(
            "DISTILL_FS_RELEASE=v9.8.7\n"
            "DISTILL_FS_AMD64_URL=https://example.test/distill-amd64.tar.gz\n"
            f"DISTILL_FS_AMD64_SHA256={'1' * 64}\n"
            "DISTILL_FS_ARM64_URL=https://example.test/distill-arm64.tar.gz\n"
            f"DISTILL_FS_ARM64_SHA256={'2' * 64}\n"
        )
        self.calls = self.root / "docker-calls.jsonl"
        self.proxy_environments = self.root / "docker-proxy-environments.jsonl"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['BUILD_TEST_CALLS'], 'a') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"proxy_names = {PROXY_NAMES!r}\n"
            "with open(os.environ['BUILD_TEST_PROXY_ENVIRONMENTS'], 'a') as output:\n"
            "    output.write(json.dumps({name: os.environ.get(name) "
            "for name in proxy_names}) + '\\n')\n"
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
        uname = bin_dir / "uname"
        uname.write_text("#!/bin/sh\necho riscv64\n")
        uname.chmod(0o755)
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(AMBIENT_BUILD_PREFIXES)
            and key not in AMBIENT_BUILD_NAMES
        }
        self.environment.update(
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            BUILD_TEST_CALLS=str(self.calls),
            BUILD_TEST_PROXY_ENVIRONMENTS=str(self.proxy_environments),
            AKERNEL_TARGETARCH="arm64",
            AKERNEL_ENABLE_KATA="false",
            AKERNEL_ENABLE_FIRECRACKER="false",
            AKERNEL_ENABLE_RUNC="true",
        )

    def run_helper(self, *arguments, **environment):
        child_environment = {**self.environment, **environment}
        child_environment = {
            key: value for key, value in child_environment.items() if value is not None
        }
        return subprocess.run(
            ["/bin/bash", str(self.root / "deploy/scripts/build-image.sh"),
             "--repository", "test/all-in-one", "--tag", "test", *arguments],
            env=child_environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def docker_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def docker_proxy_environments(self):
        if not self.proxy_environments.exists():
            return []
        return [json.loads(line) for line in self.proxy_environments.read_text().splitlines()]

    def write_profile(self, **values):
        profile = self.root / ".akernel/test/config.env"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(
            "IMAGE_REPOSITORY=profile/all-in-one\nIMAGE_TAG=profile\n"
            + "".join(f"{key}={value}\n" for key, value in values.items())
        )

    def test_fixture_ignores_ambient_shell_and_build_configuration(self):
        startup = self.root / "ambient-startup.sh"
        startup.write_text("exit 47\n")
        leaked = {
            "BASH_ENV": str(startup), "ENV": str(startup),
            "DOCKER_DEFAULT_PLATFORM": "PRIVATE_INVALID_VALUE", "DOCKER_BUILDKIT": "0",
            "AKERNEL_TARGETARCH": "PRIVATE_INVALID_VALUE", "RUNTIME_PROFILE": "python",
            "RRT_RUNTIME_URL": "https://private-override.invalid/rrt",
            "OPEN_YR_CORE_WHEEL_URL": "https://private-override.invalid/core",
            "GVISOR_UNSELECTED_PIN": "PRIVATE_INVALID_VALUE",
            "RUNC_UNSELECTED_PIN": "PRIVATE_INVALID_VALUE",
            "FIRECRACKER_UNSELECTED_PIN": "PRIVATE_INVALID_VALUE",
            "DISTILL_FS_UNSELECTED_PIN": "PRIVATE_INVALID_VALUE",
            "HTTP_PROXY": "https://private-proxy.invalid",
        }
        fixture = BuildImageTests("test_arm64_runc_uses_matching_pins_and_platforms")
        self.addCleanup(fixture.doCleanups)
        with mock.patch.dict(os.environ, leaked):
            fixture.setUp()
        for name in leaked:
            if name != "AKERNEL_TARGETARCH":
                self.assertNotIn(name, fixture.environment)
        result = fixture.run_helper()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(fixture.docker_calls()), 2)
        self.assertNotIn("private-override.invalid", result.stdout)
        self.assertNotIn("private-proxy.invalid", result.stdout)

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

    def test_target_precedence_and_stable_amd64_default(self):
        cases = (
            ({"AKERNEL_TARGETARCH": None}, "amd64"),
            ({"AKERNEL_TARGETARCH": None, "DOCKER_DEFAULT_PLATFORM": ""}, "amd64"),
            ({"AKERNEL_TARGETARCH": None, "DOCKER_DEFAULT_PLATFORM": "linux/amd64"}, "amd64"),
            ({"AKERNEL_TARGETARCH": None, "DOCKER_DEFAULT_PLATFORM": "linux/arm64"}, "arm64"),
            ({"AKERNEL_TARGETARCH": "amd64", "DOCKER_DEFAULT_PLATFORM": "linux/arm64"}, "amd64"),
            ({"AKERNEL_TARGETARCH": "aarch64", "DOCKER_DEFAULT_PLATFORM": "linux/amd64"}, "arm64"),
            ({"DOCKER_DEFAULT_PLATFORM": "PRIVATE_INVALID_VALUE"}, "arm64"),
        )
        for environment, architecture in cases:
            with self.subTest(environment=environment):
                result = self.run_helper(**environment)
                self.assertEqual(result.returncode, 0, result.stdout)
                for call in self.docker_calls()[-2:]:
                    self.assertEqual(call[:3], ["build", "--platform", f"linux/{architecture}"])
        self.assertEqual(len(self.docker_calls()), 2 * len(cases))

    def test_saved_profile_does_not_change_the_caller_platform(self):
        self.write_profile(AKERNEL_TARGETARCH="s390x", DOCKER_DEFAULT_PLATFORM="PRIVATE_INVALID_VALUE")
        result = self.run_helper("--env", "test")
        self.assertEqual(result.returncode, 0, result.stdout)
        for call in self.docker_calls():
            self.assertEqual(call[:3], ["build", "--platform", "linux/arm64"])

    def test_invalid_docker_default_platform_fails_before_build(self):
        for platform in ("linux/arm64/v8", "linux/amd64,linux/arm64", "windows/amd64",
                         "amd64", "PRIVATE_INVALID_VALUE"):
            with self.subTest(platform=platform):
                result = self.run_helper(AKERNEL_TARGETARCH=None, DOCKER_DEFAULT_PLATFORM=platform)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("DOCKER_DEFAULT_PLATFORM must be linux/amd64 or linux/arm64", result.stdout)
                self.assertNotIn("PRIVATE_INVALID_VALUE", result.stdout)
                self.assertEqual(self.docker_calls(), [])

    def test_disabled_buildkit_fails_before_build(self):
        for architecture in ("amd64", "arm64"):
            with self.subTest(architecture=architecture):
                result = self.run_helper(AKERNEL_TARGETARCH=architecture, DOCKER_BUILDKIT="0")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("image builds require BuildKit", result.stdout)
                self.assertEqual(self.docker_calls(), [])

    def test_explicit_buildkit_is_supported(self):
        result = self.run_helper(DOCKER_BUILDKIT="1")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.docker_calls()), 2)

    def test_caller_payload_flags_override_profile(self):
        for profile_values in (
            {"AKERNEL_ENABLE_RUNC": "false", "AKERNEL_ENABLE_KATA": "true",
             "AKERNEL_ENABLE_FIRECRACKER": "true"},
            {name: "PRIVATE_INVALID_VALUE" for name in (
                "AKERNEL_ENABLE_RUNC", "AKERNEL_ENABLE_KATA", "AKERNEL_ENABLE_FIRECRACKER",
            )},
        ):
            with self.subTest(profile_values=profile_values):
                self.write_profile(**profile_values)
                result = self.run_helper("--env", "test")
                self.assertEqual(result.returncode, 0, result.stdout)
                node = self.docker_calls()[-1]
                self.assertIn("AKERNEL_ENABLE_RUNC=true", node)
                self.assertIn("AKERNEL_ENABLE_KATA=false", node)
                self.assertIn("AKERNEL_ENABLE_FIRECRACKER=false", node)

    def test_profile_payload_flags_apply_when_caller_does_not_set_them(self):
        self.write_profile(AKERNEL_ENABLE_RUNC="true", AKERNEL_ENABLE_KATA="false",
                           AKERNEL_ENABLE_FIRECRACKER="false")
        result = self.run_helper(
            "--env", "test", AKERNEL_ENABLE_RUNC=None,
            AKERNEL_ENABLE_KATA=None, AKERNEL_ENABLE_FIRECRACKER=None,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        node = self.docker_calls()[1]
        self.assertIn("AKERNEL_ENABLE_RUNC=true", node)
        self.assertIn("AKERNEL_ENABLE_KATA=false", node)
        self.assertIn("AKERNEL_ENABLE_FIRECRACKER=false", node)

    def test_unset_payload_flags_use_amd64_defaults(self):
        result = self.run_helper(
            AKERNEL_TARGETARCH="amd64", AKERNEL_ENABLE_RUNC=None,
            AKERNEL_ENABLE_KATA=None, AKERNEL_ENABLE_FIRECRACKER=None,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        node = self.docker_calls()[1]
        self.assertIn("AKERNEL_ENABLE_RUNC=false", node)
        self.assertIn("AKERNEL_ENABLE_KATA=true", node)
        self.assertIn("AKERNEL_ENABLE_FIRECRACKER=true", node)

    def test_invalid_payload_flags_fail_after_configuration_merge(self):
        flags = ("AKERNEL_ENABLE_RUNC", "AKERNEL_ENABLE_KATA", "AKERNEL_ENABLE_FIRECRACKER")
        for source in ("caller", "profile"):
            for flag in flags:
                for value in ("", "yes", "PRIVATE_INVALID_VALUE"):
                    with self.subTest(source=source, flag=flag, value=value):
                        self.write_profile(**{name: "false" for name in flags})
                        environment = {"AKERNEL_TARGETARCH": "amd64"}
                        if source == "caller":
                            environment[flag] = value
                        else:
                            self.write_profile(**{name: value if name == flag else "false"
                                                  for name in flags})
                            environment.update({name: None for name in flags})
                        result = self.run_helper("--env", "test", **environment)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(f"{flag} must be true or false", result.stdout)
                        self.assertNotIn("PRIVATE_INVALID_VALUE", result.stdout)
                        self.assertEqual(self.docker_calls(), [])

    def test_rrt_override_reaches_runtime_build(self):
        result = self.run_helper(
            "--rrt-runtime-url", "https://example.test/rrt", "--rrt-runtime-sha256", "f" * 64,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime = self.docker_calls()[0]
        self.assertIn("RRT_RUNTIME_URL=https://example.test/rrt", runtime)
        self.assertIn(f"RRT_RUNTIME_SHA256={'f' * 64}", runtime)

    def test_core_override_and_cli_precedence_reach_builds(self):
        result = self.run_helper(
            "--rrt-runtime-url", "https://example.test/cli-rrt",
            "--rrt-runtime-sha256", "A" * 64,
            "--open-yr-core-wheel-url", "https://example.test/cli-core.whl",
            "--open-yr-core-wheel-sha256", "F" * 64,
            RRT_RUNTIME_URL="https://example.test/env-rrt", RRT_RUNTIME_SHA256="b" * 64,
            OPEN_YR_CORE_WHEEL_URL="https://example.test/env-core.whl",
            OPEN_YR_CORE_WHEEL_SHA256="c" * 64,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        runtime, node = self.docker_calls()
        self.assertIn("RRT_RUNTIME_URL=https://example.test/cli-rrt", runtime)
        self.assertIn(f"RRT_RUNTIME_SHA256={'A' * 64}", runtime)
        self.assertIn("OPEN_YR_CORE_WHEEL_URL=https://example.test/cli-core.whl", node)
        self.assertIn(f"OPEN_YR_CORE_WHEEL_SHA256={'F' * 64}", node)

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
        self.assertEqual(self.docker_proxy_environments(), [proxies, proxies])
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

    def test_invalid_artifact_overrides_fail_before_either_build(self):
        private_url = "https://private-override.invalid/payload"
        cases = (
            (private_url, None, "must be set together"),
            (None, "f" * 64, "must be set together"),
            ("", "f" * 64, "must be set together"),
            (private_url, "", "must be set together"),
            (private_url, "PRIVATE_INVALID_VALUE", "64-character hexadecimal digest"),
            (private_url, "f" * 63, "64-character hexadecimal digest"),
            (private_url, "f" * 65, "64-character hexadecimal digest"),
            (private_url, "z" * 64, "64-character hexadecimal digest"),
        )
        for prefix, cli_prefix in (
            ("RRT_RUNTIME", "rrt-runtime"), ("OPEN_YR_CORE_WHEEL", "open-yr-core-wheel"),
        ):
            for source in ("caller", "cli"):
                for url, sha256, expected in cases:
                    with self.subTest(prefix=prefix, source=source, url=url, sha256=sha256):
                        values = {"url": url, "sha256": sha256}
                        environment, arguments = {}, []
                        for key, value in values.items():
                            if value is None:
                                continue
                            if source == "caller":
                                environment[f"{prefix}_{key.upper()}"] = value
                            else:
                                arguments.extend((f"--{cli_prefix}-{key}", value))
                        result = self.run_helper(*arguments, **environment)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(expected, result.stdout)
                        self.assertNotIn("private-override.invalid", result.stdout)
                        self.assertNotIn("PRIVATE_INVALID_VALUE", result.stdout)
                        self.assertEqual(self.docker_calls(), [])

    def test_missing_cli_values_fail_before_build(self):
        for option in (
            "--env", "--repository", "--tag", "--runtime-image", "--runtime-profile",
            "--open-yr-core-wheel-url", "--open-yr-core-wheel-sha256",
            "--rrt-runtime-url", "--rrt-runtime-sha256",
        ):
            with self.subTest(option=option):
                result = self.run_helper(option)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{option} requires a value", result.stdout)
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

    def test_missing_and_invalid_release_pins_fail_before_build_on_both_architectures(self):
        originals = {self.manifest: self.manifest.read_text(), self.distill: self.distill.read_text()}
        for architecture in ("amd64", "arm64"):
            suffix = architecture.upper()
            pins = (
                (self.manifest, "GVISOR_RELEASE", "gVisor", 0),
                (self.manifest, f"GVISOR_{suffix}_URL", "gVisor", 0),
                (self.manifest, f"GVISOR_{suffix}_SHA512", "gVisor", 128),
                (self.manifest, "RUNC_VERSION", "runc", 0),
                (self.manifest, "RUNC_RELEASE_BASE_URL", "runc", 0),
                (self.manifest, f"RUNC_{suffix}_SHA256", "runc", 64),
                (self.distill, "DISTILL_FS_RELEASE", "distill-fs", 0),
                (self.distill, f"DISTILL_FS_{suffix}_URL", "distill-fs", 0),
                (self.distill, f"DISTILL_FS_{suffix}_SHA256", "distill-fs", 64),
            )
            for path, key, component, digest_length in pins:
                values = (None, "")
                if digest_length:
                    values += ("PRIVATE_INVALID_VALUE", "f" * (digest_length - 1),
                               "g" * digest_length)
                for value in values:
                    with self.subTest(architecture=architecture, pin=key, value=value):
                        for original_path, content in originals.items():
                            original_path.write_text(content)
                        lines = [line for line in originals[path].splitlines()
                                 if not line.startswith(f"{key}=")]
                        if value is not None:
                            lines.append(f"{key}={value}")
                        path.write_text("\n".join(lines) + "\n")
                        result = self.run_helper(AKERNEL_TARGETARCH=architecture)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(f"pin the linux/{architecture} {component} release", result.stdout)
                        self.assertNotIn("PRIVATE_INVALID_VALUE", result.stdout)
                        self.assertEqual(self.docker_calls(), [])

    def test_missing_and_invalid_firecracker_pins_fail_when_enabled(self):
        original = self.manifest.read_text()
        for key in ("FIRECRACKER_RELEASE", "FIRECRACKER_AMD64_URL", "FIRECRACKER_AMD64_SHA256"):
            values = (None, "")
            if key.endswith("SHA256"):
                values += ("PRIVATE_INVALID_VALUE", "f" * 63, "g" * 64)
            for value in values:
                with self.subTest(pin=key, value=value):
                    lines = [line for line in original.splitlines() if not line.startswith(f"{key}=")]
                    if value is not None:
                        lines.append(f"{key}={value}")
                    self.manifest.write_text("\n".join(lines) + "\n")
                    result = self.run_helper(AKERNEL_TARGETARCH="amd64", AKERNEL_ENABLE_FIRECRACKER="true")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("FIRECRACKER_RELEASE, FIRECRACKER_AMD64_URL", result.stdout)
                    self.assertNotIn("PRIVATE_INVALID_VALUE", result.stdout)
                    self.assertEqual(self.docker_calls(), [])

    def test_disabled_optional_payloads_do_not_require_their_pins(self):
        self.manifest.write_text("\n".join(
            line for line in self.manifest.read_text().splitlines()
            if not line.startswith(("RUNC_", "FIRECRACKER_"))
        ) + "\n")
        result = self.run_helper(AKERNEL_TARGETARCH="amd64", AKERNEL_ENABLE_RUNC="false",
                                 AKERNEL_ENABLE_FIRECRACKER="false")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.docker_calls()), 2)

    def test_missing_manifest_pins_cannot_be_filled_by_environment_or_profile(self):
        originals = {self.manifest: self.manifest.read_text(), self.distill: self.distill.read_text()}
        cases = (
            (self.manifest, "GVISOR_ARM64_SHA512", "b" * 128, "arm64", "gVisor"),
            (self.manifest, "RUNC_ARM64_SHA256", "d" * 64, "arm64", "runc"),
            (self.distill, "DISTILL_FS_ARM64_SHA256", "2" * 64, "arm64", "distill-fs"),
            (self.manifest, "FIRECRACKER_AMD64_SHA256", "e" * 64, "amd64", "FIRECRACKER_RELEASE"),
        )
        for source in ("caller", "profile"):
            for path, key, value, architecture, component in cases:
                with self.subTest(source=source, pin=key):
                    self.calls.unlink(missing_ok=True)
                    for original_path, content in originals.items():
                        original_path.write_text(content)
                    path.write_text("\n".join(
                        line for line in originals[path].splitlines() if not line.startswith(f"{key}=")
                    ) + "\n")
                    environment = {"AKERNEL_TARGETARCH": architecture}
                    if architecture == "amd64":
                        environment["AKERNEL_ENABLE_FIRECRACKER"] = "true"
                    arguments = []
                    if source == "caller":
                        environment[key] = value
                    else:
                        self.write_profile(**{key: value})
                        arguments = ["--env", "test"]
                    result = self.run_helper(*arguments, **environment)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(component, result.stdout)
                    self.assertEqual(self.docker_calls(), [])

    def test_profile_cannot_override_manifest_pins(self):
        keys = (
            "GVISOR_RELEASE", "GVISOR_ARM64_URL", "GVISOR_ARM64_SHA512",
            "RUNC_VERSION", "RUNC_RELEASE_BASE_URL", "RUNC_ARM64_SHA256",
            "DISTILL_FS_RELEASE", "DISTILL_FS_ARM64_URL", "DISTILL_FS_ARM64_SHA256",
            "FIRECRACKER_RELEASE", "FIRECRACKER_AMD64_URL", "FIRECRACKER_AMD64_SHA256",
        )
        self.write_profile(**{key: "PRIVATE_INVALID_VALUE" for key in keys})
        result = self.run_helper("--env", "test")
        self.assertEqual(result.returncode, 0, result.stdout)
        node = self.docker_calls()[1]
        self.assertIn("GVISOR_RELEASE=test-release", node)
        self.assertIn(f"GVISOR_SHA512={'b' * 128}", node)
        self.assertIn("RUNC_VERSION=9.8.7", node)
        self.assertIn(f"RUNC_SHA256={'d' * 64}", node)
        self.assertIn("DISTILL_FS_RELEASE=v9.8.7", node)
        self.assertIn(f"DISTILL_FS_SHA256={'2' * 64}", node)
        self.assertNotIn("PRIVATE_INVALID_VALUE", json.dumps(node))

    def test_builder_distill_manifest_cannot_inherit_sandboxd_test_pins(self):
        self.manifest.write_text(
            self.manifest.read_text() + f"DISTILL_FS_ARM64_SHA256={'2' * 64}\n"
        )
        self.distill.write_text("\n".join(
            line for line in self.distill.read_text().splitlines()
            if not line.startswith("DISTILL_FS_ARM64_SHA256=")
        ) + "\n")
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pin the linux/arm64 distill-fs release", result.stdout)
        self.assertEqual(self.docker_calls(), [])

    def test_unsupported_architecture_fails_before_build(self):
        for architecture in ("", "s390x"):
            with self.subTest(architecture=architecture):
                result = self.run_helper(AKERNEL_TARGETARCH=architecture)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsupported image architecture", result.stdout)
                self.assertEqual(self.docker_calls(), [])


if __name__ == "__main__":
    unittest.main()
