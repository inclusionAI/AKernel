# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Exercise PTY connection cleanup through the public SDK and a real guest."""

import os
import time
import unittest
import uuid

from akernel_sdk import Sandbox

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_TOKEN"))
)
_RUNTIME = os.environ.get("AKERNEL_TEST_RUNTIME", "runsc")


@unittest.skipUnless(_ENABLED, "set AKERNEL_RUN_INTEGRATION=1 and SDK credentials")
class PtyLifecycleIntegrationTest(unittest.TestCase):
    def test_completed_pty_sessions_do_not_accumulate(self):
        sessions = []
        with Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048) as sandbox:
            try:
                for _ in range(100):
                    session = sandbox.pty.create(
                        command=("/bin/sh", "-c", "exit 7")
                    )
                    sessions.append(session)
                    self.assertEqual(session.wait(timeout=10), 7)
                self.assertEqual(len(sandbox.pty._sessions), 0)
                self.assertEqual(
                    sandbox.commands.run("printf PTY_READY").stdout, "PTY_READY"
                )
            finally:
                for session in sessions:
                    session.close()

    def test_close_terminates_guest_process_and_preserves_sandbox(self):
        marker = f"/tmp/akernel-pty-{uuid.uuid4().hex}.pid"
        with Sandbox(runtime=_RUNTIME, cpu=1000, memory=2048) as sandbox:
            session = sandbox.pty.create(
                command=("/bin/sh", "-c", f"echo $$ > {marker}; exec sleep 60")
            )
            closed = False
            try:
                deadline = time.monotonic() + 10
                while not sandbox.files.exists(marker):
                    if time.monotonic() >= deadline:
                        self.fail("PTY guest process did not write its PID")
                    time.sleep(0.1)
                pid = sandbox.files.read(marker).strip()
                self.assertTrue(pid.isdecimal(), pid)
                running = sandbox.commands.run(f"kill -0 {pid} 2>/dev/null", timeout=5)
                self.assertEqual(running.exit_code, 0, running.stderr)

                session.close()
                closed = True
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    alive = sandbox.commands.run(
                        f"kill -0 {pid} 2>/dev/null", timeout=5
                    )
                    if alive.exit_code != 0:
                        break
                    time.sleep(0.1)
                else:
                    self.fail(f"PTY process {pid} survived client close")
                self.assertEqual(
                    sandbox.commands.run("printf PTY_CLOSED").stdout, "PTY_CLOSED"
                )
            finally:
                if not closed:
                    session.close()
                if sandbox.files.exists(marker):
                    sandbox.files.remove(marker)


if __name__ == "__main__":
    unittest.main()
