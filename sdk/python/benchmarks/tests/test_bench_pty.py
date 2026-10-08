"""PTY benchmark verifies output, exit status, and session cleanup."""

import unittest

from benchmarks import bench_pty


class _Session:
    def __init__(self, code=0, output=b"marker"):
        self.code = code
        self.output = output
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def wait(self, timeout=None):
        return self.code


class _Pty:
    def __init__(self, session):
        self.session = session

    def create(self, command, *, on_data, timeout):
        on_data(self.session.output)
        return self.session


class _Sandbox:
    def __init__(self, session):
        self.pty = _Pty(session)


class PtyBenchmarkTest(unittest.TestCase):
    def test_success_checks_output_and_closes_session(self):
        session = _Session()
        measurement = bench_pty.run_pty_case(_Sandbox(session), "marker")
        self.assertGreaterEqual(measurement["full_seconds"], 0)
        self.assertLessEqual(measurement["output_seconds"], measurement["full_seconds"])
        self.assertTrue(session.closed)

    def test_nonzero_exit_is_a_failure_and_session_closes(self):
        session = _Session(code=7)
        with self.assertRaisesRegex(AssertionError, "exit code 7"):
            bench_pty.run_pty_case(_Sandbox(session), "marker")
        self.assertTrue(session.closed)

    def test_wrong_output_is_a_failure_and_session_closes(self):
        session = _Session(output=b"other")
        with self.assertRaisesRegex(AssertionError, "output mismatch"):
            bench_pty.run_pty_case(_Sandbox(session), "marker")
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
