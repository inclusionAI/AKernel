#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in bounded TCP/conntrack checkpoint probe on an empty local standalone."""
import argparse
import importlib.metadata
import json
import os
import queue
from pathlib import Path
import subprocess
import threading
import time

PYTHON_IMAGE = 'docker.io/library/python:3.12-slim@sha256:6b25cbe6577518889d553cdd5a5da97d259348d8f6e45b527dd731caaba27a07'
DIND_IMAGE = 'docker.io/library/docker:27.5.1-dind@sha256:f649ef046008ca7f926a2571c32b0ac22e5c59eb61b959617f9acc2a4c638cf5'
CHECKPOINT = '''import http.client,json,socket
class UnixHTTP(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
  self.sock.settimeout(120)
  self.sock.connect('/run/akernel/execd.sock')
c=UnixHTTP('localhost',timeout=120)
c.request('POST','/checkpoint')
r=c.getresponse();body=r.read().decode();print(body,flush=True)
assert r.status==200,(r.status,body)
assert json.loads(body)['status']=='completed',body
'''
DIND_SETUP = '''set -eu
export PATH="/usr/local/sbin/.iptables-legacy:$PATH"
iptables --version
apk add --no-cache python3 curl iproute2 >/tmp/issue129-apk.log 2>&1
mkdir -p /var/lib/docker /tmp/docker-exec
nohup dockerd --host=unix:///var/run/docker.sock --storage-driver=vfs --data-root=/var/lib/docker --exec-root=/tmp/docker-exec --pidfile=/tmp/issue129-dockerd.pid --bip=172.30.0.1/24 --iptables=true --ip6tables=false --userland-proxy=true > /tmp/issue129-dockerd.log 2>&1 </dev/null &
for n in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info --format 'storage={{.Driver}}'
mkdir -p /tmp/issue129-inner/bin /tmp/issue129-inner/lib /tmp/issue129-inner/www
cp /bin/busybox /tmp/issue129-inner/bin/busybox
cp /lib/ld-musl-x86_64.so.1 /tmp/issue129-inner/lib/
printf 'issue129-inner-ok\\n' > /tmp/issue129-inner/www/index.html
tar -C /tmp/issue129-inner -cf - . | docker import - issue129-inner:local
docker run -d --name issue129-inner -p 18080:8080 --entrypoint /bin/busybox issue129-inner:local httpd -f -p 8080 -h /www
curl --fail --max-time 10 http://172.30.0.1:18080/
iptables-save -t nat
docker inspect issue129-inner --format 'inner_pid={{.State.Pid}}'
readlink /proc/self/ns/net
inner_pid=$(docker inspect issue129-inner --format '{{.State.Pid}}')
readlink "/proc/${inner_pid}/ns/net"
'''
NETNS_SETUP = '''set -eu
export PATH="/usr/local/sbin/.iptables-legacy:$PATH"
apk add --no-cache python3 curl iproute2 >/tmp/issue129-apk.log 2>&1
iptables --version
ip netns add issue129
ip link add br129 type bridge
ip addr add 172.30.0.1/24 dev br129
ip link set br129 up
ip link add veth129 type veth peer name peer129
ip link set veth129 master br129
ip link set veth129 up
ip link set peer129 netns issue129
ip -n issue129 addr add 172.30.0.2/24 dev peer129
ip -n issue129 link set lo up
ip -n issue129 link set peer129 up
ip -n issue129 route add default via 172.30.0.1
nohup ip netns exec issue129 python3 /tmp/issue129-server.py > /tmp/issue129-server.log 2>&1 </dev/null &
iptables -t nat -N ISSUE129
iptables -t nat -A OUTPUT -d 172.30.0.1/32 -p tcp --dport 18080 -j ISSUE129
iptables -t nat -A ISSUE129 -p tcp --dport 18080 -j DNAT --to-destination 172.30.0.2:8080
iptables -t nat -A POSTROUTING -d 172.30.0.2/32 -p tcp --dport 8080 -j MASQUERADE
sleep 2
curl --interface 172.30.0.1 --fail --max-time 10 http://172.30.0.1:18080/
iptables -t nat -D OUTPUT -d 172.30.0.1/32 -p tcp --dport 18080 -j ISSUE129
if curl --noproxy '*' --fail --max-time 3 http://172.30.0.1:18080/; then
 echo 'NAT negative control unexpectedly succeeded' >&2; exit 1
fi
iptables -t nat -A OUTPUT -d 172.30.0.1/32 -p tcp --dport 18080 -j ISSUE129
curl --noproxy '*' --fail --max-time 10 http://172.30.0.1:18080/
echo 'NAT negative control passed: reachable only while DNAT rule is installed'
iptables-save -t nat
readlink /proc/self/ns/net
ip netns exec issue129 readlink /proc/self/ns/net
'''
NETNS_SERVER = '''from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path=='/active-close':self.rfile.read()
  body=b'issue129-nat-ok\\n';self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
'''
TRAFFIC = '''import json,os,socket,threading,time
target=('172.30.0.1',18080)
idle=[]
for _ in range(32):
 s=socket.create_connection(target,5);s.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
 for k,v in [(socket.TCP_KEEPIDLE,1),(socket.TCP_KEEPINTVL,1),(socket.TCP_KEEPCNT,10)]:s.setsockopt(socket.IPPROTO_TCP,k,v)
 idle.append(s)
progress={'requests':0,'errors':0,'keepalive_sockets':len(idle),'pid':os.getpid()}
retained=[]
def churn():
 while True:
  try:
   s=socket.create_connection(target,2)
   s.sendall(b'GET /active-close HTTP/1.0\\r\\nHost: inner\\r\\n\\r\\n');s.shutdown(socket.SHUT_WR)
   while s.recv(4096):pass
   retained.append(s)
   if len(retained)>256:retained.pop(0).close()
   progress['requests']+=1
  except OSError:progress['errors']+=1
  time.sleep(.05)
threading.Thread(target=churn,daemon=True).start()
for tick in range(600):
 progress['tick']=tick
 with open('/proc/net/tcp') as f:progress['root_time_wait']=sum(l.split()[3]=='06' for l in f.readlines()[1:])
 with open('/tmp/issue129-progress.new','w') as f:json.dump(progress,f)
 os.replace('/tmp/issue129-progress.new','/tmp/issue129-progress.json');time.sleep(1)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["baseline", "dind", "netns"], required=True)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--restores", type=int, default=2)
    parser.add_argument("--veth-path", choices=["move", "direct"], default="move")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--container", default="akernel-node")
    args = parser.parse_args()
    if not 1 <= args.attempts <= 8:
        parser.error("--attempts must be between 1 and 8")
    if not 1 <= args.restores <= 2:
        parser.error("--restores must be between 1 and 2")

    data = args.data_dir.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    sdk_version = importlib.metadata.version("akernel-sdk")
    node = json.loads(subprocess.check_output(["docker", "inspect", args.container]))[0]
    mounts = {mount["Destination"]: mount["Source"] for mount in node["Mounts"]}
    if mounts.get("/home/akernel") != str(data):
        raise RuntimeError("Container /home/akernel does not match --data-dir")
    node_id = node["Id"]

    def endpoint(port, scheme):
        bindings = node["NetworkSettings"]["Ports"].get(f"{port}/tcp") or []
        if not bindings:
            raise RuntimeError(f"Container port {port} is not published")
        binding = bindings[0]
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(
            binding["HostIp"], binding["HostIp"]
        )
        if ":" in host:
            host = f"[{host}]"
        return f"{scheme}://{host}:{binding['HostPort']}"

    os.environ["AKERNEL_SERVER_ADDRESS"] = endpoint(8443, "https")
    os.environ["AKERNEL_GATEWAY_ADDRESS"] = endpoint(8080, "http")
    os.environ["AKERNEL_TOKEN"] = (data / "token").read_text().strip()
    os.environ["AKERNEL_VERIFY_TLS"] = "1"
    os.environ["SSL_CERT_FILE"] = str(data / "adx/tls/ingress-public.pem")
    from akernel_sdk import Sandbox

    runtime = [
        "docker", "exec", node_id, "runsc",
        "--root=/home/akernel/sandboxd/root/runsc",
    ]
    inventory = json.loads(subprocess.check_output(
        runtime + ["list", "--format=json"], timeout=15,
    ))
    if inventory:
        raise RuntimeError("Probe requires an empty dedicated runsc backend")

    out = args.output.resolve() / (
        args.mode + "-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    out.mkdir()
    result = {
        "mode": args.mode, "sdk": sdk_version, "node_id": node_id,
        "image_id": node["Image"], "veth_path": args.veth_path,
        "attempts": [], "restores": [],
    }

    def save():
        (out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")

    def run(sandbox, command, timeout=60):
        response = sandbox.commands.run(command, timeout=timeout)
        if response.exit_code != 0:
            raise RuntimeError((
                command, response.exit_code,
                response.stdout[-1500:], response.stderr[-1500:],
            ))
        return response.stdout

    def wait_for_traffic(sandbox, previous):
        deadline = time.monotonic() + 5
        while True:
            current = json.loads(run(sandbox, "cat /tmp/issue129-progress.json", timeout=5))
            if current["requests"] > previous["requests"]:
                return current
            if time.monotonic() >= deadline:
                raise RuntimeError("HTTP traffic did not progress within the observation budget")
            time.sleep(0.25)

    def capture(label):
        listing = subprocess.run(
            runtime + ["list", "--format=json"],
            capture_output=True, text=True, timeout=15,
        )
        (out / f"{label}-runsc-list.json").write_text(listing.stdout)
        for state in json.loads(listing.stdout or "[]"):
            sandbox_id = state["id"]
            debug = subprocess.run(
                runtime + ["debug", "--stacks", sandbox_id],
                capture_output=True, text=True, timeout=20,
            )
            (out / f"{label}-{sandbox_id}-stacks.log").write_text(
                debug.stdout + debug.stderr
            )

    sandbox = None
    try:
        sandbox = Sandbox(
            image=PYTHON_IMAGE if args.mode == "baseline" else DIND_IMAGE,
            runtime="runsc", cpu=2000, memory=2048, storage_mb=2048,
            idle_timeout=900, failover=True,
            create_timeout=90 if args.mode == "baseline" else 360,
        )
        result["sandbox_id"] = sandbox.id
        save()
        print("sandbox created", sandbox.id, flush=True)
        if args.mode != "baseline":
            sandbox.files.write("/tmp/issue129-server.py", NETNS_SERVER)
            setup_command = DIND_SETUP if args.mode == "dind" else NETNS_SETUP
            if args.mode == "netns" and args.veth_path == "direct":
                setup_command = setup_command.replace(
                    "ip link add veth129 type veth peer name peer129\n",
                    "ip link add veth129 type veth peer name peer129 netns issue129\n",
                ).replace("ip link set peer129 netns issue129\n", "")
            setup = sandbox.commands.run(
                setup_command, timeout=240,
            )
            (out / "setup.log").write_text(setup.stdout + setup.stderr)
            if setup.exit_code != 0:
                diagnostic = sandbox.commands.run(
                    "tail -80 /tmp/issue129-dockerd.log /tmp/issue129-apk.log 2>/dev/null",
                    timeout=30,
                )
                (out / "setup-diagnostic.log").write_text(
                    diagnostic.stdout + diagnostic.stderr
                )
                raise RuntimeError(
                    f"{args.mode} fixture setup failed: exit={setup.exit_code}; see setup logs"
                )
            sandbox.files.write("/tmp/issue129-traffic.py", TRAFFIC)
            run(sandbox, "nohup python3 /tmp/issue129-traffic.py "
                "> /tmp/issue129-traffic.log 2>&1 </dev/null &")
            time.sleep(10)
            result["initial_traffic"] = json.loads(run(
                sandbox, "cat /tmp/issue129-progress.json",
            ))
            if result["initial_traffic"]["root_time_wait"] <= 0:
                raise RuntimeError("No root TIME_WAIT observed")
            chain = "DOCKER" if args.mode == "dind" else "ISSUE129"
            result["initial_nat_counters"] = run(
                sandbox, f"/usr/sbin/iptables-legacy -t nat -nvxL {chain}",
            )
            print("traffic ready", result["initial_traffic"], flush=True)
            source_traffic = result["initial_traffic"]

        sandbox.files.write("/tmp/issue129-checkpoint.py", CHECKPOINT)
        for attempt in range(args.attempts):
            run(sandbox, "printf before > /tmp/issue129-rollback-state")
            start = time.monotonic()
            handle = sandbox.commands.run(
                "python3 /tmp/issue129-checkpoint.py", background=True,
            )
            item = {
                "attempt": attempt + 1, "command_id": handle.command_id,
                "command_pid": handle.pid,
            }
            result["attempts"].append(item)
            save()
            outcome = queue.Queue()

            def observe():
                try:
                    outcome.put((True, handle.wait(120)))
                except BaseException as error:
                    outcome.put((False, error))

            threading.Thread(target=observe, daemon=True).start()
            observed = None
            for elapsed in range(0, 125, 5):
                try:
                    observed = outcome.get(timeout=5)
                    break
                except queue.Empty:
                    pass
                if elapsed in (15, 45, 90):
                    capture(f"attempt-{attempt + 1}-{elapsed}s")
            if observed is None:
                item["observation"] = (
                    "observation guard expired; remote command outcome unresolved"
                )
                save()
                raise TimeoutError(
                    "No completed result; retained stable command identity; no retry or restore"
                )
            ok, response = observed
            if not ok:
                raise response
            item.update(
                seconds=round(time.monotonic() - start, 3),
                exit_code=response.exit_code,
                stdout=response.stdout, stderr=response.stderr,
            )
            save()
            print("checkpoint result", item, flush=True)
            if response.exit_code != 0 or '"completed"' not in response.stdout:
                capture(f"attempt-{attempt + 1}-unfinished")
                raise RuntimeError(
                    "Checkpoint did not explicitly complete; no restore or duplicate request submitted"
                )
            run(sandbox, "printf after > /tmp/issue129-rollback-state")
            if args.mode != "baseline":
                source_traffic = wait_for_traffic(sandbox, source_traffic)
                item["source_traffic"] = source_traffic
            save()
            time.sleep(0.25 + 0.15 * (attempt % 3))

        for restore_number in range(1, args.restores + 1):
            restore_result = {"restore": restore_number}
            result["restores"].append(restore_result)
            save()
            if not sandbox.reload():
                raise RuntimeError("Reload did not succeed")
            if run(sandbox, "cat /tmp/issue129-rollback-state") != "before":
                raise RuntimeError("Reload did not restore checkpointed file contents")
            restore_result["file_rollback_passed"] = True
            if args.mode != "baseline":
                restored = sandbox.commands.run(
                    "curl --noproxy '*' --fail --max-time 10 http://172.30.0.1:18080/",
                    timeout=20,
                )
                restore_result["http"] = {
                    "exit_code": restored.exit_code,
                    "stdout": restored.stdout, "stderr": restored.stderr,
                }
                save()
                if restored.exit_code != 0:
                    diagnostic = sandbox.commands.run(
                        "ip addr; ip route; /usr/sbin/iptables-legacy -t nat -S; "
                        "ip netns exec issue129 ip addr; "
                        "tail -30 /tmp/issue129-server.log /tmp/issue129-traffic.log",
                        timeout=30,
                    )
                    (out / f"reload-{restore_number}-diagnostic.log").write_text(
                        diagnostic.stdout + diagnostic.stderr
                    )
                    raise RuntimeError("Restored NAT HTTP request failed; see reload diagnostic")
                expected_body = "issue129-inner-ok" if args.mode == "dind" else "issue129-nat-ok"
                if expected_body not in restored.stdout:
                    raise RuntimeError("Restored HTTP body missing")
                if args.mode == "netns":
                    addresses = json.loads(run(sandbox, "ip -j -n issue129 addr show dev peer129"))
                    routes = json.loads(run(sandbox, "ip -j -n issue129 route show"))
                    restore_result["topology"] = {"addresses": addresses, "routes": routes}
                    has_address = any(
                        address.get("local") == "172.30.0.2"
                        for interface in addresses if interface.get("ifname") == "peer129"
                        for address in interface.get("addr_info", [])
                    )
                    has_route = any(
                        route.get("dst") == "default"
                        and route.get("gateway") == "172.30.0.1"
                        and route.get("dev") == "peer129" for route in routes
                    )
                    if not has_address or not has_route:
                        raise RuntimeError("Restored child peer address or route is missing")
                before = json.loads(run(sandbox, "cat /tmp/issue129-progress.json"))
                after = wait_for_traffic(sandbox, before)
                restore_result.update(traffic_before=before, traffic_after=after)
                save()
            run(sandbox, f"printf after-restore-{restore_number} > /tmp/issue129-rollback-state")
            save()
            print("restore result", restore_result, flush=True)
        result["reload_rollback_passed"] = True
        result["status"] = "passed"
        save()
    except BaseException as error:
        result["status"] = "failed"
        result["error"] = str(error)
        for key in ("environment_id", "sandbox_id", "cleanup_errors"):
            if hasattr(error, key):
                result[key] = str(getattr(error, key))
        save()
        raise
    finally:
        if sandbox is not None:
            try:
                sandbox.kill()
                result["sdk_cleanup"] = "kill returned"
            except Exception as error:
                result["sdk_cleanup_error"] = str(error)
            save()
        print("evidence", out, flush=True)


if __name__ == "__main__":
    main()
