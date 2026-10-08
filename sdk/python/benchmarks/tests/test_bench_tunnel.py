"""Reverse-tunnel benchmark response verification."""

import unittest
from types import SimpleNamespace

from benchmarks.bench_tunnel import probe_guest


class _Commands:
    def __init__(self, stdout: str, exit_code: int = 0) -> None:
        self.stdout = stdout
        self.exit_code = exit_code

    def run(self, _command: str, *, timeout: int):
        assert timeout == 20
        return SimpleNamespace(stdout=self.stdout, exit_code=self.exit_code)


class TunnelProbeTest(unittest.TestCase):
    def test_matching_guest_payload_succeeds(self):
        sandbox = SimpleNamespace(commands=_Commands("TUNNEL_case1"))
        self.assertGreaterEqual(probe_guest(sandbox, "TUNNEL_case1"), 0)

    def test_wrong_payload_or_exit_code_fails(self):
        for commands in (_Commands("other"), _Commands("TUNNEL_case1", 1)):
            with self.subTest(commands=commands):
                sandbox = SimpleNamespace(commands=commands)
                with self.assertRaisesRegex(AssertionError, "response mismatch"):
                    probe_guest(sandbox, "TUNNEL_case1")


if __name__ == "__main__":
    unittest.main()
