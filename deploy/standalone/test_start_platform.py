# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Check OrbStack launch gates and cleanup without running containers."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


STANDALONE_DIR = Path(__file__).resolve().parent


def function_from(path, name):
    match = re.search(
        rf"^{name}\(\) \{{\n.*?^\}}", path.read_text(), re.MULTILINE | re.DOTALL
    )
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group()


class StartPlatformTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.calls = self.directory / "docker-run"
        self.environment = dict(os.environ)
        for name in (
            "BASH_ENV",
            "AKERNEL_ENABLE_KATA",
            "AKERNEL_ENABLE_FIRECRACKER",
            "AKERNEL_ENABLE_GPU",
        ):
            self.environment.pop(name, None)
        self.environment.update(
            SCRIPT_DIR=str(STANDALONE_DIR),
            CONFIG_DIR=str(STANDALONE_DIR / "config"),
            DATA_DIR=str(self.directory / "data"),
            IMAGE="akernel-test:arm64",
            AKERNEL_ENABLE_RUNC="true",
            AKERNEL_NAT_BACKEND="iptables",
            MOCK_HOST="Darwin",
            MOCK_OS="OrbStack",
            MOCK_ENGINE="linux/aarch64",
            MOCK_IMAGE="linux/arm64",
            MOCK_KATA="false",
            MOCK_FIRECRACKER="false",
            MOCK_RUNC="true",
            MOCK_CALLS=str(self.calls),
        )

    def run_function(self, name, **overrides):
        environment = self.environment | overrides
        stubs = r'''
log_info() { :; }
log_warn() { printf '%s\n' "$1" >&2; }
log_error() { printf '%s\n' "$1" >&2; }
uname() { printf '%s\n' "$MOCK_HOST"; }
id() { printf '0\n'; }
modprobe() { printf 'linux-modprobe\n' >&2; return 42; }
docker() {
    case "$1" in
        info)
            case "$3" in
                '{{.OperatingSystem}}') printf '%s\n' "$MOCK_OS" ;;
                '{{.OSType}}/{{.Architecture}}') printf '%s\n' "$MOCK_ENGINE" ;;
            esac
            ;;
        image)
            case "$4" in
                '{{.Os}}/{{.Architecture}}') printf '%s\n' "$MOCK_IMAGE" ;;
                *org.akernel.kata.enabled*) printf '%s\n' "$MOCK_KATA" ;;
                *org.akernel.firecracker.enabled*) printf '%s\n' "$MOCK_FIRECRACKER" ;;
                *org.akernel.runc.enabled*) printf '%s\n' "$MOCK_RUNC" ;;
                *) return 2 ;;
            esac
            ;;
        run) printf '%s\n' "$@" > "$MOCK_CALLS" ;;
        *) return 2 ;;
    esac
}
DOCKER_CMD=docker
DOCKER_PREFIX=()
'''
        return subprocess.run(
            [
                "bash",
                "-e",
                "-c",
                stubs
                + function_from(STANDALONE_DIR / "start.sh", name)
                + f"\n{name}\n",
            ],
            cwd=self.directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_native_runc_uses_only_disposable_capability_probe(self):
        result = self.run_function("prepare_host_network_modules")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.calls.read_text().splitlines()
        self.assertEqual(
            arguments[:5], ["run", "--rm", "--privileged", "--net", "bridge"]
        )
        self.assertIn("AKERNEL_ENABLE_RUNC=true", arguments)
        self.assertIn(f"{self.directory}/data:/home/akernel", arguments)
        self.assertEqual(
            arguments[-3:],
            ["/bin/bash", "akernel-test:arm64", "/orbstack-preflight.sh"],
        )

    def test_runsc_profile_does_not_require_runc_payload(self):
        result = self.run_function(
            "prepare_host_network_modules",
            AKERNEL_ENABLE_RUNC="false",
            MOCK_RUNC="false",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AKERNEL_ENABLE_RUNC=false", self.calls.read_text().splitlines())

    def test_unsupported_engines_payloads_and_options_fail_before_probe(self):
        cases = (
            ({"MOCK_OS": "Docker Desktop"}, "requires OrbStack"),
            ({"MOCK_ENGINE": "linux/x86_64"}, "native linux/arm64 Docker engine"),
            ({"MOCK_IMAGE": "linux/amd64"}, "native linux/arm64 AKernel image"),
            ({"MOCK_IMAGE": "windows/arm64"}, "native linux/arm64 AKernel image"),
            ({"MOCK_KATA": "true"}, "image must disable kata"),
            ({"MOCK_FIRECRACKER": "<no value>"}, "image must disable firecracker"),
            ({"MOCK_RUNC": "false"}, "runc payload enabled"),
            ({"MOCK_RUNC": "<no value>"}, "runc payload enabled"),
            ({"AKERNEL_ENABLE_GPU": "true"}, "GPU disabled"),
            ({"AKERNEL_ENABLE_KATA": "true"}, "Kata, Firecracker"),
            ({"AKERNEL_ENABLE_FIRECRACKER": "true"}, "Kata, Firecracker"),
            ({"AKERNEL_NAT_BACKEND": "bpfnat"}, "iptables networking"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                result = self.run_function(
                    "prepare_host_network_modules", **overrides
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.calls.exists())

    def test_linux_still_uses_host_module_loading(self):
        result = self.run_function("prepare_host_network_modules", MOCK_HOST="Linux")
        self.assertEqual(result.returncode, 42)
        self.assertIn("linux-modprobe", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_absolute_profile_path_is_accepted_and_relative_path_rejected(self):
        result = self.run_function("check_prerequisites", DATA_DIR="relative-data")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be an absolute path", result.stderr)
        self.assertFalse((self.directory / "relative-data").exists())
        result = self.run_function("check_prerequisites")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.directory / "data/sandboxd/config").is_dir())

    def test_existing_node_or_gateway_is_rejected_without_cleanup(self):
        function = function_from(STANDALONE_DIR / "start.sh", "cleanup_existing")
        for container in ("akernel-node", "akernel-traefik"):
            with self.subTest(container=container):
                result = subprocess.run(
                    [
                        "bash", "-e", "-c",
                        'docker() { [[ "$1 $2" == "container inspect" '
                        '&& "$3" == "$MOCK_COLLISION" ]]; }; '
                        'log_warn() { printf "%s\\n" "$1" >&2; }; '
                        'DOCKER_CMD=docker; DOCKER_PREFIX=(); '
                        'NODE_CONTAINER_NAME=akernel-node; '
                        'TRAEFIK_CONTAINER_NAME=akernel-traefik;\n'
                        + function + "\ncleanup_existing\n",
                    ],
                    env=self.environment | {"MOCK_COLLISION": container},
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Existing container '{container}'", result.stderr)


class PreflightCleanupTest(unittest.TestCase):
    def run_cleanup(self, *, failure=False, status=0):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.probe = directory / "probe"
        self.probe.mkdir()
        for name in ("ext4", "lower", "overlay"):
            (self.probe / name).mkdir()
        (self.probe / "filestore.img").write_text("owned image")
        self.sentinel = directory / "unrelated"
        self.sentinel.write_text("preserved")
        self.cgroup = directory / "probe-cgroup"
        self.cgroup.mkdir()
        environment = dict(os.environ)
        environment.pop("BASH_ENV", None)
        environment.update(
            MOCK_PROBE=str(self.probe), MOCK_CGROUP=str(self.cgroup),
            MOCK_FAILURE="true" if failure else "false", MOCK_STATUS=str(status),
        )
        script = r'''
probe_dir="$MOCK_PROBE"
cgroup_probe="$MOCK_CGROUP"
ext4_mounted=true
lower_mounted=true
overlay_mounted=true
tap_created=false
veth_created=false
ipset_created=false
ipv4_chain_created=false
ipv6_chain_created=false
umount() { [[ "$MOCK_FAILURE" != true || "$1" != "$probe_dir/overlay" ]]; }
'''
        return subprocess.run(
            ["bash", "-e", "-c", script
             + function_from(STANDALONE_DIR / "orbstack-preflight.sh", "cleanup")
             + '\ntrap cleanup EXIT\nexit "$MOCK_STATUS"\n'],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_cleanup_removes_only_owned_paths_and_preserves_original_failure(self):
        result = self.run_cleanup(status=43)
        self.assertEqual(result.returncode, 43, result.stderr)
        self.assertFalse(self.probe.exists())
        self.assertFalse(self.cgroup.exists())
        self.assertEqual(self.sentinel.read_text(), "preserved")

    def test_unmount_failure_is_reported_and_keeps_backing_image(self):
        result = self.run_cleanup(failure=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("preflight cleanup failed", result.stderr)
        self.assertTrue((self.probe / "filestore.img").exists())
        self.assertEqual(self.sentinel.read_text(), "preserved")


if __name__ == "__main__":
    unittest.main()
