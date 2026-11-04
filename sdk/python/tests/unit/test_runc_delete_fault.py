# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

"""Exercise real subprocesses against the bounded fixture injector."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.e2e.fault.runc_delete_fault import arm, install


@unittest.skipUnless(sys.platform == "linux", "Linux fixture process identity")
class RuncDeleteFaultTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "runc"
        self.real_log = self.root / "real.log"
        self.binary.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"with Path({str(self.real_log)!r}).open('a') as f: f.write('real\\n')\n"
        )
        self.binary.chmod(0o755)
        self.original = self.binary.read_bytes()
        self.fault = self.root / "fault"
        install(self.binary, self.fault)
        self.args = ["--root", "/run/sandboxd/runc", "delete", "--force", "target"]

    def invoke(self, args=None):
        return subprocess.run(
            [str(self.binary), *(args or self.args)], capture_output=True, timeout=10
        ).returncode

    def test_exactly_one_matching_delete_fails_and_next_delete_runs(self):
        arm(self.fault, "target", os.getpid())
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.real_log.read_text().splitlines(), ["real", "real"])
        self.assertEqual((self.fault / "real-runc").read_bytes(), self.original)
        self.assertTrue(self.binary.is_file())

    def test_unrelated_commands_do_not_consume_fault(self):
        arm(self.fault, "target", os.getpid())
        for args in [
            ["--version"],
            ["--root", "/run/sandboxd/runc", "kill", "target", "TERM"],
            self.args[:-1] + ["other"],
        ]:
            self.assertEqual(self.invoke(args), 0)
        self.assertTrue((self.fault / "armed.json").exists())
        self.assertNotEqual(self.invoke(), 0)

    def test_concurrent_deletes_consume_only_once(self):
        arm(self.fault, "target", os.getpid())
        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = list(pool.map(lambda _: self.invoke(), range(8)))
        self.assertEqual(codes.count(1), 1)
        self.assertEqual(codes.count(0), 7)

    def test_expired_fault_does_not_block_cleanup(self):
        arm(self.fault, "target", os.getpid())
        token = self.fault / "armed.json"
        value = json.loads(token.read_text())
        value["expires"] = time.time() - 1
        token.write_text(json.dumps(value))
        self.assertEqual(self.invoke(), 0)

    def test_restarted_daemon_does_not_inherit_fault(self):
        owner = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            arm(self.fault, "target", owner.pid)
        finally:
            owner.kill()
            owner.wait()
        self.assertEqual(self.invoke(), 0)

    def test_timeout_after_injection_cannot_disable_subsequent_cleanup(self):
        code = (
            "import os,runpy,subprocess,sys,time; "
            f"m=runpy.run_path({str(self.fault / 'helper.py')!r}); "
            f"m['arm']({str(self.fault)!r},'target',os.getpid()); "
            f"r=subprocess.run({[str(self.binary), *self.args]!r},"
            "capture_output=True); "
            "print(r.returncode,flush=True); time.sleep(30)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "1")
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=0.02)
        finally:
            child.kill()
            child.wait()
            child.stdout.close()
        self.assertEqual(self.invoke(), 0)
        self.assertFalse((self.fault / "armed.json").exists())


if __name__ == "__main__":
    unittest.main()
