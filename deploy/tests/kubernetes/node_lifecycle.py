#!/usr/bin/env python3
"""Opt-in destructive acceptance on an idle, dedicated AKernel Kubernetes cluster.

Deletes a Coordinator Pod twice and one Node Pod, and kills one owned adxlet.
Requires kubectl, the ADX Python SDK, admin credentials in the deployment Secret,
and at least two ready workers. Never run against a cluster with user Environments.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import subprocess
import time

from adx_environment import ConnectionConfig, Environment


# Read both Redis hashes in one operation. Metrics are refreshed asynchronously
# and cannot be used as the baseline of an exact-once fault assertion.
REDIS_PROBE = r'''
import hashlib,json,os,socket,urllib.parse,yaml
u=urllib.parse.urlsplit(os.environ['ADX_REDIS_URL'])
with socket.create_connection((u.hostname,u.port or 6379),timeout=5) as s, s.makefile('rb') as f:
 def read():
  t=f.read(1);line=f.readline()[:-2]
  if t==b'+':return line.decode()
  if t==b'-':raise RuntimeError('Redis read rejected')
  if t==b':':return int(line)
  if t==b'$':
   n=int(line)
   if n==-1:return None
   value=f.read(n);f.read(2);return value.decode()
  if t==b'*':return [read() for _ in range(int(line))]
  raise RuntimeError('Invalid Redis response')
 def cmd(*args):
  values=[str(v).encode() for v in args]
  s.sendall(b'*'+str(len(values)).encode()+b'\r\n'+b''.join(b'$'+str(len(v)).encode()+b'\r\n'+v+b'\r\n' for v in values));return read()
 if u.password is not None:
  password=urllib.parse.unquote(u.password)
  if u.username:cmd('AUTH',urllib.parse.unquote(u.username),password)
  else:cmd('AUTH',password)
 if u.path.strip('/'):cmd('SELECT',u.path.strip('/'))
 config=yaml.safe_load(open('/etc/akernel/adx-node.yaml'))
 key='adx:{'+config['namespace']+'}:control:v1'
 control,business=cmd('EVAL',"return {redis.call('HGETALL',KEYS[1]),redis.call('HGETALL',KEYS[2])}",2,key,key+':business')
 control=dict(zip(control[::2],control[1::2]));business=dict(zip(business[::2],business[1::2]))
 header=json.loads(control['header'])
 facts={k:json.loads(v).get('worker_incidents',0) for k,v in business.items()}
 nodes={k[5:]:{f:json.loads(v).get(f) for f in ('session','retirement','last_incident_id')} for k,v in control.items() if k.startswith('node:')}
 environments={k[12:]:{'invalidated':json.loads(v).get('invalidated'), 'state':(json.loads(v).get('result') or {}).get('state'), 'resources_held':(json.loads(v).get('result') or {}).get('resources_held')} for k,v in control.items() if k.startswith('environment:')}
 print(json.dumps({'epoch':header.get('epoch'),'incidents':header.get('observability',{}).get('worker_incidents',0)+sum(facts.values()),'facts':facts,'nodes':nodes,'environments':environments,'departed':[json.loads(v) for k,v in control.items() if k.startswith('node-departed:')]}))
'''

OFFLINE = r'''
import json,os,pathlib,signal,subprocess
pid=int(subprocess.check_output(['systemctl','show','adx.service','--property=MainPID','--value']))
assert pid>1 and pathlib.Path('/proc',str(pid),'comm').read_text().strip()=='adxctl'
children=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:
  parent=int(next(l.split()[1] for l in (p/'status').read_text().splitlines() if l.startswith('PPid:')))
  if parent==pid and (p/'comm').read_text().strip()=='adxlet':children.append(int(p.name))
 except (FileNotFoundError,ProcessLookupError):pass
assert len(children)==1
os.kill(pid,signal.SIGSTOP)
try:os.kill(children[0],signal.SIGKILL)
except BaseException:os.kill(pid,signal.SIGCONT);raise
print(json.dumps({'supervisor':pid,'adxlet':children[0]}))
'''


class Acceptance:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.report: dict = {"passed": False, "events": [], "cleanup_errors": []}
        self.environments: list[Environment] = []
        self.paused: tuple[str, int] | None = None
        self.finalized: str | None = None
        self.delayed: str | None = None
        self.finalizer = "akernel.io/lifecycle-acceptance"

    def kube(self, *args: str) -> str:
        result = subprocess.run(["kubectl", "--kubeconfig", self.args.kubeconfig,
                                 "-n", self.args.namespace, *args],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(f"kubectl {args[0]} failed: {result.stderr[-1000:]}")
        return result.stdout

    def execute(self, pod: str, *args: str) -> str:
        return self.kube("exec", pod, "-c", "akernel-node", "--", *args)

    def pods(self, selector: str) -> list[dict]:
        return json.loads(self.kube("get", "pods", "-l", selector, "-o", "json"))["items"]

    def node_pod(self, node: str) -> dict:
        return next(p for p in self.pods("app=node") if p["spec"]["nodeName"] == node
                    and not p["metadata"].get("deletionTimestamp"))

    def probe(self) -> dict:
        reader = self.node_pod(self.peer)["metadata"]["name"]
        return json.loads(self.execute(reader, "python3", "-c", REDIS_PROBE))

    def ready(self, nodes: list[str]) -> bool:
        # Pin a non-terminating Pod: exec deploy can pick the old Pod during rollout.
        pod = next(p for p in self.pods("app=akernel-adx-coordinator")
                   if not p["metadata"].get("deletionTimestamp"))
        raw = self.kube("exec", pod["metadata"]["name"], "-c", "coordinator", "--",
                        "curl", "-fsS", "http://127.0.0.1:19090/metrics")
        return all(any(line.startswith("adx_coordinator_node_schedulable{")
                       and f'node_id="{node}"' in line and line.rsplit(" ", 1)[-1] == "1"
                       for line in raw.splitlines()) for node in nodes)

    @staticmethod
    def wait(fn, timeout: int = 180):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                value = fn()
                if value:
                    return value
            except (RuntimeError, StopIteration, KeyError):
                pass
            time.sleep(2)
        raise AssertionError("acceptance condition timed out")

    def create(self, node: str) -> Environment:
        env = Environment(cpu=500, memory=1024, runtime="runsc", node_id=node, idle_timeout=1800,
                          connection=self.connection)
        self.environments.append(env)
        result = env.commands.run("printf lifecycle-alive")
        assert result.exit_code == 0 and result.stdout == "lifecycle-alive"
        return env

    def restart(self, expected_delta: int, baseline: dict, nodes: list[str]) -> None:
        old = next(p for p in self.pods("app=akernel-adx-coordinator")
                   if not p["metadata"].get("deletionTimestamp"))
        event = {"kind": "coordinator_restart", "old_pod": old["metadata"]["name"],
                 "baseline": baseline, "samples": []}
        self.report["events"].append(event)
        finalizers = old["metadata"].get("finalizers", [])
        assert self.finalizer not in finalizers
        self.kube("patch", "pod", event["old_pod"], "--type=merge", "-p", json.dumps({
            "metadata": {"finalizers": [*finalizers, self.finalizer]}}))
        self.finalized = event["old_pod"]
        started = time.monotonic()
        self.kube("delete", "pod", event["old_pod"], "--wait=false")
        def stopped():
            pod = json.loads(self.kube("get", "pod", event["old_pod"], "-o", "json"))
            containers = {c["name"]: c["state"].get("terminated")
                          for c in pod["status"].get("containerStatuses", [])}
            return containers if containers and all(containers.values()) else None
        event["termination"] = self.wait(stopped)
        event["stop_elapsed"] = time.monotonic() - started
        assert all(c["exitCode"] == 0 for c in event["termination"].values()), event["termination"]
        assert event["stop_elapsed"] < old["spec"]["terminationGracePeriodSeconds"]
        self.remove_finalizer()
        def replaced():
            pods = self.pods("app=akernel-adx-coordinator")
            return (all(p["metadata"]["uid"] != old["metadata"]["uid"] for p in pods)
                    and self.ready(nodes))
        self.wait(replaced)
        # Remain stable beyond the 30s recovery grace. Redis, not a cached metric,
        # must show no added incidents for healthy peers at any sampled boundary.
        for _ in range(9):
            now = self.probe()
            event["samples"].append(now)
            assert now["incidents"] - baseline["incidents"] == expected_delta
            assert all(now["nodes"][n]["session"]["id"] == baseline["nodes"][n]["session"]["id"] for n in nodes)
            assert self.ready(nodes)
            time.sleep(4)
        event["passed"] = True
        print(f"PASS Coordinator restart: incident delta {expected_delta}, all containers exited cleanly", flush=True)

    def resume(self) -> None:
        if self.paused:
            pod, pid = self.paused
            self.execute(pod, "python3", "-c",
                         "import os,signal,pathlib,sys; p=int(sys.argv[1]); "
                         "assert pathlib.Path('/proc',str(p),'comm').read_text().strip()=='adxctl'; "
                         "os.kill(p,signal.SIGCONT)", str(pid))
            self.paused = None

    def remove_finalizer(self) -> None:
        if self.finalized:
            pod = json.loads(self.kube("get", "pod", self.finalized, "-o", "json"))
            index = pod["metadata"]["finalizers"].index(self.finalizer)
            path = f"/metadata/finalizers/{index}"
            self.kube("patch", "pod", self.finalized, "--type=json", "-p", json.dumps([
                {"op": "test", "path": path, "value": self.finalizer},
                {"op": "remove", "path": path}]))
            self.finalized = None

    def remove_delay(self) -> None:
        if not self.delayed:
            return
        def still_running():
            pods = json.loads(self.kube("get", "pods", "--field-selector",
                                        "metadata.name=" + self.delayed, "-o", "json"))["items"]
            return any("running" in c["state"] for p in pods
                       for c in p["status"].get("containerStatuses", [])
                       if c["name"] == "akernel-node")
        # /run belongs to the old container, so termination already removed the
        # drop-in. Do not report a cleanup failure by exec'ing a dead container.
        if still_running():
            try:
                self.execute(self.delayed, "bash", "-ec", "rm -f /run/systemd/system/adx.service.d/lifecycle-acceptance.conf; systemctl daemon-reload")
            except RuntimeError:
                if still_running():
                    raise
        self.delayed = None

    def run(self) -> None:
        self.nodes = sorted(p["spec"]["nodeName"] for p in self.pods("app=node")
                            if not p["metadata"].get("deletionTimestamp"))
        assert len(self.nodes) >= 2 and self.args.node in self.nodes
        self.peer = next(n for n in self.nodes if n != self.args.node)
        self.wait(lambda: self.ready(self.nodes))
        before = self.probe()
        assert not any(not e["invalidated"] and e["state"] not in ("Deleted", "Failed")
                       for e in before["environments"].values()), "cluster has user Environments"
        secret = json.loads(self.kube("get", "secret", "akernel-adx-tls", "-o", "json"))
        token = base64.b64decode(secret["data"]["admin-key"]).decode().strip()
        self.connection = ConnectionConfig(server_address=self.args.server, token=token,
                                           use_tls=True, verify_tls=False,
                                           gateway_address=self.args.gateway, gateway_use_tls=False)
        peer_env = self.create(self.peer)
        self.restart(0, self.probe(), self.nodes)
        assert peer_env.commands.run("printf peer-alive").stdout == "peer-alive"

        pod = self.node_pod(self.args.node)["metadata"]["name"]
        baseline = self.probe()
        ids = json.loads(self.execute(pod, "python3", "-c", OFFLINE))
        self.paused = (pod, ids["supervisor"])
        def expired():
            state = self.probe()
            return state if state["nodes"][self.args.node]["retirement"] else None
        expired_state = self.wait(expired, 90)
        assert expired_state["incidents"] - baseline["incidents"] == 1
        since = expired_state["nodes"][self.args.node]["retirement"]["since_unix_seconds"]
        peers = [n for n in self.nodes if n != self.args.node]
        self.restart(1, baseline, peers)
        assert self.probe()["nodes"][self.args.node]["retirement"]["since_unix_seconds"] == since
        self.resume()
        self.wait(lambda: self.ready(self.nodes))
        assert self.probe()["nodes"][self.args.node]["session"]["id"] != baseline["nodes"][self.args.node]["session"]["id"]

        # Exercise the actual kubelet preStop and grace deadline, not an exec of
        # systemctl alone. A transient post-stop delay exceeds Kubernetes' former
        # 30s default without delaying admission closure or unregister.
        target_env = self.create(self.args.node)
        pod = self.node_pod(self.args.node)
        assert pod["spec"]["terminationGracePeriodSeconds"] >= 150
        name = pod["metadata"]["name"]
        dropin = "/run/systemd/system/adx.service.d/lifecycle-acceptance.conf"
        self.delayed = name
        self.execute(name, "python3", "-c", "import pathlib; p=pathlib.Path(" + repr(dropin) + "); p.parent.mkdir(parents=True,exist_ok=True); p.write_text('[Service]\\nExecStopPost=/bin/sleep 35\\n')")
        self.execute(name, "systemctl", "daemon-reload")
        finalizers = pod["metadata"].get("finalizers", [])
        assert self.finalizer not in finalizers
        self.kube("patch", "pod", name, "--type=merge", "-p", json.dumps({"metadata": {"finalizers": [*finalizers, self.finalizer]}}))
        self.finalized = name
        baseline = self.probe();boot = baseline["nodes"][self.args.node]["session"]["id"]
        event = {"kind": "node_pod_delete", "old_pod": name, "old_boot": boot, "baseline": baseline}
        event["init"] = json.loads(self.execute(name, "python3", "-c",
            "import json,pathlib; f=dict(l.split(':',1) for l in pathlib.Path('/proc/1/status').read_text().splitlines()); "
            "print(json.dumps({'comm':pathlib.Path('/proc/1/comm').read_text().strip(), "
            "'can_halt':bool(int(f['CapEff'].strip(),16)&(1<<22))}))"))
        assert event["init"]["comm"] == "systemd"
        self.report["events"].append(event)
        started = time.monotonic()
        self.kube("delete", "pod", name, "--wait=false")
        def terminated():
            old = json.loads(self.kube("get", "pod", name, "-o", "json"))
            status = next(c for c in old["status"].get("containerStatuses", []) if c["name"] == "akernel-node")
            if "service_stop" not in event and "running" in status["state"]:
                raw = self.execute(name, "systemctl", "show", "adx.service",
                                   "--property=SubState,Result,ExecMainCode,ExecMainStatus")
                service = dict(line.split("=", 1) for line in raw.splitlines())
                if service.get("SubState") in ("stop-post", "dead"):
                    event["service_stop"] = service
            return status["state"].get("terminated")
        termination = self.wait(terminated, 180)
        event.update(termination=termination, elapsed=time.monotonic()-started)
        # SIGRTMIN+3 asks systemd to halt. In a privileged PID namespace the
        # successful reboot(RB_HALT_SYSTEM) is reported as SIGINT (128+2), see
        # https://man7.org/linux/man-pages/man2/reboot.2.html. Qualify that status
        # with the service result, deadline and authenticated departure below.
        assert termination["exitCode"] == 0 or (termination["exitCode"] == 130
                                                and event["init"]["can_halt"]), termination
        service = event.get("service_stop", {})
        assert service.get("Result") == "success" and service.get("ExecMainCode") == "1" and service.get("ExecMainStatus") == "0", service
        assert 35 <= event["elapsed"] < pod["spec"]["terminationGracePeriodSeconds"]
        after = self.probe()
        assert [self.args.node, boot] in after["departed"]
        assert after["incidents"] == baseline["incidents"]
        # Planned departure retires execution ownership; the user's Environment
        # remains Failed until collection or explicit deletion, rather than being
        # silently converted into a user-requested Delete.
        retired = after["environments"].get(target_env.id)
        assert retired is None or (retired["invalidated"] and not retired["resources_held"]
                                   and retired["state"] in ("Failed", "Deleted")), retired
        event.update(after=after, passed=True)
        print("PASS Node Pod stop: service success, authenticated unregister, incident delta 0", flush=True)
        self.remove_finalizer();self.delayed = None
        self.wait(lambda: self.ready(self.nodes))
        assert self.probe()["nodes"][self.args.node]["session"]["id"] != boot
        assert peer_env.commands.run("printf peer-alive").stdout == "peer-alive"
        self.create(self.args.node)
        self.check_shared_storage()
        self.report["passed"] = True

    def check_shared_storage(self) -> None:
        # Local checkpoint profiles need no shared mount. Operators select the
        # mounted path explicitly for distributed_fs (including custom paths).
        if self.args.shared_storage_path is None:
            return
        for node in self.nodes:
            self.execute(self.node_pod(node)["metadata"]["name"], "python3", "-c",
                         "import os,sys; os.stat(sys.argv[1])", self.args.shared_storage_path)

    def cleanup(self) -> None:
        actions = [self.resume, self.remove_delay, self.remove_finalizer]
        for action in actions:
            try:
                action()
            except Exception as error:
                self.report["cleanup_errors"].append(type(error).__name__)
        for env in self.environments:
            try:
                env.kill()
            except Exception as error:
                self.report["cleanup_errors"].append(type(error).__name__)
        if hasattr(self, "nodes"):
            try:
                self.wait(lambda: self.ready(self.nodes))
            except Exception as error:
                self.report["cleanup_errors"].append(type(error).__name__)
        if self.report["cleanup_errors"]:
            self.report["passed"] = False
        self.args.output.write_text(json.dumps(self.report, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("kubeconfig", "namespace", "node", "server", "gateway"):
        parser.add_argument("--" + arg, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--shared-storage-path", help="Optional shared checkpoint mount to stat on every worker; omit for local storage")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    acceptance = Acceptance(args)
    try:
        acceptance.run()
    except Exception as error:
        acceptance.report["error"] = str(error)
        raise
    finally:
        acceptance.cleanup()
    assert acceptance.report["passed"], "lifecycle acceptance failed"
    print("PASS coordinator restart +0, node loss/restart +1, graceful Pod deletion +0; selected storage checks and peer workload healthy")


if __name__ == "__main__":
    main()
