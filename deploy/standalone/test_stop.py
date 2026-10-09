# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Exercise stop.sh's real entrypoint with an isolated fake runtime."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


STOP_SCRIPT = Path(__file__).resolve().parent / "stop.sh"
GATEWAY_ID = "a" * 64
NODE_ID = "b" * 64


class StopTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.calls = self.directory / "calls"
        self.environment = dict(os.environ)
        for name in ("BASH_ENV", "ENV", "DOCKER_DEFAULT_PLATFORM"):
            self.environment.pop(name, None)
        # This PATH contains no host Docker/Pouch/sudo executables. The script
        # and fake runtimes require only Bash builtins, so fallback is isolated.
        self.environment.update(
            PATH=str(self.bin), MOCK_CALLS=str(self.calls),
            MOCK_MISSING="", MOCK_STOP_FAILURE="", MOCK_RM_FAILURE="",
            MOCK_INFO_FAILURE="false", MOCK_SUDO_AVAILABLE="false",
            MOCK_INVENTORY_FAILURE="false", MOCK_INVENTORY_INVALID="false",
            MOCK_GATEWAY_ID=GATEWAY_ID, MOCK_NODE_ID=NODE_ID,
        )
        self.runtime = r'''#!/bin/bash
printf '%s' "${0##*/}" >> "$MOCK_CALLS"
printf '|%s' "$@" >> "$MOCK_CALLS"
printf '\n' >> "$MOCK_CALLS"
case "$1" in
    info) [[ "$MOCK_INFO_FAILURE" != true || "${MOCK_PRIVILEGED:-false}" == true ]] ;;
    ps)
        if [[ "$MOCK_INVENTORY_FAILURE" == true ]]; then
            printf 'fixture-private-diagnostic\n' >&2
            exit 33
        fi
        if [[ " $MOCK_MISSING " != *' akernel-traefik '* ]]; then
            printf '%s akernel-traefik\n' "$MOCK_GATEWAY_ID"
        fi
        if [[ " $MOCK_MISSING " != *' akernel-node '* ]]; then
            printf '%s /akernel-node\n' "$MOCK_NODE_ID"
        fi
        printf '%s akernel-node-unrelated\n' "$MOCK_GATEWAY_ID"
        if [[ "$MOCK_INVENTORY_INVALID" == true ]]; then
            printf 'not-an-id akernel-node\n'
        fi
        ;;
    stop)
        container=akernel-node
        if [[ "$2" == "$MOCK_GATEWAY_ID" ]]; then container=akernel-traefik; fi
        if [[ " $MOCK_STOP_FAILURE " == *" $container "* ]]; then
            printf 'fixture-private-diagnostic\n' >&2
            exit 31
        fi
        ;;
    rm)
        container=akernel-node
        if [[ "$2" == "$MOCK_GATEWAY_ID" ]]; then container=akernel-traefik; fi
        if [[ " $MOCK_RM_FAILURE " == *" $container "* ]]; then
            printf 'fixture-private-diagnostic\n' >&2
            exit 32
        fi
        ;;
    *) exit 99 ;;
esac
'''
        self.write_executable("docker", self.runtime)
        self.write_executable("sudo", r'''#!/bin/bash
printf 'sudo' >> "$MOCK_CALLS"
printf '|%s' "$@" >> "$MOCK_CALLS"
printf '\n' >> "$MOCK_CALLS"
[[ "$MOCK_SUDO_AVAILABLE" == true ]] || exit 1
if [[ "$1" == -n ]]; then shift; fi
export MOCK_PRIVILEGED=true
exec "$@"
''')

    def write_executable(self, name, contents):
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o700)

    def run_stop(self, **overrides):
        result = subprocess.run(
            ["/bin/bash", str(STOP_SCRIPT)], env=self.environment | overrides,
            cwd=self.directory, capture_output=True, text=True, timeout=10,
            check=False,
        )
        self.assertNotIn("fixture-private-diagnostic", result.stdout + result.stderr)
        return result

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [line.split("|") for line in self.calls.read_text().splitlines()]

    def owned_calls(self):
        return [call[1:] for call in self.recorded_calls() if call[0] in ("docker", "pouch") and call[1] in ("stop", "rm")]

    def test_success_stops_gateway_before_node_without_force_or_prune(self):
        result = self.run_stop()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.owned_calls(), [
            ["stop", GATEWAY_ID], ["rm", GATEWAY_ID],
            ["stop", NODE_ID], ["rm", NODE_ID],
        ])
        self.assertIn(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}"], self.recorded_calls())
        self.assertIn("stopped successfully", result.stdout)

    def test_missing_containers_are_idempotent(self):
        for missing in ("akernel-traefik", "akernel-traefik akernel-node"):
            with self.subTest(missing=missing):
                self.calls.unlink(missing_ok=True)
                result = self.run_stop(MOCK_MISSING=missing)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("not found", result.stdout)
                calls = self.owned_calls()
                for container in missing.split():
                    container_id = GATEWAY_ID if container == "akernel-traefik" else NODE_ID
                    self.assertNotIn(["stop", container_id], calls)
                    self.assertNotIn(["rm", container_id], calls)

    def test_failures_are_accumulated_and_second_container_is_still_attempted(self):
        cases = (
            {"MOCK_STOP_FAILURE": "akernel-traefik"},
            {"MOCK_RM_FAILURE": "akernel-traefik"},
            {"MOCK_STOP_FAILURE": "akernel-node"},
            {"MOCK_RM_FAILURE": "akernel-node"},
            {"MOCK_STOP_FAILURE": "akernel-traefik akernel-node", "MOCK_RM_FAILURE": "akernel-traefik akernel-node"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.calls.unlink(missing_ok=True)
                result = self.run_stop(**overrides)
                self.assertEqual(result.returncode, 1)
                self.assertIn("shutdown incomplete", result.stdout)
                self.assertNotIn("stopped successfully", result.stdout)
                self.assertIn(["rm", NODE_ID], self.owned_calls())
                self.assertEqual(len(self.owned_calls()), 4)
                for container in overrides.get("MOCK_RM_FAILURE", "").split():
                    self.assertNotIn(f"Container removed: {container}", result.stdout)
                    self.assertIn(f"Failed to remove container: {container}", result.stdout)

    def test_pouch_fallback_is_supported(self):
        (self.bin / "docker").unlink()
        self.write_executable("pouch", self.runtime)
        result = self.run_stop()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Found Pouch", result.stdout)
        self.assertTrue(all(call[0] == "pouch" for call in self.recorded_calls()))

    def test_passwordless_sudo_fallback_is_supported(self):
        result = self.run_stop(MOCK_INFO_FAILURE="true", MOCK_SUDO_AVAILABLE="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.owned_calls()), 4)
        self.assertEqual(sum(call[0] == "sudo" for call in self.recorded_calls()), 6)

    def test_inventory_failure_or_invalid_owned_ids_cannot_report_success(self):
        for override in ("MOCK_INVENTORY_FAILURE", "MOCK_INVENTORY_INVALID"):
            with self.subTest(override=override):
                self.calls.unlink(missing_ok=True)
                result = self.run_stop(**{override: "true"})
                self.assertEqual(result.returncode, 1)
                self.assertIn("shutdown was not attempted", result.stdout)
                self.assertNotIn("stopped successfully", result.stdout)
                self.assertEqual(self.owned_calls(), [])

    def test_unavailable_daemon_does_not_try_container_cleanup(self):
        result = self.run_stop(MOCK_INFO_FAILURE="true")
        self.assertEqual(result.returncode, 1)
        self.assertIn("daemon is not running", result.stdout)
        self.assertEqual(self.owned_calls(), [])

    def test_missing_runtime_fails_without_host_fallback(self):
        (self.bin / "docker").unlink()
        result = self.run_stop()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Neither Docker nor Pouch", result.stdout)
        self.assertEqual(self.recorded_calls(), [])


if __name__ == "__main__":
    unittest.main()
