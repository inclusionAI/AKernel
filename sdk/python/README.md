# AKernel Python SDK

`akernel-sdk` is the Python interface for creating and managing remote AKernel sandboxes. Applications use one stable API for commands, files, interactive PTYs, port forwarding, and reverse tunnels. The current implementation uses `openYuanrong` as its backend adapter; backend-specific handles and namespaces are not part of the public API.

## Navigation

- [Install and configure](#install-and-configure)
- [Create a sandbox](#create-a-sandbox)
- [Commands](#commands)
- [Filesystem](#filesystem)
- [Interactive PTYs](#interactive-ptys)
- [Port forwarding](#port-forwarding)
- [Reverse tunnels](#reverse-tunnels)
- [Rootfs and mounts](#rootfs-and-mounts)
- [Resources and lifecycle](#resources-and-lifecycle)
- [CLI](#cli)
- [Examples and tests](#examples-and-tests)

## Install and configure

AKernel SDK 0.1.0 requires Python 3.10 or newer.

```bash
pip install akernel-sdk
```

To install from a source checkout:

```bash
python -m pip install ./sdk/python
```

Configure the public AKernel entrypoint and a signed JWT token:

```bash
export AKERNEL_SERVER_ADDRESS="akernel.example.com"
export AKERNEL_TOKEN="<token>"
```

Address behavior is deterministic:

- A host or IP without a port uses HTTPS/WSS on 443 for the frontend and HTTP
  on 80 for public sandbox port URLs.
- `host:port` uses that port as a shared HTTPS/WSS endpoint.
- `AKERNEL_GATEWAY_ADDRESS` overrides the port-forwarding and exec gateway for
  standalone or custom topologies. An override without a scheme uses HTTP/WS.

The standalone launcher prints the Traefik container IP to use as
`AKERNEL_SERVER_ADDRESS`.

## Create a sandbox

```python
from akernel_sdk import Sandbox

with Sandbox(cpu=1000, memory=2048) as sandbox:
    result = sandbox.commands.run("printf hello")
    print(result.stdout)
```

The constructor accepts:

```python
Sandbox(
    image: str | None = None,
    rootfs: S3Config | None = None,
    runtime: str = "runsc",
    cpu: int = 1000,
    memory: int = 4096,
    cpu_limit: int = 0,
    mem_limit: int = 0,
    idle_timeout: int = 300,
    schedule_timeout: int = 30,
    env: dict[str, str] | None = None,
    name: str | None = None,
    cwd: str | None = None,
    port_forwardings: list[int] | None = None,
    mounts: list[Mount] | None = None,
    reverse_tunnel: HttpReverseTunnel | None = None,
    detached: bool = False,
    node_id: str | None = None,
)
```

`cpu` is measured in millicores and `memory` in MiB. A zero CPU or memory limit means the limit follows the corresponding request. A positive limit must not be smaller than its request.

## Sandbox runtimes

AKernel uses the gVisor `runsc` runtime when `runtime` is omitted. Callers may also select `runsc` explicitly or request Kata Containers:

```python
default_sandbox = Sandbox()
runsc_sandbox = Sandbox(runtime="runsc")
kata_sandbox = Sandbox(runtime="kata")
```

Kata requires at least one cluster node whose sandboxd instance successfully initialized the Kata runtime with a usable `/dev/kvm` device. Nodes without KVM remain available for runsc workloads and do not advertise Kata; when no eligible Kata node exists, the scheduler returns a no-resource error.

See [`examples/sandbox_runtime.py`](./examples/sandbox_runtime.py) for a runnable example.

## Commands

Run a foreground command:

```python
result = sandbox.commands.run(
    "printf $GREETING",
    envs={"GREETING": "hello"},
    cwd="/tmp",
    timeout=60,
)
print(result.stdout, result.stderr, result.exit_code)
```

Run and control a background command:

```python
handle = sandbox.commands.run("sleep 30", background=True)
print(handle.pid)

for process in sandbox.commands.list():
    print(process.pid, process.command, process.running)

handle.kill()
```

Enable stdin only when it is needed:

```python
handle = sandbox.commands.run("wc -l", background=True, stdin=True)
handle.send_stdin("one\ntwo\n")
handle.close_stdin()
result = handle.wait(timeout=15)
```

Foreground commands use one actor RPC with the configured timeout. Background commands return a handle whose `wait()` method also performs one actor RPC.

## Filesystem

```python
sandbox.files.write("/tmp/message.txt", "hello")
print(sandbox.files.read("/tmp/message.txt"))

sandbox.files.write("/tmp/data.bin", b"\x00\x01")
print(sandbox.files.read("/tmp/data.bin", format="bytes"))

for entry in sandbox.files.list("/tmp"):
    print(entry.path, entry.type, entry.size)

sandbox.files.make_dir("/workspace")
sandbox.files.rename("/tmp/message.txt", "/workspace/message.txt")
sandbox.files.remove("/workspace/message.txt")
```

Copy local files or directories through the frontend exec WebSocket:

```python
sandbox.files.copy_from_local("./project", "/workspace/project")
sandbox.files.copy_to_local("/workspace/result.json", "./result.json")
```

## Interactive PTYs

Use `sandbox.pty` for an interactive byte stream with stdin, streaming output, terminal resizing, and an exit status:

```python
import sys

from akernel_sdk import Sandbox


def write_output(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


with Sandbox() as sandbox:
    with sandbox.pty.create(on_data=write_output) as session:
        session.send_stdin(b"echo hello from PTY\n")
        session.resize(rows=40, cols=120)
        session.send_stdin(b"exit 7\n")
        print(session.wait())
```

PTY output remains bytes so the SDK does not guess the terminal encoding. Use `session.close_stdin()` to signal end-of-input while continuing to receive output. A session belongs to its WebSocket connection: closing it terminates the remote interactive process, and reconnecting to an existing session is not supported.

Use `sandbox.commands` instead when the caller needs separate stdout and stderr, a complete `CommandResult`, or a controllable background process. The former `Shell` API and its actor `bash_*` methods were removed before the v0.1.0 public API was released.

## Port forwarding

Declare each sandbox port at creation time:

```python
from akernel_sdk import Sandbox

with Sandbox(port_forwardings=[8080]) as sandbox:
    server = sandbox.commands.run(
        "python3 -m http.server 8080 --bind 0.0.0.0",
        background=True,
    )
    print(sandbox.get_port_url(8080))
    server.kill()
```

`get_port_url()` rejects undeclared ports. Pass `internal=True` only when a
deployment operator explicitly wants the direct Traefik address instead of the
public gateway.

## Reverse tunnels

A reverse tunnel lets sandbox code call an HTTP or HTTPS service reachable
from the machine running the SDK:

```python
from akernel_sdk import HttpReverseTunnel, Sandbox

tunnel = HttpReverseTunnel(
    target="https://service.example.com",
    reverse_port=8765,
    listen_port=8766,
    connect_timeout=60,
)

with Sandbox(reverse_tunnel=tunnel) as sandbox:
    result = sandbox.commands.run(
        f"curl {sandbox.reverse_tunnel.url}/health"
    )
```

`reverse_port` carries the WebSocket tunnel through Traefik. `listen_port` is
the loopback HTTP listener used inside the sandbox. Consequently,
`sandbox.reverse_tunnel.url` is always
`http://127.0.0.1:<listen_port>`, even when `target` uses HTTPS.

For an HTTPS target, the SDK-side tunnel client performs the TLS handshake and
certificate verification. The sandbox application talks only to its loopback
HTTP listener. AKernel 0.1.0 supports one HTTP/WebSocket reverse tunnel per
sandbox; it does not expose a general TCP tunnel.

## Rootfs and mounts

Use a public OCI image:

```python
with Sandbox(image="ubuntu:24.04") as sandbox:
    print(sandbox.commands.run("cat /etc/os-release").stdout)
```

Or use an object in S3-compatible storage as the rootfs:

```python
from akernel_sdk import S3Config, Sandbox

rootfs = S3Config(
    endpoint="https://s3.example.com",
    bucket="akernel-rootfs",
    object="ubuntu-24.04/rootfs.img",
    access_key="<optional>",
    secret_key="<optional>",
)

with Sandbox(rootfs=rootfs) as sandbox:
    print(sandbox.commands.run("cat /etc/os-release").stdout)
```

`image` and `rootfs` are mutually exclusive. The SDK generates the backend
wire representation; callers do not pass raw rootfs JSON or override the
runtime inside an S3 object.

The same `S3Config` type can be used as a read-only mount source:

```python
from akernel_sdk import Mount

mount = Mount(target="/models", type="erofs", s3_config=rootfs)
with Sandbox(mounts=[mount]) as sandbox:
    print(sandbox.commands.run("ls /models").stdout)
```

OCI images can also be mounted read-only:

```python
mount = Mount(target="/opt/tools", image_url="ubuntu:24.04")
```

## Resources and lifecycle

`resources()` returns stable `NodeInfo` values rather than backend objects:

```python
from akernel_sdk import resources

for node in resources():
    print(node.id, node.status, node.capacity, node.allocatable, node.labels)
```

Use the context manager for ordinary sandboxes. For a named detached sandbox,
explicitly delete it when it is no longer needed:

```python
sandbox = Sandbox(name="worker", detached=True)
sandbox.kill()             # closes local clients; remote sandbox remains
Sandbox.delete("worker")   # terminates the named remote sandbox
```

`sandbox.id` is the physical ID shown by `ak list`. `get_info()` returns a
`SandboxInfo` containing `id`, state, requested CPU and memory, and the OCI
image when one was configured.

## CLI

The `ak` CLI is installed with the SDK package:

```bash
ak resources
ak list
ak list --quiet
ak exec <sandbox-id>
ak exec <sandbox-id> -- /bin/sh
ak delete <sandbox-id> [<sandbox-id> ...]
```

It uses the same `AKERNEL_SERVER_ADDRESS` and `AKERNEL_TOKEN` environment as
the Python API.

## Examples and tests

Maintained examples are under [`examples/`](./examples):

- `basic_usage.py`
- `command_stdin.py`
- `custom_image.py`
- `named_sandbox.py`
- `pty.py`
- `port_forwarding.py`
- `reverse_tunnel.py`
- `s3_rootfs_and_mounts.py`

Run unit tests without a deployment:

```bash
PYTHONPATH=sdk/python \
  python -m unittest discover -s sdk/python/tests/unit -t sdk/python -v
```

Run the integration suite against a configured deployment:

```bash
export AKERNEL_RUN_INTEGRATION=1
PYTHONPATH=sdk/python \
  python -m unittest discover -s sdk/python/tests/integration -t sdk/python -v
```

Load and transfer benchmarks live under [`benchmarks/`](./benchmarks) and are
not part of the default test suite.

## Public value types

| Type | Fields |
|---|---|
| `CommandResult` | `stdout`, `stderr`, `exit_code` |
| `CommandInfo` | `pid`, `command`, `running` |
| `EntryInfo` | `name`, `path`, `type`, `size`, `permissions`, `modified_time` |
| `SandboxInfo` | `id`, `state`, `cpu`, `memory`, `image` |
| `NodeInfo` | `id`, `status`, `capacity`, `allocatable`, `labels` |
| `S3Config` | `endpoint`, `bucket`, `object`, optional credentials |
| `Mount` | `target`, one source, and `type` |
| `HttpReverseTunnel` | `target`, `reverse_port`, `listen_port`, `connect_timeout` |
