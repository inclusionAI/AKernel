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

"""Fail one selected runc delete, then verify ordinary upstream retry cleanup."""

import hashlib
import json
import os
import subprocess
import time
import unittest
import uuid
from pathlib import Path

import httpx

from akernel_sdk import Sandbox
from akernel_sdk._addresses import api_endpoint_from_env
from tests.e2e.fault import runc_delete_fault
from tests.e2e.full.test_redis_lifecycle import _Redis
from tests.e2e.run import _capture_redis_inventory

_ENABLED = (
    os.environ.get("AKERNEL_RUN_INTEGRATION") == "1"
    and os.environ.get("AKERNEL_TEST_DESTRUCTIVE_FAULTS") == "1"
    and bool(os.environ.get("AKERNEL_SERVER_ADDRESS"))
    and bool(os.environ.get("AKERNEL_REDIS_HOST"))
    and bool(os.environ.get("AKERNEL_TEST_CONTAINER"))
    and bool(os.environ.get("AKERNEL_TEST_CONTAINER_ID"))
)

# Emit only ownership/retention evidence; OCI specs and metadata may contain secrets.
_SNAPSHOT = r"""
import hashlib,json,os,sys
from pathlib import Path
physical,marker,pid,cgroup=sys.argv[1:]
paths={
 "runtime":Path('/run/sandboxd/runc')/physical,
 "bundle":Path('/home/akernel/sandboxd/root/containers')/physical,
 "storage":Path('/home/akernel/filestore/.runc')/physical,
 "netns":Path('/run/netns')/('runc-'+physical),
}
bundle=paths['bundle']
state=paths['runtime']/'state.json'
if state.exists():
 data=json.loads(state.read_text())
 pid=str(data.get('init_process_pid') or pid)
config=bundle/'config.json'
if config.exists():
 raw=json.loads(config.read_text()).get('linux',{}).get('cgroupsPath','')
 if raw: cgroup=str(Path('/sys/fs/cgroup')/raw.lstrip('/'))
alive=False
if int(pid)>1:
 try: os.kill(int(pid),0);alive=True
 except ProcessLookupError: pass
file=bundle/'rootfs'/'tmp'/marker
result={k:p.exists() for k,p in paths.items()}
result.update(metadata=(bundle/'meta.pb').exists(),
 marker_sha256=hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else None,
 init_pid=int(pid),init_alive=alive,cgroup_path=cgroup,
 cgroup_exists=bool(cgroup) and Path(cgroup).exists(),
 mounts=[line for line in Path('/proc/self/mountinfo').read_text().splitlines()
         if physical in line])
print(json.dumps(result))
"""


