"""Opt-in managed DinD IPv4 network and original-process C/R regression.

Requires an isolated compatible runtime (Docker 28 runsc flags, managed upper,
reflink and durable sync). Never updates a deployment. Set
AKERNEL_RUN_COMPOSE_TESTS=1, AKERNEL_TEST_DIND_IMAGE and
AKERNEL_TEST_IMAGE_BRIDGE_BINARY. The caller supplies ordinary SDK connection
variables. AKERNEL_TEST_EGRESS_HOST defaults to 1.1.1.1:80 and must be reachable.
"""

import http.server
import json
import os
import shlex
import tempfile
import threading
import time
import unittest
from pathlib import Path

import yaml

from akernel_sdk import (
    ComposeProject,
    DockerRuntime,
    HttpReverseTunnel,
    Sandbox,
    ServiceProcessHandle,
)

_ENABLED = os.environ.get("AKERNEL_RUN_COMPOSE_TESTS") == "1"
_IMAGE = os.environ.get("AKERNEL_TEST_DIND_IMAGE", "")
_BRIDGE = os.environ.get("AKERNEL_TEST_IMAGE_BRIDGE_BINARY", "")
_SSE = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
_SERVER = """
import socket,threading
listener=socket.socket();listener.bind(('0.0.0.0',8000));listener.listen(64)
def echo(peer):
 with peer:
  while chunk:=peer.recv(65536):peer.sendall(chunk)
def udp():
 peer=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);peer.bind(('0.0.0.0',8001))
 while True:
  chunk,address=peer.recvfrom(65536);peer.sendto(chunk,address)
threading.Thread(target=udp,daemon=True).start()
while True:
 peer,_=listener.accept();threading.Thread(target=echo,args=(peer,),daemon=True).start()
"""
_PROBE = """
import socket,pathlib,time,os,json
peer=socket.create_connection(('api',8000),5);peer.settimeout(None);cookie=41
while True:
 if pathlib.Path('/tmp/mutate').exists():cookie=99
 peer.sendall(b'original');assert peer.recv(8)==b'original'
 value={'pid':os.getpid(),'cookie':cookie,'local':peer.getsockname(),
        'remote':peer.getpeername(),'time':time.time()}
 path=pathlib.Path('/tmp/witness.tmp');path.write_text(json.dumps(value))
 path.replace('/tmp/witness.json');time.sleep(.1)
"""


