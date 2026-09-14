"""Exercise the real bootstrap supervisor with a blocked CLI cleanup."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


class NodeSupervisorTest(unittest.TestCase):
    def test_signal_waits_for_cleanup(self):
        source = (Path(__file__).resolve().parents[1] / "scripts/yr_node_bootstrap.sh").read_text()
        function = source.split("run_yuanrong() {", 1)[1].split('\nif [  "x${AKS_LOCAL_MODE}"', 1)[0]
        function = "run_yuanrong() {" + function
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                child = root / "cli.sh"
                child.write_text('''#!/bin/bash
cleanup() {
    echo signal >> "$1/signals"
    touch "$1/draining"
    while [ ! -f "$1/release" ]; do sleep 0.02; done
    touch "$1/cleaned"
    exit 7
}
trap 'cleanup "$1"' TERM
touch "$1/ready"
while :; do sleep 0.02; done
''')
                supervisor = root / "supervisor.sh"
                supervisor.write_text(function + '\nrun_yuanrong /bin/bash "$1" "$2"\n')
                process = subprocess.Popen(
                    ["/bin/bash", str(supervisor), str(child), directory], start_new_session=True
                )
                try:
                    self.wait_file(root / "ready", process)
                    process.send_signal(sig)
                    self.wait_file(root / "draining", process)
                    process.send_signal(sig)
                    time.sleep(0.15)
                    self.assertIsNone(process.poll(), "Bash exited before CLI cleanup")
                    (root / "release").touch()
                    self.assertEqual(process.wait(timeout=5), 7)
                    self.assertTrue((root / "cleaned").exists())
                    self.assertEqual((root / "signals").read_text().splitlines(), ["signal"])
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)

    def wait_file(self, path, process):
        deadline = time.monotonic() + 5
        while not path.exists():
            self.assertIsNone(process.poll(), "Supervisor exited while waiting for " + path.name)
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
