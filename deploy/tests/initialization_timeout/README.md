# Initialization timeout qualification

Run this opt-in qualification on an empty, task-owned Linux runsc standalone,
using a separately installed candidate SDK wheel with `init_timeout` support.
Published `akernel-sdk==0.2.0rc4` lacks this API. This probe does not install the
SDK or add the checkout to Python's import path. Record the source SHA, wheel
SHA-256, native dependency version and node image ID with the results; the
candidate retains source version metadata and is not a new published release.

Prepare the node with the [standalone guide](../../standalone/README.md).
Use an exclusive persistent data directory and loopback control/data ports.
The host needs Docker access and the node needs capacity for one runsc sandbox
with one CPU and 1024 MiB memory. Supply a digest-pinned Python image containing
Python 3 and a shell. Image preparation may need registry access.

Match independent backend RPC budgets to the test before starting it. For the
packaged standalone profile, use these overrides in the exclusive node's
`/etc/akernel/adx-standalone.yaml` while it is empty, then restart `adx.service`:

```yaml
service_overrides:
  coordinator:
    config:
      rpc_timeout_seconds: 120
  apiserver:
    config:
      rpc_timeout_seconds: 120
  adxlet:
    config:
      rpc_timeout_seconds: 120
```

Merge this fragment with the existing profile, retaining its other settings.
The ordinary node RPC default is 30 seconds: delaying runtime creation for 35
seconds would otherwise hit that independent limit before the 60-second
initialization policy. The probe checks the node budget before installing its
fault fixture. These are qualification settings, not new product defaults.

```bash
python deploy/tests/initialization_timeout/run.py \
  --data-dir /absolute/path/to/task/standalone/data \
  --container-id "$OWNED_NODE_ID" --allow-runtime-delay \
  --image 'docker.io/library/python:3.12-slim@sha256:<verified-digest>' \
  --output /absolute/path/to/fresh/evidence-directory
```

The probe verifies the full node ID, data mount, loopback bindings, empty
logical inventory and empty physical runsc inventory before changing its
exclusive node. It preserves the original runsc binary beside a node-only
wrapper. An atomically consumed control file delays one matching environment's
real `runsc create` invocation during restoration by 35 seconds. The pinned
sandboxd then loads saved state through runsc's control RPC, without invoking a
`runsc restore` CLI command. All other runtime calls execute
the original binary, and adjacent gVisor helpers remain intact. No SDK classes,
constructors, transports, business commands or restore results are intercepted.
The fixture remains in this disposable node: destroy the drained node after the
probe, rather than returning its modified filesystem to general use.

The three sequential cases verify:

- Manual reload with `init_timeout=60` completes after more than 30 seconds.
- Automatic same-node checkpoint failover retains that same server policy and
  succeeds with the controlled delay after the original runtime is killed.
- Manual reload with `init_timeout=10` fails during backend preparation/runtime
  start before the delayed restore completes. The failure phase and physical
  cleanup must be observed, rather than inferred from `reload() == False`.

Each case creates a real local checkpoint through Execd's internal Unix socket
while the original business command runs in the background. The checkpoint
caller is a separate helper, because its socket request is retired on restore.
That socket remains an internal test interface. Successful recovery preserves
the logical ID, command ID and
public facades, rolls back a file, resumes the original command to its expected
nonzero result, and permits subsequent commands. The result contract remains
unchecked unless a caller requests checking. Failed or unknown checkpoint
outcomes stop the case without another checkpoint or business-command replay.

The probe explicitly deletes its environments, checks resource reservations
and physical runtime inventory, and waits beyond the injected delay to detect a
late restore after cancellation. Require exit code zero, summary status
`passed`, all case cleanup checks and an empty final physical inventory.
Retain failed summaries/process output and inspect the node before teardown.
The summary contains IDs, timings, version/budget and failure context; it never
writes the token or full Environment specification.

This qualifies the installed candidate and recorded runtime bundle. It does
not establish cross-node failover, a universal 600-second default, arbitrary
workload timing, released-package availability, or a caller-side reload deadline.

Run the fault-scope unit tests without a node using
`python -m unittest discover -s deploy/tests/initialization_timeout -p 'test_*.py'`.
