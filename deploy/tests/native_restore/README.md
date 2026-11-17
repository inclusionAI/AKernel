# Native checkpoint qualification

Use Python 3.12+ with the published SDK version being qualified. These opt-in
probes create and delete real remote sandboxes. Run them on an operator-owned,
idle deployment with explicit source/target node IDs and adequate capacity.
They do not install the SDK, change the cluster, or stop cloud instances.

Keep the connection JSON outside Git with mode 0600. Its keys are api_url,
api_key, verify_tls and gateway_address, matching akernel_sdk.Client. Use
verify_tls=true and SSL_CERT_FILE for a private CA; the URL hostname must match
the service certificate. Control/API credentials belong on HTTPS. The gateway
address selects the deployment's existing sandbox data port.

Before running, verify the actual image digests and require all target workers
in the authenticated resource API's available state. Pod Running and Helm wait
can precede ADX registration. Record runtime pins and filesystem placement;
local checkpoint inputs and private filestores must support actual FICLONE for
native reflink. The [deployment guide](../../shared-storage/production.md)
describes S3 publication and full local materialization before restore.

## SDK lifecycle through the configured entry

```bash
python deploy/tests/native_restore/checkpoint_lifecycle.py \
  --connection-file "$SDK_CONNECTION_FILE" \
  --source-node "$SOURCE_NODE" --target-node "$TARGET_NODE" \
  --result "$EVIDENCE/lifecycle.json"
```

This checks execution/files, pause and same-ID resume, leave-running Snapshot
capture, cross-node restore, write isolation and explicit remote deletion of
instances and Snapshot. It reports workload and cleanup failures together.
The result path must be fresh. The default immutable Python image can be
overridden with --image if the replacement supplies Python 3 and a shell.

## Source-host loss and uncached recovery

```bash
python deploy/tests/native_restore/shared_checkpoint_probe.py capture \
  --connection-file "$SDK_CONNECTION_FILE" --node-id "$SOURCE_NODE" \
  --state "$EVIDENCE/checkpoint.json" --name "$OWNED_SNAPSHOT_NAME"
```

Capture deliberately retains the detached source and Snapshot for an
operator-owned outage test. The source continues after publication. The state
file records its IDs, random nonce, approximately 19 MiB of memory, business
counter and immutable image. Normal and sparse file contents are also checked.
The call duration includes publication and does not isolate freeze time.

Before stopping the source host, verify that every affected workload is owned
by the test. Record that target checkpoint caches contain no artifacts; warming
the ordinary guest image does not warm the checkpoint cache. Stop the source
host through its owner's workflow and require actual stopped/unreachable
status. Keep it unavailable through each of these phases:

```bash
python deploy/tests/native_restore/shared_checkpoint_probe.py restore \
  --connection-file "$SDK_CONNECTION_FILE" --state "$EVIDENCE/checkpoint.json" \
  --node-id "$TARGET_B" --result "$EVIDENCE/cold-b.json"
python deploy/tests/native_restore/shared_checkpoint_probe.py restore \
  --connection-file "$SDK_CONNECTION_FILE" --state "$EVIDENCE/checkpoint.json" \
  --node-id "$TARGET_C" --result "$EVIDENCE/cold-c.json"
python deploy/tests/native_restore/shared_checkpoint_probe.py restore \
  --connection-file "$SDK_CONNECTION_FILE" --state "$EVIDENCE/checkpoint.json" \
  --node-id "$TARGET_B" --result "$EVIDENCE/repeat-b.json"
python deploy/tests/native_restore/shared_checkpoint_probe.py concurrent \
  --connection-file "$SDK_CONNECTION_FILE" --state "$EVIDENCE/checkpoint.json" \
  --node-id "$TARGET_B,$TARGET_C" --result "$EVIDENCE/concurrent.json"
```

Restore checks the original memory/business state, file contents and private
writes. Concurrent keeps both consumers alive while checking independent
memory and disk changes. These phases explicitly delete their detached remote
instances. Restore the source host even if qualification fails, and verify
actual service availability after it boots; a stale Node Ready condition is
insufficient. Delete the retained source explicitly by ID when it is no longer
needed. Keep the Snapshot until all observations finish.

## Continuous observation and cleanup

```bash
python deploy/tests/native_restore/shared_checkpoint_soak.py \
  --connection-file "$SDK_CONNECTION_FILE" --state "$EVIDENCE/checkpoint.json" \
  --nodes "$TARGET_B,$TARGET_C" --result-directory "$EVIDENCE/soak" \
  --duration 86400 --interval 300
```

Run through the operator's process supervisor and record its identity and
ownership. Each round verifies memory, business state, files and cleanup.
Running is not a completed 24-hour result. Interruption is recorded as failure;
record an intentional deployment replacement separately without rewriting the
old evidence. After observation stops, explicitly delete the owned Snapshot
through Client.Sandbox.delete_snapshot. An in-use response requires confirming
and releasing its consumers before a bounded retry. Ordinary detached kill()
only closes local transports; use kill(delete_remote=True) or the by-ID
Client.Sandbox.kill operation to delete remote instances.

Keep component failover, storage/network/disk faults, forced cache eviction
during background restore, backups and isolated recovery as separate acceptance
gates. Individual timings from these probes are functional samples rather than
a controlled storage performance comparison.

## Unknown-outcome runtime fixture

run.py and unknown_restore.rs exercise production ADX Controllers against a
dedicated empty Sandboxd using an in-process authority. They require identical
checkpoint paths on the fixture host and Sandboxd and the matching runtime
bundle. Read run.py --help before selecting this separate local fault fixture.