@unittest.skipUnless(_ENABLED and _IMAGE and _BRIDGE, "enable managed Compose tests")
class ComposeNetworkIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.environments = []
        self.snapshots = []
        self.addCleanup(self.cleanup_remote)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requests = []
        requests = self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(
                    (self.path, {"authorization": self.headers.get("Authorization")})
                )
                self.rfile.read(int(self.headers.get("content-length", "0")))
                if self.headers.get("x-api-key") != "trial-fixture":
                    self.send_error(401)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in (_SSE[:13], _SSE[13:]):
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.tunnel = HttpReverseTunnel(f"http://127.0.0.1:{server.server_port}")
        context = self.root / "context"
        context.mkdir()
        services = {
            name: {"image": "python:3.12-slim", "command": ["sleep", "infinity"]}
            for name in ("main", "loadgen", "standard", "private")
        }
        services["main"].update(
            networks={"default": {}, "custom": {"aliases": ["api"]}},
            ports=["127.0.0.1:18090:8000/tcp", "127.0.0.1:18091:8001/udp"],
        )
        services["loadgen"]["networks"] = ["custom"]
        services["private"]["networks"] = ["private"]
        self.compose = context / "compose.yaml"
        self.original = yaml.safe_dump(
            {
                "services": services,
                "networks": {
                    "custom": {"ipam": {"config": [{"subnet": "172.28.41.0/24"}]}},
                    "private": {"internal": True},
                },
            }
        )
        self.compose.write_text(self.original)
        self.project = ComposeProject.prepare(
            context,
            [self.compose],
            output_dir=self.root / "prepared",
            network="bridge",
            auto_images=True,
            runtime=DockerRuntime(bridge_binary=Path(_BRIDGE), writable_mb=2048),
        )
        self.options = self.project.sandbox_options()
        self.options["reverse_tunnel"] = self.tunnel
        self.sandbox = Sandbox(
            image=_IMAGE,
            cpu=2000,
            memory=4096,
            idle_timeout=1200,
            schedule_timeout=180,
            storage_mb=8192,
            **self.options,
        )
        self.environments.append(self.sandbox.id)
        self.project.start_runtime(self.sandbox)
        self.project.up(wait_timeout=60)
        for name in ("main", "private"):
            self.project.service(name).start_process(
                shlex.join(["python3", "-c", _SERVER]), shell="/bin/sh"
            )
        # Confirm the negative isolation test has a real listening target.
        self.assertEqual(self.python("private", self.echo("127.0.0.1")), "ok")
        self.process = self.project.service("loadgen").start_process(
            shlex.join(["python3", "-c", _PROBE]), shell="/bin/sh"
        )
        self.endpoint = self.project.service("main").expose_tunnel(self.tunnel)
        self.sandbox.commands.run(
            shlex.join(
                ["python3", "-c", _PROBE.replace("('api',8000)", "('127.0.0.1',18090)")]
            ),
            background=True,
        )

    def cleanup_remote(self):
        for identity in self.environments:
            Sandbox.delete(sandbox_id=identity)
        for identity in self.snapshots:
            Sandbox.delete_snapshot(identity)

    def python(self, service, script):
        result = self.project.service(service).exec(
            shlex.join(["python3", "-c", script]), timeout=15
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def echo(host):
        return (
            "import socket,json; "
            f"s=socket.create_connection(({host!r},8000),3); "
            "s.sendall(b'ok');print(json.dumps(s.recv(2).decode()))"
        )

    def witness(self):
        started = time.time()
        for _ in range(60):
            value = self.python(
                "loadgen",
                "import pathlib; "
                "p=pathlib.Path('/tmp/witness.json'); "
                "print(p.read_text() if p.exists() else 'null')",
            )
            if value and value.pop("time") > started:
                return value
            time.sleep(0.1)
        self.fail("Existing inner TCP connection did not resume")

    def outer_witness(self):
        started = time.time()
        for _ in range(60):
            result = self.sandbox.commands.run(
                shlex.join(
                    [
                        "python3",
                        "-c",
                        "import pathlib; p=pathlib.Path('/tmp/witness.json'); "
                        "print(p.read_text() if p.exists() else 'null')",
                    ]
                ),
                timeout=15,
                check=True,
            )
            value = json.loads(result.stdout)
            if value and value.pop("time") > started:
                return value
            time.sleep(0.1)
        self.fail("Existing published-port TCP connection did not resume")

    def check_network(self):
        self.assertEqual(self.python("loadgen", self.echo("api")), "ok")
        self.assertEqual(self.python("standard", self.echo("main")), "ok")
        result = self.sandbox.commands.run(
            shlex.join(
                [
                    "python3",
                    "-c",
                    """
import socket,json
s=socket.create_connection(('127.0.0.1',18090),3);s.sendall(b'tcp');a=s.recv(3)
u=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);u.settimeout(3)
u.sendto(b'udp',('127.0.0.1',18091))
print(json.dumps([a.decode(),u.recv(3).decode()]))
""",
                ]
            ),
            timeout=15,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), ["tcp", "udp"])
        for name in ("main", "private"):
            endpoint = self.project.service(name).expose_tunnel(self.tunnel)
            self.assertEqual(
                self.python(
                    name,
                    "import urllib.request,json; "
                    f"r=urllib.request.Request({endpoint!r}+'/v1/messages?trace=1',"
                    "data=b'{}',headers={'x-api-key':'trial-fixture',"
                    "'Authorization':'Bearer opaque'}); "
                    "print(json.dumps(urllib.request.urlopen(r,timeout=5).read().decode()))",
                ),
                _SSE.decode(),
            )
        host = os.environ.get("AKERNEL_TEST_EGRESS_HOST", "1.1.1.1")
        self.assertTrue(
            self.python(
                "loadgen",
                "import socket,json; "
                f"s=socket.create_connection(({host!r},80),3);"
                "print(json.dumps(bool(s.getsockname())));s.close()",
            )
        )
        inspect = self.project.service("private").identity()
        result = self.sandbox.commands.run(
            shlex.join(["docker", "inspect", inspect["container_id"]]), check=True
        )
        networks = json.loads(result.stdout)[0]["NetworkSettings"]["Networks"]
        address = next(iter(networks.values()))["IPAddress"]
        self.assertTrue(
            self.python(
                "main",
                "import socket,json\ntry:\n "
                f"s=socket.create_connection(({address!r},8000),1); print('false')"
                "\nexcept (OSError,TimeoutError): print('true')",
            )
        )
        # A different service must be rejected at TCP, even with valid model auth.
        self.assertTrue(
            self.python(
                "loadgen",
                "import socket,json,urllib.parse; "
                f"u=urllib.parse.urlsplit({self.endpoint!r}); "
                "s=socket.create_connection((u.hostname,u.port),3);s.settimeout(3)\n"
                "try:\n s.sendall(b'GET / HTTP/1.0\\r\\n\\r\\n'); "
                "print(json.dumps(s.recv(100)==b''))\n"
                "except ConnectionResetError: print('true')",
            )
        )

    def test_native_network_and_two_restores(self):
        self.project.service("private").expose_tunnel(self.tunnel)
        identities = {
            name: self.project.service(name).identity()
            for name in ("main", "loadgen", "standard", "private")
        }
        before = self.witness()
        published_before = self.outer_witness()
        self.assertEqual(before["cookie"], 41)
        self.check_network()
        for _ in range(2):
            snapshot = self.sandbox.create_snapshot()
            self.snapshots.append(snapshot.snapshot_id)
            self.project.service("loadgen").exec("touch /tmp/mutate")
            self.assertEqual(self.witness()["cookie"], 99)
            self.sandbox.kill()
            self.sandbox = Sandbox.from_snapshot(
                snapshot.snapshot_id,
                idle_timeout=1200,
                schedule_timeout=180,
                **self.options,
            )
            self.environments.append(self.sandbox.id)
            self.project = ComposeProject.attach(self.sandbox, self.project.descriptor)
            self.project.reconnect_network()
            self.process = ServiceProcessHandle.attach(
                self.project.service("loadgen"), self.process.descriptor
            )
            self.assertEqual(self.witness(), before)
            self.assertEqual(self.outer_witness(), published_before)
            self.assertEqual(
                {name: self.project.service(name).identity() for name in identities},
                identities,
            )
            self.check_network()
        self.assertEqual(self.compose.read_text(), self.original)
        self.assertTrue(
            all(
                path == "/v1/messages?trace=1"
                and headers.get("authorization") == "Bearer opaque"
                for path, headers in self.requests
            ),
            self.requests,
        )


if __name__ == "__main__":
    unittest.main()
