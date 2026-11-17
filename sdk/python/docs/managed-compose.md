# Managed Compose integration

The managed runtime runs Docker and Compose inside one outer AKernel sandbox.
Applications submit their original Dockerfiles, build contexts and Compose files.
The operator supplies build connectivity, tools, Registry authorization and shared
read-only image layers. The SDK exposes project, service and process handles.
This opt-in Linux/amd64 runsc profile is separate from the SDK's strict Dockerfile
interpreter.

## Component responsibilities

| Component | Responsibility |
| --- | --- |
| Application or evaluation framework | Task resources, environment variables, bind paths, verifier isolation, checkpoint timing and client-side state. Fence the source before restoring a running clone. |
| AKernel SDK | Freeze and transfer inputs, submit runtime operations, expose artifact/project/service/process handles and reconnect client tunnels. |
| Managed runtime | Run official Docker Compose and Buildx remote, publish immutable image references, prepare authorized ROFS collections and start Docker/containerd with private writable layers. |
| Agent DX | Environment creation, command/file/tunnel transport and reusable snapshot control. |
| Sandboxd and the image manager | Authorize profiles, enforce storage quotas, distribute shared ROFS content and retain live/checkpoint collection leases. |
| runsc | Capture the complete outer sandbox, including Docker, services, memory, private filesystem state and sockets. |

BuildKit owns build-step caching. Dragonfly and the existing image manager own
final image distribution and ROFS caching. Each sandbox has a private writable
upper; the SDK does not maintain a second shared image-layer cache.

## Operator prerequisites

1. Configure a managed-runtime profile with an immutable tool image, a reachable
   remote BuildKit endpoint, an internal Registry and authorized tenants. Build
   workers need Registry push access; runtime nodes need pull access. Harbor
   clients need neither Registry credentials nor Kubernetes access.
2. Enable runsc file-backed writable mounts, reflink checkpoint mode and durable
   output synchronization on a compatible filesystem. On Linux 5.10, backing
   files and checkpoint clones must use the same mount object. Collection leases
   and checkpoint output must share a filesystem. The SDK cannot enable these
   features through a snapshot option.
3. Configure Docker 28's runsc flags `--net-raw=true` and
   `--allow-packet-socket-write=true`. Package Docker, Compose, Buildx, containerd,
   the [image bridge](../../../builder/image-bridge/README.md) and support tools
   in the operator's runtime image. User task images do not need these tools.
4. Publish the profile's storage floor through authenticated worker metadata.
   See the [operator guide](../../../builder/managed-docker/README.md) for
   configuration, service installation and storage-label generation.

The SDK derives the storage floor from that worker metadata before allocation.
For example, a 4096 MiB image upper plus a 4096 MiB root reservation requires
8192 MiB total. Omitted storage uses the advertised floor; explicit requests or
limits below it fail before creation. Sandboxd checks the actual quota again.

## Fresh creation

Select the operator-provided profile:

```python
from akernel_sdk import ComposeProject, DockerRuntime, Sandbox

project = ComposeProject.prepare(
    "./project", ["./project/docker-compose.yaml"],
    runtime=DockerRuntime(profile="harbor-v1"),
    network="bridge", output_dir="./artifacts/compose",
)
sandbox = Sandbox(**project.sandbox_options())
try:
    project.start_runtime(sandbox)
    project.build(timeout=600)
    project.pull(timeout=600)
    project.up(wait_timeout=600)
    print(project.service("main").exec("uname -s").stdout)
finally:
    sandbox.kill()
```

Keep `output_dir` outside the input context. The SDK freezes inputs without
parsing managed YAML; official Compose resolves services after `start_runtime()`,
including native `!reset` and `!override` tags. Service handles become available
at that point. BuildKit receives the original Dockerfile and context through
Buildx remote. Normal build caches remain enabled unless `no_cache=True` is
explicitly requested.

The runtime publishes digest-addressed images and prepares their ROFS roots.
The image bridge registers validated image aliases and committed snapshots in
inner containerd. Missing registrations fail instead of extracting replacement
lower layers into the sandbox's private upper. A partially completed startup
must not be replayed on the same project object; delete the owned sandbox and
retain its diagnostics and any cleanup errors.

