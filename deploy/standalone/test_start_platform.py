# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Check launch order, platform gates and cleanup without real containers."""

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


def isolated_environment():
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith("AKERNEL_") or name in (
            "BASH_ENV", "ENV", "DOCKER_DEFAULT_PLATFORM"
        ):
            environment.pop(name)
    return environment


class StartPlatformTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.calls = self.directory / "calls"
        self.environment = isolated_environment()
        self.environment.update(
            SCRIPT_DIR=str(STANDALONE_DIR),
            CONFIG_DIR=str(STANDALONE_DIR / "config"),
            DATA_DIR=str(self.directory / "data"),
            IMAGE="akernel-test:arm64",
            TRAEFIK_IMAGE="traefik-test:arm64",
            TOKEN_FILE=str(self.directory / "data/token"),
            IAM_SEED_FILE=str(self.directory / "data/iam-seed"),
            SANDBOXD_CONFIG_FILE=str(self.directory / "data/sandboxd/config.toml"),
            NODE_CONTAINER_NAME="akernel-node",
            TRAEFIK_CONTAINER_NAME="akernel-traefik",
            ETCD_PORT="2379", ETCD_PEER_PORT="2378",
            YR_IMAGE_PROCESS_CONFIG="/run/akernel/yr-image-process.json",
            AKERNEL_ENABLE_RUNC="true", AKERNEL_NAT_BACKEND="iptables",
            AKERNEL_FIRECRACKER_BACKEND="kvm",
            MOCK_HOST="Darwin", MOCK_OS="OrbStack",
            MOCK_ENGINE="linux/aarch64", MOCK_IMAGE="linux/arm64",
            MOCK_TRAEFIK_IMAGE="linux/arm64",
            MOCK_KATA="false", MOCK_FIRECRACKER="false", MOCK_RUNC="true",
            MOCK_LOCAL_IMAGE="true", MOCK_PROBE_STATUS="0",
            MOCK_CALLS=str(self.calls),
        )

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [line.split("|") for line in self.calls.read_text().splitlines()]

    def run_script(self, commands, **overrides):
        stubs = r'''
record() { printf '%s' "$1" >> "$MOCK_CALLS"; shift; printf '|%s' "$@" >> "$MOCK_CALLS"; printf '\n' >> "$MOCK_CALLS"; }
log_info() { printf '%s\n' "$1"; }
log_warn() { printf '%s\n' "$1" >&2; }
log_error() { printf '%s\n' "$1" >&2; }
uname() { printf '%s\n' "$MOCK_HOST"; }
hostname() { printf 'fixture-host\n'; }
id() { printf '0\n'; }
modprobe() { record modprobe "$@"; printf 'linux-modprobe\n' >&2; return 42; }
docker() {
    record docker "$@"
    case "$1" in
        info)
            case "${3:-}" in
                '{{.OperatingSystem}}') printf '%s\n' "$MOCK_OS" ;;
                '{{.OSType}}/{{.Architecture}}') printf '%s\n' "$MOCK_ENGINE" ;;
            esac
            ;;
        container) [[ "$3" == "${MOCK_COLLISION:-}" ]] ;;
        image)
            if [[ "${3:-}" != --format ]]; then
                [[ "$MOCK_LOCAL_IMAGE" == true ]]
                return
            fi
            case "$4" in
                '{{.Os}}/{{.Architecture}}')
                    if [[ "$5" == "$TRAEFIK_IMAGE" ]]; then
                        printf '%s\n' "$MOCK_TRAEFIK_IMAGE"
                    else
                        printf '%s\n' "$MOCK_IMAGE"
                    fi
                    ;;
                *org.akernel.kata.enabled*) printf '%s\n' "$MOCK_KATA" ;;
                *org.akernel.firecracker.enabled*) printf '%s\n' "$MOCK_FIRECRACKER" ;;
                *org.akernel.runc.enabled*) printf '%s\n' "$MOCK_RUNC" ;;
                *) return 2 ;;
            esac
            ;;
        run|pull)
            local platform="${DOCKER_DEFAULT_PLATFORM:-linux/arm64}"
            local previous='' argument
            for argument in "$@"; do
                if [[ "$previous" == --platform ]]; then platform="$argument"; fi
                case "$argument" in --platform=*) platform="${argument#--platform=}" ;; esac
                previous="$argument"
            done
            if [[ "$MOCK_HOST" == Darwin && "$platform" != linux/arm64 ]]; then return 87; fi
            if [[ "$1" == run && "$*" == *'/orbstack-preflight.sh' ]]; then return "$MOCK_PROBE_STATUS"; fi
            ;;
        *) return 2 ;;
    esac
}
pouch() { docker "$@"; }
DOCKER_CMD=docker
DOCKER_PREFIX=()
CONTAINER_PLATFORM_ARGS=()
PROXY_RUN_ARGS=()
GPU_RUN_ARGS=()
'''
        names = (
            "check_prerequisites", "prepare_data", "configure_auth",
            "cleanup_existing", "ensure_image",
            "validate_container_proxy", "configure_container_proxy",
            "validate_host_platform", "validate_image_platform",
            "validate_image_capabilities", "prepare_host_network_modules",
            "start_node_container", "start_traefik_container",
        )
        functions = "\n".join(
            function_from(STANDALONE_DIR / "start.sh", name) for name in names
        )
        return subprocess.run(
            ["/bin/bash", "-e", "-c", stubs + functions + "\n" + commands],
            cwd=self.directory, env=self.environment | overrides,
            capture_output=True, text=True, timeout=10, check=False,
        )

    def run_main(self, **overrides):
        # Execute the real Main body. Only unrelated side effects are replaced;
        # gates, image selection, data setup and Docker run argv stay real.
        stubs = r'''
configure_auth() { record stage configure_auth; }
configure_container_proxy() { record stage configure_container_proxy; }
configure_gpu() { record stage configure_gpu; }
configure_network() { record stage configure_network; }
wait_for_ready() { record stage wait_for_ready; }
container_ip() { printf '192.0.2.44\n'; }
write_traefik_config() { record stage write_traefik_config; }
wait_for_gateway() { record stage wait_for_gateway; }
show_status() { record stage show_status; }
'''
        main = (STANDALONE_DIR / "start.sh").read_text().split("# Main\n", 1)[1]
        return self.run_script(stubs + main, **overrides)

    def assert_no_profile_mutation(self):
        self.assertFalse((self.directory / "data").exists())
        self.assertFalse(any(call[0] == "stage" for call in self.recorded_calls()))
        self.assertFalse(any(call[:2] == ["docker", "run"] for call in self.recorded_calls()))

    def test_invalid_firecracker_backend_is_rejected_before_profile_mutation(self):
        result = self.run_main(AKERNEL_FIRECRACKER_BACKEND="invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AKERNEL_FIRECRACKER_BACKEND must be kvm or pvm", result.stderr)
        self.assert_no_profile_mutation()

    def test_native_runc_uses_only_disposable_capability_probe(self):
        result = self.run_script(
            "validate_host_platform\nvalidate_image_capabilities\nprepare_host_network_modules"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = next(call[1:] for call in self.recorded_calls() if call[1] == "run")
        self.assertEqual(arguments[:7], [
            "run", "--platform", "linux/arm64", "--rm", "--privileged", "--net", "bridge"
        ])
        self.assertIn("AKERNEL_ENABLE_RUNC=true", arguments)
        self.assertIn(f"{self.directory}/data:/home/akernel", arguments)
        self.assertEqual(arguments[-3:], ["/bin/bash", "akernel-test:arm64", "/orbstack-preflight.sh"])

    def test_runsc_profile_does_not_require_runc_payload(self):
        result = self.run_main(AKERNEL_ENABLE_RUNC="false", MOCK_RUNC="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = next(call for call in self.recorded_calls() if "/orbstack-preflight.sh" in call)
        self.assertIn("AKERNEL_ENABLE_RUNC=false", probe)

    def test_main_rejects_unsupported_platforms_before_profile_mutation(self):
        cases = (
            ({"MOCK_OS": "Docker Desktop"}, "requires OrbStack"),
            ({"MOCK_ENGINE": "linux/x86_64"}, "native linux/arm64 Docker engine"),
            ({"MOCK_IMAGE": "linux/amd64"}, "not native linux/arm64"),
            ({"MOCK_IMAGE": "windows/arm64"}, "not native linux/arm64"),
            ({"MOCK_TRAEFIK_IMAGE": "linux/amd64"}, "not native linux/arm64"),
            ({"MOCK_KATA": "true"}, "image must disable kata"),
            ({"MOCK_FIRECRACKER": "<no value>"}, "image must disable firecracker"),
            ({"MOCK_RUNC": "false"}, "runc payload enabled"),
            ({"MOCK_RUNC": "<no value>"}, "runc payload enabled"),
            ({"AKERNEL_ENABLE_GPU": "true"}, "GPU disabled"),
            ({"AKERNEL_ENABLE_GPU": "tru"}, "AKERNEL_ENABLE_GPU must be true or false"),
            ({"AKERNEL_ENABLE_KATA": "true"}, "Kata, Firecracker"),
            ({"AKERNEL_ENABLE_FIRECRACKER": "true"}, "Kata, Firecracker"),
            ({"AKERNEL_NAT_BACKEND": "bpfnat"}, "iptables networking"),
            ({"AKERNEL_NAT_BACKEND": "invalid"}, "must be 'iptables' or 'bpfnat'"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                self.calls.unlink(missing_ok=True)
                result = self.run_main(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assert_no_profile_mutation()

    def test_main_runs_probe_before_auth_network_or_container_start(self):
        result = self.run_main()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded_calls()
        probe = next(i for i, call in enumerate(calls) if "/orbstack-preflight.sh" in call)
        auth = calls.index(["stage", "configure_auth"])
        network = calls.index(["stage", "configure_network"])
        node = next(i for i, call in enumerate(calls) if "akernel-node" in call and "run" in call)
        self.assertLess(probe, auth)
        self.assertLess(auth, network)
        self.assertLess(network, node)
        self.assertTrue((self.directory / "data/sandboxd/config").is_dir())

    def test_probe_failure_does_not_regenerate_auth_or_network(self):
        result = self.run_main(MOCK_PROBE_STATUS="45")
        self.assertEqual(result.returncode, 45, result.stderr)
        self.assertFalse(any(call[0] == "stage" for call in self.recorded_calls()))
        self.assertEqual(sum(call[:2] == ["docker", "run"] for call in self.recorded_calls()), 1)

    def test_conflicting_docker_default_is_overridden_for_pull_probe_node_and_gateway(self):
        result = self.run_main(DOCKER_DEFAULT_PLATFORM="linux/amd64", MOCK_LOCAL_IMAGE="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [call for call in self.recorded_calls() if call[1] in ("pull", "run")]
        self.assertEqual(len(commands), 5)
        for command in commands:
            self.assertEqual(command[command.index("--platform") + 1], "linux/arm64")

    def test_wrong_architecture_cached_tag_is_not_pulled_or_overwritten(self):
        result = self.run_main(MOCK_IMAGE="linux/amd64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Existing local tags are not overwritten automatically", result.stderr)
        self.assertFalse(any(call[1] == "pull" for call in self.recorded_calls()))
        self.assert_no_profile_mutation()

    def test_linux_and_pouch_also_gate_explicit_runc_payload(self):
        for runtime in ("docker", "pouch"):
            with self.subTest(runtime=runtime):
                result = self.run_script(
                    f"DOCKER_CMD={runtime}\nvalidate_host_platform\nvalidate_image_capabilities",
                    MOCK_HOST="Linux", MOCK_RUNC="<no value>",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("runc payload enabled", result.stderr)

    def test_linux_still_uses_host_module_loading_without_platform_override(self):
        result = self.run_script(
            "validate_host_platform\nprepare_host_network_modules", MOCK_HOST="Linux"
        )
        self.assertEqual(result.returncode, 42)
        self.assertIn("linux-modprobe", result.stderr)
        self.assertIn(["modprobe", "tun"], self.recorded_calls())
        self.assertFalse(any(call[:2] == ["docker", "run"] for call in self.recorded_calls()))

    def test_linux_gpu_option_typo_is_rejected_before_profile_mutation(self):
        result = self.run_main(MOCK_HOST="Linux", AKERNEL_ENABLE_GPU="tru")
        self.assertEqual(result.returncode, 1)
        self.assertIn("AKERNEL_ENABLE_GPU must be true or false", result.stderr)
        self.assert_no_profile_mutation()

    def test_prerequisites_are_readonly_and_data_setup_is_separate(self):
        result = self.run_script("check_prerequisites", DATA_DIR="relative-data")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be an absolute path", result.stderr)
        self.assertFalse((self.directory / "relative-data").exists())
        result = self.run_script("check_prerequisites")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.directory / "data").exists())
        result = self.run_script("prepare_data")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.directory / "data/sandboxd/config").is_dir())

    def test_existing_node_or_gateway_is_rejected_without_cleanup(self):
        for container in ("akernel-node", "akernel-traefik"):
            with self.subTest(container=container):
                self.calls.unlink(missing_ok=True)
                result = self.run_main(MOCK_COLLISION=container)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Existing container '{container}'", result.stderr)
                self.assert_no_profile_mutation()
                self.assertFalse(any(call[1] in ("stop", "rm") for call in self.recorded_calls()))

    def test_proxy_values_are_protected_and_absent_from_logs_and_docker_argv(self):
        proxy = "http://fixture-user:fixture-password@host.docker.internal:7890"
        result = self.run_script(
            "prepare_data\nconfigure_container_proxy\nstart_node_container",
            AKERNEL_CONTAINER_PROXY=proxy,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        env_file = self.directory / "data/proxy.env"
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
        self.assertIn(f"HTTPS_PROXY={proxy}\n", env_file.read_text())
        self.assertIn("NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16\n", env_file.read_text())
        self.assertEqual(list((self.directory / "data").glob(".proxy.env.*")), [])
        call = next(call for call in self.recorded_calls() if call[1] == "run")
        self.assertIn("--env-file", call)
        self.assertIn(str(env_file), call)
        self.assertIn(f"{env_file}:/etc/akernel/proxy.env:ro", call)
        self.assertNotIn(proxy, result.stdout + result.stderr + self.calls.read_text())

    def test_proxy_line_breaks_are_rejected_before_profile_mutation_without_echo(self):
        for variable in ("AKERNEL_CONTAINER_PROXY", "AKERNEL_CONTAINER_NO_PROXY"):
            for line_break in ("\r", "\n"):
                with self.subTest(variable=variable, line_break=line_break):
                    result = self.run_main(**{
                        "AKERNEL_CONTAINER_PROXY": "http://fixture-proxy:7890",
                        variable: f"fixture-secret{line_break}INJECTED=value",
                    })
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("must not contain line breaks", result.stderr)
                    self.assertNotIn("fixture-secret", result.stdout + result.stderr)
                    self.assert_no_profile_mutation()

    def test_new_auth_seed_is_private_even_with_wide_umask(self):
        # The synthetic generator also checks permissions before writing data,
        # so a later chmod cannot hide an initially world-readable temp file.
        result = self.run_script(r'''
umask 022
python3() {
    command python3 -c 'import glob, os; paths=glob.glob(os.environ["DATA_DIR"]+"/.iam-seed.*"); assert len(paths)==1; assert os.stat(paths[0]).st_mode & 0o777 == 0o600; print("A1"*32)'
}
prepare_data
configure_auth
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        seed = self.directory / "data/iam-seed"
        self.assertEqual(seed.read_text(), "A1" * 32 + "\n")
        self.assertEqual(seed.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.directory / "data/token").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list((self.directory / "data").glob(".iam-seed.*")), [])
        self.assertNotIn("A1" * 32, result.stdout + result.stderr)

    def test_existing_auth_seed_is_tightened_without_rotating_identity(self):
        seed = self.directory / "data/iam-seed"
        seed.parent.mkdir()
        contents = "b2" * 32 + "\n"
        seed.write_text(contents)
        seed.chmod(0o644)
        result = self.run_script("umask 022\nconfigure_auth")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(seed.read_text(), contents)
        self.assertEqual(seed.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("Generated a deployment-specific IAM seed", result.stdout)
        self.assertNotIn(contents.strip(), result.stdout + result.stderr)

    def test_invalid_auth_seed_failure_does_not_echo_seed(self):
        seed = self.directory / "data/iam-seed"
        seed.parent.mkdir()
        seed.write_text("fixture-secret-invalid-hex")
        result = self.run_script("configure_auth")
        self.assertEqual(result.returncode, 1)
        self.assertIn("even-length hexadecimal seed", result.stderr)
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)
        self.assertFalse((self.directory / "data/token").exists())


class PreflightCleanupTest(unittest.TestCase):
    def run_cleanup(self, *, failed_unmount="", status=0, partial=False, cgroup_failure=False):
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
        environment = isolated_environment()
        environment.update(
            MOCK_PROBE=str(self.probe), MOCK_CGROUP=str(self.cgroup),
            MOCK_FAILED_UNMOUNT=failed_unmount, MOCK_STATUS=str(status),
            MOCK_PARTIAL="true" if partial else "false",
            MOCK_CGROUP_FAILURE="true" if cgroup_failure else "false",
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
umount() { printf 'umount %s\n' "$1"; [[ "$1" != "$probe_dir/$MOCK_FAILED_UNMOUNT" ]]; }
ip() { printf 'ip %s\n' "$*"; }
ipset() { printf 'ipset %s\n' "$*"; }
iptables() { printf 'iptables %s\n' "$*"; }
ip6tables() { printf 'ip6tables %s\n' "$*"; }
if [[ "$MOCK_PARTIAL" == true || "$MOCK_CGROUP_FAILURE" == true ]]; then
    ext4_mounted=false; lower_mounted=false; overlay_mounted=false
fi
if [[ "$MOCK_PARTIAL" == true ]]; then tap_created=true; ipv4_chain_created=true; fi
'''
        commands = '\ntrap cleanup EXIT\nexit "$MOCK_STATUS"\n'
        if cgroup_failure:
            commands = r'''
trap cleanup EXIT
cgroup_probe=''
mktemp() { return 47; }
cgroup_probe="$(mktemp -d /sys/fs/cgroup/akernel-preflight.XXXXXX)"
'''
        return subprocess.run(
            ["/bin/bash", "-e", "-c", script
             + function_from(STANDALONE_DIR / "orbstack-preflight.sh", "cleanup")
             + commands],
            env=environment, capture_output=True, text=True, timeout=10, check=False,
        )

    def test_cleanup_removes_only_owned_paths_and_preserves_original_failure(self):
        result = self.run_cleanup(status=43)
        self.assertEqual(result.returncode, 43, result.stderr)
        self.assertFalse(self.probe.exists())
        self.assertFalse(self.cgroup.exists())
        self.assertEqual(self.sentinel.read_text(), "preserved")

    def test_unmount_failure_is_reported_and_keeps_backing_image(self):
        for mount in ("overlay", "lower", "ext4"):
            with self.subTest(mount=mount):
                result = self.run_cleanup(failed_unmount=mount)
                self.assertEqual(result.returncode, 1)
                self.assertIn("preflight cleanup failed", result.stderr)
                self.assertTrue((self.probe / "filestore.img").exists())
                self.assertEqual(self.sentinel.read_text(), "preserved")

    def test_cgroup_allocation_failure_cleans_probe_without_empty_cgroup_removal(self):
        result = self.run_cleanup(cgroup_failure=True)
        self.assertEqual(result.returncode, 47, result.stderr)
        self.assertFalse(self.probe.exists())
        self.assertTrue(self.cgroup.exists())
        self.assertEqual(self.sentinel.read_text(), "preserved")

    def test_partial_creation_cleans_only_resources_marked_owned(self):
        result = self.run_cleanup(partial=True, status=44)
        self.assertEqual(result.returncode, 44, result.stderr)
        self.assertIn("ip link del akp0", result.stdout)
        self.assertIn("iptables -t filter -F AKP4", result.stdout)
        self.assertIn("iptables -t filter -X AKP4", result.stdout)
        self.assertNotIn("akpv0", result.stdout)
        self.assertNotIn("AKPSET", result.stdout)
        self.assertNotIn("AKP6", result.stdout)
        self.assertNotIn("umount", result.stdout)
        self.assertFalse(self.probe.exists())
        self.assertFalse(self.cgroup.exists())
        self.assertEqual(self.sentinel.read_text(), "preserved")


if __name__ == "__main__":
    unittest.main()
