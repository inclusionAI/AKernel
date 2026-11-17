# Network checkpoint and restore diagnostic

This opt-in probe creates one runsc sandbox on an **empty, dedicated local
standalone**. It reads credentials from the explicit standalone data directory,
verifies the container's `/home/akernel` mount, discovers its published
8443/8080 ports, and enables TLS verification with that profile's public CA.
Use a virtual environment with the published `akernel-sdk` version being
qualified; this script does not install or import the repository SDK by path.

Prepare the node using the [standalone guide](../../standalone/README.md), with
the runsc platform selected in its configuration. The host needs Docker access,
and the node must allow one sandbox with two CPUs, 2048 MiB of memory and
2048 MiB of storage. Network modes download Alpine packages during setup and
require working outbound connectivity. Record the installed SDK version and
the complete runtime bundle selected by
`src/sandboxd/third_party/runtime-versions.env` with the results.

From the repository root, after starting the dedicated standalone:

```bash
python deploy/tests/checkpoint_conntrack/run.py \
  --data-dir /absolute/path/to/dedicated/standalone/data \
  --output /absolute/path/to/ignored/evidence \
  --mode netns --veth-path move --attempts 8 --restores 2
```

`--container` defaults to `akernel-node`. Modes are:

- `baseline`: an immutable Python image, checkpoint completion and file rollback.
- `netns`: an immutable Docker image used as a rootfs, with Python/iproute2/curl
  installed from Alpine. A child network namespace, bridge, veth, DNAT and
  MASQUERADE carry real HTTP traffic. A rule-removal negative control verifies
  that access depends on DNAT. This mode does not run dockerd.
- `dind`: start dockerd with the image's legacy iptables wrappers, vfs storage, default userland proxy, IPv4 bridge/NAT and an imported local busybox image. Explicitly install `busybox-extras` for its HTTP applet, which is absent from the image's base BusyBox, and wait for the server before continuing. Stop docker-proxy before checkpointing, then require published-port HTTP from both the sandbox and an independent ingress network namespace. An HTTP server in that namespace observes the inner container's outbound source address to verify MASQUERADE. Repeat these checks after every checkpoint and reload, retaining the same inner container ID. It requires the runtime's legacy addrtype and conntrack support. A setup failure is a failure before checkpoint coverage, even if an alternative mode works; container state and logs are retained alongside daemon diagnostics.

Network modes open 32 keepalive connections and short-lived HTTP connections.
For `netns`, run both `--veth-path move` (create the pair, then move the peer)
and `--veth-path direct` (create the peer in the destination namespace).
The netns HTTP server waits for the client FIN on `/active-close`; retaining up
to 256 socket handles makes their TIME_WAIT state visible through gVisor's
socket-based `/proc/net/tcp` enumeration. A zero count after closing every file
descriptor does not establish that the transport has no TIME_WAIT endpoints.
Legacy iptables rule counters can remain zero on gVisor; the rule-removal
control establishes traffic dependence instead of relying on those counters.

There are 1–8 checkpoint attempts (default 3). Each uses a stable command ID,
an Execd Unix-socket HTTP request with a 120-second timeout, and a separately
observed background command. Bounded runtime stack captures are collected if
completion is slow. An unknown or failed outcome stops the probe without a
duplicate checkpoint or restore. The SDK observation budget is 120 seconds;
the host performs up to 25 five-second waits plus bounded diagnostic calls.
Cold network-image creation gets a separate 360-second budget.

Only explicit checkpoint completion permits reload. `--restores` selects one
or two reloads (default 2) from the same last checkpoint, with a fresh marker
write between them. File rollback, restored peer address/default route and the
post-reload HTTP request are checked separately. Source and restored HTTP
request counters must keep increasing. The HTTP exit code and body
are recorded directly so a later command cannot mask a failed request.
The probe deletes the sandbox in `finally`; inspect `summary.json` and the
actual runsc inventory before reusing or stopping the standalone. Local logs
and credentials must remain untracked.

Each invocation creates a timestamped subdirectory under `--output`.
`summary.json` records the SDK version, node/image identity, sandbox identity,
checkpoint command IDs and outcomes, restore assertions, and cleanup result.
Setup logs, bounded runtime stacks and restore diagnostics are collected when
applicable. Preflight failures can occur before that subdirectory exists, so
retain the process output as well. Require process exit 0 and summary status
`passed` for a successful workload result. Check `sdk_cleanup_error` separately
and confirm the actual inventory is empty before reusing the node.

A successful `baseline` run covers checkpoint completion and file rollback.
Network acceptance additionally requires successful fixture setup, advancing
traffic, and successful HTTP requests after every reload; `netns` also checks
the restored peer address and default route. Run both veth paths separately and
qualify each runtime platform and architecture being deployed. A setup failure
or unresolved checkpoint outcome leaves the corresponding coverage incomplete.
Passing `netns` does not qualify the Docker daemon path. The `dind` fixture
requires legacy iptables addrtype support and currently imports an x86_64
loader, so run that mode only on Linux/amd64.

This traffic workload exercises checkpoint and restore under active TCP
connections. It does not deterministically trigger every timer/conntrack lock
interleaving; use the gVisor transport regression tests for controlled
lock-order coverage. It also does not replace the full standalone SDK suite or
multi-sandbox isolation checks. Drain active sandboxes before upgrading; a
passing run does not establish checkpoint compatibility across runtime builds.