## Checkpoint and recovery

Call `sandbox.create_snapshot()` externally. Sandboxd/runsc freezes and captures
the whole sandbox; application signals, Python and an idle model request are not
prerequisites. Persist the snapshot ID and `project.descriptor`. For a command
started with `ServiceHandle.start_command()`, persist its stable `command_id`.

Fence the source before creating a running clone with
`Sandbox.from_snapshot(..., **project.sandbox_options())`. Preserve the upper
geometry, mounts, reverse tunnel and current network policy. Bind the restored
sandbox using `ComposeProject.attach(restored, descriptor)`, then call
`reconnect_network()` to update owned forwarding/NAT for the new outer TAP.
Reconnect captured commands with `restored.commands.connect(command_id)`.
Do not rerun builds, Compose up, Docker startup or the agent command, and do not
send SIGCONT as part of ordinary whole-sandbox restore.

The snapshot does not include the Harbor client's memory, model proxy or
external services. Applications own consistency with those external systems.
Ordinary service exec and `start_command()` require no Python in the service.
The optional `start_process()`/`ServiceProcessHandle` API provides inner-process
inspection and signalling through a Python monitor in that service.

A service-specific controlled TCP entry exposes an existing outer reverse tunnel
to a bridge container without rewriting HTTP, authentication headers or SSE.
After recovery, attach saved descriptors rather than creating new listeners.
See the [SDK networking API](../README.md#native-compose-networking-and-model-tunnels).

## Retention, authorization and limits

Managed snapshots retain node-local collection leases. The last live or retained
checkpoint reference releases unused mounts; retained snapshots therefore count
toward the node's ROFS budget. Restore validates the collection, profile and
current tenant authorization. Adding tenants does not invalidate compatible
snapshots; revoking a tenant denies restore. Changes to captured tool, bootstrap
or storage geometry require a snapshot compatibility and drain plan.

Restore requires the retained collection on the same node and mount view.
Cross-node collection reconstruction, automatic Registry artifact GC and
replacement of a provider identity are outside this contract. Removing a local
mount does not authorize deletion of its Registry artifact. Keep referenced
runtime images and compatible complete runsc bundles available.

Supported networking is default/custom IPv4 bridge networking with service
DNS/aliases and TCP/UDP userland port mappings. External TCP address migration,
IPv6-only egress, non-bridge drivers, remote build contexts, arbitrary
runtime-created images and full Docker content-store/export compatibility are
outside the supported scope. Scaled service process handles are rejected.

## Client-managed runtime

For an explicitly client-managed deployment, use
`prepare_compose_images()` and `DockerRuntime(bridge_binary=...)`, or
`ComposeProject.prepare(..., auto_images=True)`. The client discovers image
metadata and supplies mount options; node pull authorization remains separate.
These options are not needed with `DockerRuntime(profile=...)`.

The helper must report `discover_schema: 2` through `--mode capabilities`.
Discovery confines Dockerfile, build-context and `.env` reads to the frozen
project and receives only explicitly supplied variables. External `include` /
`extends.file`, remote contexts and platform emulation are unsupported in this
client-managed path. See [the helper guide](../../../builder/image-bridge/README.md)
and [the client-managed example](../examples/compose_checkpoint.py).

## Verification

Run `make sdk-check`, the managed-runtime adapter unit tests and image-bridge Go
tests when changing these interfaces. Against an owned compatible endpoint,
verify unchanged task inputs, main and sidecar health, original process/memory
and filesystem recovery, and an independent verifier. Record failures and clean
up owned environments and snapshots even when an operation fails.

The opt-in `tests/integration/test_compose_network.py` checks default/custom DNS,
network isolation, TCP/UDP mappings, controlled model HTTP/auth/SSE and existing
inner/published-port TCP sockets across two delete/restore cycles. Its endpoint
and runtime prerequisites are explicit; CI does not run it on a shared cluster.
