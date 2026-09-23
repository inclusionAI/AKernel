#!/usr/bin/env python3

"""Focused tests for the standalone deployment driver."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("start.sh")


class StartScriptTest(unittest.TestCase):
    def test_deployment_generates_reuses_and_exposes_current_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, TEST_DATA=str(root))
            command = f'source {SCRIPT}; DATA_DIR="$TEST_DATA"; TOKEN_FILE="$DATA_DIR/token"; configure_auth'
            def initialize():
                return subprocess.run(["bash", "-c", command], env=env, check=True, capture_output=True)
            initialize()
            key = root / "adx/secrets/admin-key"
            token = root / "token"
            original = key.read_text().strip()
            self.assertRegex(original, r"^[0-9a-f]{64}$")
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(token.read_text().strip(), original)
            initialize()
            self.assertEqual(key.read_text().strip(), original)
            rotated = "b" * 64
            key.write_text(rotated)
            initialize()
            self.assertEqual(token.read_text(), rotated)

    def test_deployment_keeps_existing_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "token").write_text("existing-token-" + "a" * 40)
            subprocess.run(["bash", "-c", f'source {SCRIPT}; DATA_DIR="$TEST_DATA"; TOKEN_FILE="$DATA_DIR/token"; configure_auth'], env=dict(os.environ, TEST_DATA=str(root)), check=True, capture_output=True)
            self.assertTrue((root / "token").is_symlink())
            self.assertEqual((root / "token").read_text(), "existing-token-" + "a" * 40)


    def test_node_publishes_distinct_control_and_data_ports(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            '-p "${AKERNEL_CONTROL_BIND}:${AKERNEL_CONTROL_PORT}:8443"',
            script,
        )
        self.assertIn(
            '-p "${AKERNEL_DATA_BIND}:${AKERNEL_DATA_PORT}:8080"',
            script,
        )
        self.assertNotIn("TRAEFIK", script)

    def test_capacity_probe_waits_for_a_registered_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "admin-key"
            token.write_text("test-secret\n", encoding="utf-8")
            fake_curl = root / "curl"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    cat > /dev/null
                    printf '%s' "$CAPACITY_RESPONSE"
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{root}:{environment['PATH']}"
            command = (
                f"source {SCRIPT!s}; "
                f"TOKEN_FILE={token!s}; "
                "probe_adx_capacity https://127.0.0.1:8443/api/sandbox/v1/resources"
            )

            for payload, expected in (
                ('{"items":[]}', 1),
                (
                    '{"items":[{"allocatable":{"CPU":1000,"Memory":2048}}]}',
                    0,
                ),
            ):
                with self.subTest(payload=payload):
                    environment["CAPACITY_RESPONSE"] = payload
                    result = subprocess.run(
                        ["bash", "-c", command],
                        check=False,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_data_listener_probe_requires_control_api_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_curl = root / "curl"
            fake_curl.write_text(
                "#!/bin/sh\nprintf '%s' \"$DATA_STATUS\"\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{root}:{environment['PATH']}"
            command = (
                f"source {SCRIPT!s}; "
                "probe_adx_data_listener "
                "http://127.0.0.1:8080/api/sandbox/v1/resources"
            )

            for status, expected in (("426", 0), ("200", 1), ("000", 1)):
                with self.subTest(status=status):
                    environment["DATA_STATUS"] = status
                    result = subprocess.run(
                        ["bash", "-c", command],
                        check=False,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_health_probe_reads_token_from_curl_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "admin-key"
            token.write_text("test-secret\n", encoding="utf-8")
            fake_curl = root / "curl"
            captured = root / "curl-input"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    cat > "$CAPTURED_CURL_INPUT"
                    printf '%s\\n' "$@" > "$CAPTURED_CURL_ARGS"
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{root}:{environment['PATH']}",
                    "CAPTURED_CURL_INPUT": str(captured),
                    "CAPTURED_CURL_ARGS": str(root / "curl-args"),
                }
            )
            command = (
                f"source {SCRIPT!s}; "
                f"TOKEN_FILE={token!s}; "
                "probe_adx_health https://127.0.0.1:8443/healthz"
            )
            subprocess.run(
                ["bash", "-c", command],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                captured.read_text(encoding="utf-8"),
                'header = "X-Auth: test-secret"\n',
            )
            arguments = (root / "curl-args").read_text(encoding="utf-8")
            self.assertIn("https://127.0.0.1:8443/healthz", arguments)
            self.assertNotIn("test-secret", arguments)

    def test_health_probe_waits_for_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-key"
            command = (
                f"source {SCRIPT!s}; "
                f"TOKEN_FILE={missing!s}; "
                "! probe_adx_health https://127.0.0.1:8443/healthz"
            )
            subprocess.run(
                ["bash", "-c", command],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_node_health_probe_executes_inside_node_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_docker = root / "docker"
            captured = root / "docker-args"
            fake_docker.write_text(
                '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$CAPTURED_DOCKER_ARGS"\n',
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            environment = os.environ.copy()
            environment["CAPTURED_DOCKER_ARGS"] = str(captured)
            command = (
                f"source {SCRIPT!s}; "
                f"DOCKER_CMD={fake_docker!s}; "
                "NODE_CONTAINER_NAME=test-node; "
                "probe_node_health http://127.0.0.1:18080/healthz"
            )
            subprocess.run(
                ["bash", "-c", command],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )

            arguments = captured.read_text(encoding="utf-8")
            self.assertTrue(arguments.startswith("exec\ntest-node\ncurl\n"))
            self.assertIn("http://127.0.0.1:18080/healthz", arguments)


if __name__ == "__main__":
    unittest.main()
