# AKernel Python SDK

`akernel-sdk` is the Python interface for creating and managing remote AKernel
sandboxes. Applications use one stable API for commands, files, interactive
PTYs, port forwarding, and reverse tunnels.

It supports two backends:

- `openyuanrong-sandbox` (default), using a RESTful API and Rust runtime.
- `openyuanrong-sdk` (legacy), using YuanRong actors and a Python runtime.

## Navigation

- [AKernel Python SDK](#akernel-python-sdk)
  - [Navigation](#navigation)
  - [Install and configure](#install-and-configure)
  - [Create a sandbox](#create-a-sandbox)
    - [Experimental GPU and writable storage](#experimental-gpu-and-writable-storage)
    - [Network ACLs](#network-acls)
  - [Sandbox runtimes](#sandbox-runtimes)
  - [Commands](#commands)
  - [Filesystem](#filesystem)
  - [Interactive PTYs](#interactive-ptys)
  - [Port forwarding](#port-forwarding)
  - [Reverse tunnels](#reverse-tunnels)
  - [Rootfs and mounts](#rootfs-and-mounts)
  - [Launch from a Dockerfile](#launch-from-a-dockerfile)
  - [Resources and lifecycle](#resources-and-lifecycle)
  - [CLI](#cli)
  - [Examples and tests](#examples-and-tests)
  - [Public value types](#public-value-types)

## Install and configure

AKernel SDK requires Python 3.10 or newer.

```bash
pip install akernel-sdk
```

To install from source:

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

The legacy actor backend is optional. Install and select it before importing
`akernel_sdk`:

```bash
pip install "akernel-sdk[openyuanrong-sdk]"
export AKERNEL_BACKEND=openyuanrong-sdk
```

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
    *,
    xpu: str | None = None,
    storage_mb: int | None = None,
    network_policy: NetworkPolicy | None = None,
    dockerfile: DockerfileLaunch | None = None,
    extra_config: Mapping[str, object] | None = None,
)
```

### Experimental GPU and writable storage

Request a whole NVIDIA GPU by type, exact product model, and count:

```python
with Sandbox(xpu="gpu:l20:1") as sandbox:
    print(sandbox.commands.run("nvidia-smi -L").stdout)
```

The `type:model:count` value is case-insensitive and canonicalized to lower
case. The model is required and matched exactly; wildcard models are not
supported. The bundled backend currently requires the gVisor `runsc` runtime
and a node configured for gVisor nvproxy. Runtime compatibility is validated
by the backend rather than the SDK.

Set the writable root filesystem quota in MiB:

```python
with Sandbox(storage_mb=20 * 1024) as sandbox:
    print(sandbox.commands.run("df -h /").stdout)
```

The bundled backend currently requires `runsc` for an explicit `storage_mb`
quota and uses sandboxd's disk-backed XFS filestore. Runtime compatibility is
validated by the backend. When `storage_mb` is omitted, sandboxd retains its
configured default 10 GiB memory-backed writable overlay. See
[`examples/gpu_sandbox.py`](./examples/gpu_sandbox.py) and
[`examples/storage_sandbox.py`](./examples/storage_sandbox.py).

### Network ACLs

Omit `network_policy` to leave all sandbox networking unrestricted. An empty
`NetworkPolicy()` is equivalent and is omitted from the creation request:

```python
from akernel_sdk import NetworkPolicy, Sandbox

with Sandbox() as unrestricted:
    print(unrestricted.commands.run("python3 -c 'import socket; "
                                    "socket.getaddrinfo(\"github.com\", 443)'"))
```

Block new sandbox flows except the YuanRong control proxy and published
sandbox-port routes:

```python
with Sandbox(network_policy=NetworkPolicy.block()) as sandbox:
    result = sandbox.commands.run("printf 'control plane still works'")
    assert result.exit_code == 0
```

Commands and lifecycle operations continue to work in block mode. The policy
also publishes the sandbox targets required by direct filesystem I/O, reverse
tunnels, and explicit `port_forwardings`. Replies on those allowed paths are
stateful. Other new network flows remain denied.

Deny conventional DNS lookups for exact names or leading `*.` suffix
patterns:

```python
policy = NetworkPolicy.deny_dns("github.com", "*.github.com")
with Sandbox(network_policy=policy) as sandbox:
    blocked = sandbox.commands.run(
        "python3 -c 'import socket; socket.getaddrinfo(\"github.com\", 443)'"
    )
    assert blocked.exit_code != 0
```

An exact pattern matches only that name. For example, `github.com` does not
match `api.github.com`, while `*.github.com` matches descendants but not
the apex. Supply both when both should be denied. Patterns are normalized to
lower case without a trailing dot; international names must use ASCII
punycode. Each pattern may be an exact ASCII name or begin with one leading
`*.`. The remaining name is at most 253 characters; each dot-separated label
is 1-63 letters, digits, underscores, or hyphens, and a hyphen cannot start or
end a label. Other wildcard placements and `?` are rejected.

Network policies are fixed when a sandbox is created. `block_network` and
`dns_blacklist` cannot be combined in the current SDK. DNS blacklists cover
ordinary UDP and TCP DNS and return a refused response for blocked queries;
DNS-over-HTTPS and connections to a known IP are outside their scope. The
block-network packet ACL is currently stateful IPv4. DNS-only policies do not
install a general packet allowlist.

See [`examples/network_policy.py`](./examples/network_policy.py) for all
three modes. Deployment nodes must have network ACL support enabled; the
bundled standalone, Helm, and Terraform configurations enable it. Drain
existing sandboxes before upgrading a node to an ACL-enabled sandboxd
configuration, as described in the
[deployment guide](../../deploy/README.md#network-acls).

## Sandbox runtimes

AKernel uses the gVisor `runsc` runtime when `runtime` is omitted. Runtime
identifiers and optional `extra_config` are forwarded to the selected backend
instead of being restricted or interpreted by an SDK-owned registry. Values in
`extra_config` must be JSON-compatible. The bundled deployment advertises
`runsc`, Kata Containers, and Firecracker by default when their host
prerequisites are available.

Kata and Firecracker require at least one cluster node whose sandboxd instance
successfully initialized the requested runtime with a usable `/dev/kvm`
device. Nodes without KVM remain available for runsc workloads and do not
advertise either VM runtime. Firecracker accepts EROFS image roots and mounts
and rejects OCI/Nydus directories, directory mounts, GPUs, and nested KVM.

The all-in-one image can optionally package a native Linux `runc` backend.
Operators build it with `AKERNEL_ENABLE_RUNC=true` and enable it explicitly
because it provides host-kernel container isolation, not the user-space kernel
boundary of runsc. Guided cloud profiles use
`make config ENABLE_RUNC=true`; standalone deployments use
`AKERNEL_ENABLE_RUNC=true`, and direct Helm deployments use
`node.config.sandboxd.enableRunc=true`. After it is advertised:

```python
with Sandbox(runtime="runc") as sandbox:
    print(sandbox.commands.run("uname -s").stdout)

with Sandbox(
    runtime="runc",
    extra_config={"enableKVM": True},
) as sandbox_with_kvm:
    print(sandbox_with_kvm.commands.run("test -c /dev/kvm").exit_code)
```

`enableKVM` is owned by the runc backend and requires a usable `/dev/kvm` on
the selected node. Runc supports OCI/EROFS root filesystems, read-only mounts,
networking, command execution, and the default writable overlay. Experimental
GPU requests remain runsc-only; explicit `storage_mb` quotas are supported by
runsc and Firecracker. See the
[sandbox runtime comparison](../../src/sandboxd/doc/runtime.md) for the
runtime capability boundaries.

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

Foreground commands return a backend-neutral `CommandResult`. Background
commands return an AKernel `CommandHandle`; its lifecycle operations are
delegated to the selected backend.

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
HTTP listener. AKernel supports one HTTP/HTTPS reverse tunnel per sandbox and
does not expose a general TCP tunnel.

The default `openyuanrong-sandbox` backend supports custom internal tunnel
ports. Its frontend derives the WebSocket port from the HTTP listener, so
`reverse_port` must equal `listen_port - 1`. Both ports are reserved inside
that sandbox while the tunnel is active and must not also appear in
`port_forwardings`; they do not occupy ports on the SDK host.

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
runtime inside an S3 object. When neither source is supplied, AKernel sends
only the selected isolation runtime and openYuanRong overlays it onto the
rootfs configured by the deployed service.

For Firecracker, the deployed default or an explicit `rootfs` object must be a
raw EROFS image. `image="ubuntu:24.04"` produces an OCI/Nydus directory and is
therefore supported by runsc, runc, and Kata but rejected by Firecracker.

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

Firecracker mounts must instead use `type="erofs"` with an S3 object containing
a raw EROFS image; it rejects `image_url` and directory-backed mounts.

## Launch from a Dockerfile

Dockerfile direct launch is a supported AKernel SDK capability and will remain
available. Its documented strict subset evolves incrementally with production
experience; unsupported inputs continue to fail closed. The specific API surface
may evolve, with documentation and migration guidance for material changes. It
is not a general-purpose Docker build.

`FROM` supplies only the root filesystem; inherited OCI configuration is not
applied. Precheck the context, then pass its launch configuration to `Sandbox`:

```python
from akernel_sdk import DockerfileLaunch, LocalDockerContext, Sandbox, check_direct_launch
context = LocalDockerContext("Dockerfile", context_dir=".")
if check_direct_launch(context).direct_launchable:
    with Sandbox(dockerfile=DockerfileLaunch(context, run_timeout=300)) as sandbox:
        pass
```

`RUN`, `COPY`, and `ADD` run on every launch without a snapshot or cache;
unsupported Dockerfiles must be built externally. Read the complete contract,
security boundaries, and supported syntax in
[the Dockerfile launch guide](./docs/launch-from-dockerfile.md). See the
[runnable example](./examples/dockerfile_launch.py).

## Resources and lifecycle

`resources()` returns stable `NodeInfo` values rather than backend objects:

```python
from akernel_sdk import resources

for node in resources():
    print(node.id, node.status, node.capacity, node.allocatable, node.labels)
```

Accelerators appear under keys such as `GPU/l20`. Capacity is the total card
count and allocatable is the currently free count. `ak resources` renders the
same information as, for example, `gpu/l20 1/4`.

Use the context manager for ordinary sandboxes. For a named detached sandbox,
explicitly delete it when it is no longer needed:

```python
sandbox = Sandbox(name="worker", detached=True)
sandbox.kill()             # closes local clients; remote sandbox remains
Sandbox.delete("worker")   # terminates the named remote sandbox
```

`sandbox.id` is the physical ID shown by `ak list`. `get_info()` returns a
`SandboxInfo` containing `id`, state, requested CPU, memory, XPU and storage,
and the OCI image when one was configured.

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
- `dockerfile_launch.py`
- `gpu_sandbox.py`
- `named_sandbox.py`
- `network_policy.py`
- `pty.py`
- `port_forwarding.py`
- `reverse_tunnel.py`
- `s3_rootfs_and_mounts.py`
- `storage_sandbox.py`

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
| `SandboxInfo` | `id`, `state`, `cpu`, `memory`, `image`, `xpu`, `storage_mb` |
| `NodeInfo` | `id`, `status`, `capacity`, `allocatable`, `labels` |
| `S3Config` | `endpoint`, `bucket`, `object`, optional credentials |
| `Mount` | `target`, one source, and `type` |
| `HttpReverseTunnel` | `target`, `reverse_port`, `listen_port`, `connect_timeout` |
| `NetworkPolicy` | `block_network`, `dns_blacklist` |
| `DockerfileLaunch` | `context`, `auto_start_cmd`, `run_timeout` |
| `DockerContext` | Abstract Dockerfile and build-context source |
| `DockerContextEntry` | `path`, `kind`, `mode` |
| `LocalDockerContext` | Local Dockerfile and context implementation |
