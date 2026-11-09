"""Native export retries preserve diagnostics, cache policy and definite outcomes."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    "runtime_export_retry", Path(__file__).with_name("compose.py")
)
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)

COMMAND = ["docker", "buildx", "bake", "--file", "/frozen/bake.json", "default"]
MISSING = b'ERROR: failed to solve: failed to get compression blob "nydus": not found\n'
MERGE_MISSING = b'ERROR: failed to solve: merge nydus layers: get compression blob "nydus": not found\n'


def process(code, stderr):
    result = MagicMock()
    result.__enter__.return_value = result
    result.stderr = io.BytesIO(stderr)
    result.wait.return_value = code
    return result


class NativeExportRetryTest(unittest.TestCase):
    def test_real_process_pipe_streams_large_progress_and_retries_same_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "docker"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json,os,pathlib,sys\n"
                "p=pathlib.Path(os.environ['AKERNEL_EXPORT_TEST_COUNTER'])\n"
                "rows=json.loads(p.read_text())if p.exists()else[]\n"
                "rows.append(sys.argv[1:]);p.write_text(json.dumps(rows))\n"
                "if len(rows)==1:\n"
                " sys.stderr.buffer.write(b'x'*1000000+b'\\n'+"
                + repr(MISSING)
                + ");sys.stderr.flush();sys.exit(1)\n"
            )
            executable.chmod(0o700)
            counter = root / "calls.json"
            output = io.BytesIO()
            text = io.TextIOWrapper(output, encoding="utf-8", write_through=True)
            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": str(root) + os.pathsep + os.environ["PATH"],
                        "AKERNEL_EXPORT_TEST_COUNTER": str(counter),
                    },
                ),
                patch.object(compose.sys, "stderr", text),
                patch.object(compose.time, "sleep"),
            ):
                compose.execute_bake(COMMAND, {"target": {"main": {}}})
                text.flush()
                self.assertEqual(
                    json.loads(counter.read_text()), [COMMAND[1:], COMMAND[1:]]
                )
                self.assertIn(MISSING, output.getvalue())
                self.assertIn(b"retry 2/3", output.getvalue())

    def run_export(self, processes, targets=None):
        output = io.BytesIO()
        text = io.TextIOWrapper(output, encoding="utf-8", write_through=True)
        with (
            patch.object(compose.subprocess, "Popen", side_effect=processes) as spawn,
            patch.object(compose.sys, "stderr", text),
            patch.object(compose.time, "sleep") as sleep,
        ):
            compose.execute_bake(COMMAND, {"target": targets or {"main": {}}})
            text.flush()
            return spawn.call_args_list, output.getvalue(), sleep.call_count

    def test_known_completed_export_failure_reuses_exact_command_and_keeps_first_error(
        self,
    ):
        calls, output, sleeps = self.run_export(
            [process(1, MISSING), process(0, b"done\n")]
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0].args, (COMMAND,))
        self.assertIn(MISSING, output)
        self.assertIn(b"retry 2/3", output)
        self.assertEqual(sleeps, 1)

    def test_same_variant_failure_during_merge_is_also_retryable(self):
        calls, output, sleeps = self.run_export(
            [process(1, MERGE_MISSING), process(0, b"done\n")]
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertIn(MERGE_MISSING, output)
        self.assertEqual(sleeps, 1)

    def test_persistent_export_failure_is_bounded_and_diagnostic_tail_is_bounded(self):
        huge = b"x" * 100000 + b"\n" + MISSING
        output = SimpleNamespace(
            buffer=io.BytesIO(), write=lambda _: None, flush=lambda: None
        )
        with (
            patch.object(
                compose.subprocess,
                "Popen",
                side_effect=[process(1, huge) for _ in range(3)],
            ) as spawn,
            patch.object(compose.sys, "stderr", output),
            patch.object(compose.time, "sleep"),
            self.assertRaises(subprocess.CalledProcessError) as caught,
        ):
            compose.execute_bake(COMMAND, {"target": {"main": {}}})
        self.assertEqual(spawn.call_count, 3)
        self.assertLessEqual(len(caught.exception.stderr), 65536)
        self.assertIn(MISSING, caught.exception.stderr)

    def test_native_no_cache_on_any_target_disables_replay(self):
        with (
            patch.object(
                compose.subprocess, "Popen", return_value=process(1, MISSING)
            ) as spawn,
            patch.object(compose.sys, "stderr", SimpleNamespace(buffer=io.BytesIO())),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            compose.execute_bake(
                COMMAND, {"target": {"main": {}, "dependency": {"no-cache": True}}}
            )
        self.assertEqual(spawn.call_count, 1)

    def test_build_and_authorization_errors_are_not_retried(self):
        for terminal in [
            b"ERROR: failed to solve: RUN did not complete successfully: exit code: 1\n",
            b"ERROR: failed to push: 401 Unauthorized\n",
            MISSING + b"ERROR: later failure is not the known export error\n",
        ]:
            with self.subTest(error=terminal):
                with (
                    patch.object(
                        compose.subprocess, "Popen", return_value=process(1, terminal)
                    ) as spawn,
                    patch.object(
                        compose.sys, "stderr", SimpleNamespace(buffer=io.BytesIO())
                    ),
                    self.assertRaises(subprocess.CalledProcessError),
                ):
                    compose.execute_bake(COMMAND, {"target": {"main": {}}})
                self.assertEqual(spawn.call_count, 1)

    def test_unknown_process_error_keeps_original_identity_without_replay(self):
        error = OSError("unknown process outcome")
        with patch.object(compose.subprocess, "Popen", side_effect=error) as spawn:
            with self.assertRaises(OSError) as caught:
                compose.execute_bake(COMMAND, {"target": {"main": {}}})
        self.assertIs(caught.exception, error)
        self.assertEqual(spawn.call_count, 1)

    def test_process_signal_exit_is_not_an_export_retry(self):
        for code in (-15, 130, 137):
            with self.subTest(exit_code=code):
                with (
                    patch.object(
                        compose.subprocess, "Popen", return_value=process(code, MISSING)
                    ) as spawn,
                    patch.object(
                        compose.sys, "stderr", SimpleNamespace(buffer=io.BytesIO())
                    ),
                    self.assertRaises(subprocess.CalledProcessError),
                ):
                    compose.execute_bake(COMMAND, {"target": {"main": {}}})
                self.assertEqual(spawn.call_count, 1)

    def test_only_native_bake_is_eligible(self):
        with patch.object(compose.subprocess, "Popen") as spawn:
            with self.assertRaises(ValueError):
                compose.execute_bake(
                    ["docker", "compose", "up"], {"target": {"main": {}}}
                )
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