@unittest.skipUnless(
    _ENABLED, "requires isolated standalone node and explicit fault opt-in"
)
class RuncDeleteRetryIntegrationTest(unittest.TestCase):
    def test_one_delete_failure_retains_ownership_then_retry_cleans(self):
        container = os.environ["AKERNEL_TEST_CONTAINER"]
        expected = os.environ["AKERNEL_TEST_CONTAINER_ID"]
        self.assertEqual(len(expected), 64, "set the full task container ID")
        output = Path(os.environ["AKERNEL_TEST_FAULT_OUTPUT"])
        output.mkdir(parents=True, exist_ok=True)
        name = "runc-delete-retry-" + uuid.uuid4().hex[:12]
        fault = "/home/akernel/" + name
        marker = name + ".bin"
        payload = uuid.uuid4().bytes * 4096
        namespace = os.environ.get("AKERNEL_REDIS_NAMESPACE", "akernel")
        control = f"adx:{{{namespace}}}:control:v1"
        evidence = {
            "status": "failed",
            "observations": [],
            "public_delete_requests_before_cleanup": 0,
        }
        sandbox = None
        physical, pid, cgroup = "", 0, ""
        installation_attempted = False

        def docker(*args):
            identity = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.Id}}", container],
                text=True,
                timeout=15,
            ).strip()
            self.assertEqual(identity, expected, "task container identity changed")
            return subprocess.check_output(
                ["docker", "exec", expected, *args], text=True, timeout=15
            )

        def record():
            client = _Redis()
            try:
                raw = client.call("HGET", control, "environment:" + sandbox.id)
                return json.loads(raw) if raw else None
            finally:
                client.close()

        def write_evidence():
            (output / "runc-delete-retry.json").write_text(
                json.dumps(evidence, indent=2) + "\n"
            )

        def snapshot(stage):
            nonlocal pid, cgroup
            state = json.loads(
                docker("python3", "-c", _SNAPSHOT, physical, marker, str(pid), cgroup)
            )
            pid, cgroup = state["init_pid"], state["cgroup_path"]
            current = record() or {}
            result = current.get("result") or {}
            state.update(
                stage=stage,
                time=time.time(),
                state=result.get("state"),
                resources_held=result.get("resources_held"),
                assignment=current.get("assignment"),
            )
            evidence["observations"].append(state)
            write_evidence()
            return state

        try:
            self.assertFalse(
                _capture_redis_inventory()["states"], "deployment must start empty"
            )
            self.assertEqual(
                docker(
                    "python3",
                    "-c",
                    "from pathlib import Path; "
                    "print(len(list(Path('/run/sandboxd/runc').glob('*'))))",
                ).strip(),
                "0",
            )
            self.assertEqual(
                docker(
                    "python3",
                    "-c",
                    "from pathlib import Path; "
                    "print(Path('/usr/local/bin/runc').read_bytes()[:4].hex())",
                ).strip(),
                "7f454c46",
            )
            installation_attempted = True
            evidence["real_runc_sha256"] = docker(
                "python3",
                "-c",
                "import pathlib,runpy,sys; p=pathlib.Path(sys.argv[1]+'.py'); "
                "p.write_text(sys.argv[2]); "
                "print(runpy.run_path(str(p))['install']("
                "'/usr/local/bin/runc',sys.argv[1])); p.unlink()",
                fault,
                Path(runc_delete_fault.__file__).read_text(),
            ).strip()
            kwargs = {
                "name": name,
                "runtime": "runc",
                "detached": True,
                "cpu": 1000,
                "memory": 2048,
                "schedule_timeout": 120,
            }
            if os.environ.get("AKERNEL_TEST_IMAGE"):
                kwargs["image"] = os.environ["AKERNEL_TEST_IMAGE"]
            sandbox = Sandbox(**kwargs)
            self.assertEqual(
                sandbox.commands.run("printf before-delete").stdout, "before-delete"
            )
            sandbox.files.write("/tmp/" + marker, payload)
            physical_ids = json.loads(
                docker(
                    "python3",
                    "-c",
                    "import json;from pathlib import Path; "
                    "print(json.dumps([p.name for p in "
                    "Path('/run/sandboxd/runc').iterdir()]))",
                )
            )
            self.assertEqual(len(physical_ids), 1)
            physical = physical_ids[0]
            running = snapshot("running")
            self.assertEqual(
                running["marker_sha256"], hashlib.sha256(payload).hexdigest()
            )
            self.assertTrue(
                all(
                    running[k]
                    for k in (
                        "metadata",
                        "storage",
                        "runtime",
                        "netns",
                        "init_alive",
                        "cgroup_exists",
                    )
                )
            )
            evidence.update(
                environment_id=sandbox.id,
                physical_id=physical,
                fixture_id=expected,
                fault_directory=fault,
            )
            remaining = (
                float(
                    os.environ.get(
                        "AKERNEL_E2E_DEADLINE_MONOTONIC", time.monotonic() + 360
                    )
                )
                - time.monotonic()
            )
            self.assertGreaterEqual(
                remaining, 100, "insufficient time for injection and ordinary cleanup"
            )
            config = {
                "script": _SNAPSHOT,
                "args": [physical, marker, "0", ""],
                "control": control,
                "environment": sandbox.id,
            }
            docker(
                "python3",
                "-c",
                "import json,runpy,sys;from pathlib import Path; "
                "pids=[int(p.name) for p in Path('/proc').iterdir() "
                "if p.name.isdigit() and (p/'comm').exists() "
                "and (p/'comm').read_text().strip()=='sandboxd']; "
                "assert len(pids)==1; "
                "runpy.run_path(sys.argv[1]+'/helper.py')['arm']("
                "sys.argv[1],sys.argv[2],pids[0],snapshot=json.loads(sys.argv[3]))",
                fault,
                physical,
                json.dumps(config),
            )
            with httpx.Client(verify=False, trust_env=False, timeout=90) as client:
                evidence["public_delete_requests_before_cleanup"] += 1
                response = client.delete(
                    api_endpoint_from_env().base_url()
                    + "/api/environment/v1/environments/"
                    + sandbox.id,
                    headers={"X-Auth": os.environ["AKERNEL_TOKEN"]},
                )
            evidence["delete_status"] = response.status_code
            self.assertTrue(
                response.status_code in (200, 202, 204) or response.status_code >= 500
            )
            deadline = time.monotonic() + 90
            while True:
                cleaned = snapshot("await-ordinary-retry-cleanup")
                physical_clean = not any(
                    cleaned[k]
                    for k in (
                        "metadata",
                        "storage",
                        "runtime",
                        "bundle",
                        "netns",
                        "init_alive",
                        "cgroup_exists",
                        "mounts",
                    )
                )
                if physical_clean and cleaned["resources_held"] is not True:
                    break
                self.assertLess(
                    time.monotonic(), deadline, "ordinary cleanup did not retry"
                )
                time.sleep(1)
            events = json.loads(
                docker(
                    "python3",
                    "-c",
                    "import json,sys;from pathlib import Path; "
                    "print(json.dumps([json.loads(p.read_text()) "
                    "for p in sorted(Path(sys.argv[1]).glob('*-*.json'))]))",
                    fault,
                )
            )
            evidence["delete_events"] = events
            injected = [e for e in events if e["action"] == "inject"]
            delegated = [e for e in events if e["action"] == "delegate"]
            self.assertEqual(len(injected), 1)
            self.assertTrue(
                delegated, "no successful follow-up runc delete was observed"
            )
            retained = delegated[0]["ownership"]
            self.assertTrue(
                all(
                    retained[k]
                    for k in (
                        "metadata",
                        "storage",
                        "runtime",
                        "netns",
                        "cgroup_exists",
                        "resources_held",
                    )
                )
            )
            self.assertEqual(retained["marker_sha256"], running["marker_sha256"])
            self.assertEqual(retained["assignment"], running["assignment"])
            evidence["automatic_cleanup_seconds"] = (
                cleaned["time"] - injected[0]["time"]
            )
            evidence["automatic_cleanup_verified"] = True
            self.assertEqual(evidence["public_delete_requests_before_cleanup"], 1)
            evidence["status"] = "passed"
        except BaseException as error:
            evidence["error"] = type(error).__name__
            raise
        finally:
            errors = []
            if installation_attempted:
                try:
                    docker(
                        "python3",
                        "-c",
                        "from pathlib import Path;import sys; "
                        "Path(sys.argv[1]+'/armed.json').unlink(missing_ok=True)",
                        fault,
                    )
                except Exception as error:
                    errors.append("disarm:" + type(error).__name__)
            if sandbox is not None:
                try:
                    sandbox.kill()
                    Sandbox.delete(name)
                    self.assertIsNone(record())
                    evidence["terminal_record_retired_by_explicit_cleanup"] = True
                except Exception as error:
                    errors.append("catalog:" + type(error).__name__)
            if errors:
                evidence.update(status="failed", cleanup_errors=errors)
            write_evidence()
            if errors and "error" not in evidence:
                self.fail("; ".join(errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
