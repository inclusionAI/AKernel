# TCP/conntrack checkpoint diagnostic

This opt-in probe creates one runsc sandbox on an **empty, dedicated local
standalone**. It reads credentials from the explicit standalone data directory,
verifies the container's `/home/akernel` mount, discovers its published
8443/8080 ports, and enables TLS verification with that profile's public CA.
Use a virtual environment with the published `akernel-sdk` version being
qualified; this script does not install or import the repository SDK by path.

From the repository root, after starting the dedicated standalone:

```bash
python deploy/tests/checkpoint_conntrack/run.py \
  --data-dir /absolute/path/to/dedicated/standalone/data \
  --output /absolute/path/to/ignored/evidence \
  --mode netns --attempts 2
```

`--container` defaults to `akernel-node`. Modes are:

- `baseline`: an immutable Python image, checkpoint completion and file rollback.
- `netns`: an immutable Docker image used as a rootfs, with Python/iproute2/curl
  installed from Alpine. A child network namespace, bridge, veth, DNAT and
  MASQUERADE carry real HTTP traffic. A rule-removal negative control verifies
  that access depends on DNAT. This mode does not run dockerd.
- `dind`: start dockerd with the image's legacy iptables wrappers, vfs storage,
  default userland proxy, IPv4 bridge/NAT and an imported local busybox image.
  It requires the runtime's legacy addrtype support. A setup failure is a
  failure before checkpoint coverage, even if an alternative mode works.

Network modes open 32 keepalive connections and short-lived HTTP connections.
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

Only explicit checkpoint completion permits reload. File rollback and the
post-reload HTTP request are checked separately. The HTTP exit code and body
are recorded directly so a later command cannot mask a failed request.
The probe deletes the sandbox in `finally`; inspect `summary.json` and the
actual runsc inventory before reusing or stopping the standalone. Local logs
and credentials must remain untracked.

On the `release-20261012.0-akernel.1` bundle with SDK 0.2.0rc4, ordinary
checkpoint/file rollback passes. The controlled gVisor timer-output regression
from [AKernel #129](https://github.com/inclusionAI/AKernel/issues/129) still fails.
The netns workload completed eight live checkpoints, but a separately checked
post-reload HTTP request timed out with the moved child veth missing
([AKernel #134](https://github.com/inclusionAI/AKernel/issues/134)). Default
DinD NAT also fails earlier on the legacy addrtype matcher
([AKernel #133](https://github.com/inclusionAI/AKernel/issues/133)). These are
diagnostic failures to retain, not passing full network-restore acceptance.
