#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Test index-refresh recovery and failure without accessing host APT state."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().with_name("apt-get-update.sh")


class AptUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="akernel-apt-update-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.calls = self.directory / "calls"
        self.delays = self.directory / "delays"
        apt = self.bin / "apt-get"
        apt.write_text('''#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FIXTURE_CALLS"
attempt=$(wc -l < "$FIXTURE_CALLS")
if [ "$attempt" -le "$FIXTURE_FAILURES" ]; then
    echo 'E: File has unexpected size. Mirror sync in progress?' >&2
    exit "$FIXTURE_STATUS"
fi
if [ "${FIXTURE_PARTIAL_UPDATE:-0}" = 1 ]; then
    echo 'W: Some index files failed to download.' >&2
    case " $* " in
        *' APT::Update::Error-Mode=any '*) exit 100 ;;
    esac
fi
''')
        apt.chmod(0o755)
        sleep = self.bin / "sleep"
        sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$FIXTURE_DELAYS"\n')
        sleep.chmod(0o755)
        self.environment = {
            name: value for name, value in os.environ.items()
            if name not in ("BASH_ENV", "ENV")
        }
        self.environment.update(
            PATH=f"{self.bin}:/usr/bin:/bin",
            FIXTURE_CALLS=str(self.calls),
            FIXTURE_DELAYS=str(self.delays),
            FIXTURE_FAILURES="0",
            FIXTURE_STATUS="100",
        )

    def run_update(self, **values):
        return subprocess.run(
            ["/bin/sh", str(SCRIPT)], env=self.environment | values,
            cwd=self.directory, capture_output=True, text=True, timeout=10,
        )

    def test_success_does_not_retry_or_sleep(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertFalse(self.delays.exists())

    def test_mirror_sync_failure_recovers_after_full_refreshes(self):
        result = self.run_update(FIXTURE_FAILURES="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call.endswith(" update") for call in calls))
        self.assertEqual(self.delays.read_text().splitlines(), ["5", "10"])
        self.assertIn("File has unexpected size", result.stderr)

    def test_persistent_failure_is_bounded_and_preserves_exit_status(self):
        result = self.run_update(FIXTURE_FAILURES="10", FIXTURE_STATUS="42")
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 5)
        self.assertEqual(self.delays.read_text().splitlines(), ["5", "10", "15", "20"])
        self.assertIn("failed after 5 attempts", result.stderr)

    def test_partial_index_update_cannot_succeed_using_stale_indexes(self):
        result = self.run_update(FIXTURE_PARTIAL_UPDATE="1")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 5)


if __name__ == "__main__":
    unittest.main()
