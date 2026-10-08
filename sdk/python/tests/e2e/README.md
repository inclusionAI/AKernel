# SDK end-to-end test levels

The directories below encode deployment requirements so that a narrow gate
never discovers tests that need a larger environment by accident.

- `standalone/`: public SDK contracts that must pass on one AKernel node. Full
  deployments rerun this suite against their public endpoints.
- `multi_vm/`: cross-node placement, routing, failover, and node-loss cases.
- `full/`: Kubernetes deployment, multi-worker, storage, accelerator, and
  infrastructure fault cases.
- `fault/`: opt-in process fault cases. The Full Deployment runner reports
  this group separately; it is skipped without an isolated test deployment
  and explicit restart hooks.

Keep unit tests under `tests/unit/`. The existing `tests/integration/` suite
contains runtime-heavy checkpoint and reverse-tunnel coverage and is selected
explicitly by the standalone gate. New cases must be added to the smallest
deployment level that provides all required capabilities.

## Functional coverage and execution

Run from `sdk/python/` after setting `AKERNEL_RUN_INTEGRATION=1`,
`AKERNEL_SERVER_ADDRESS`, `AKERNEL_GATEWAY_ADDRESS`, and `AKERNEL_TOKEN` for a
real deployment:

```bash
python -m tests.e2e.run --level l0 --output out/e2e/l0
python -m tests.e2e.run --level standalone --output out/e2e/standalone
python -m tests.e2e.run --level full --output out/e2e/full
python -m tests.e2e.run --level full --require-clean-redis --output out/e2e/formal
python -m tests.e2e.run --level full --case cli-contract --output out/e2e/cli
python -m tests.e2e.run --level full --case io \
  --campaign-root out/e2e/campaign-01 --output out/e2e/campaign-01/io-01
```

The level includes all lower levels. Each group runs as a separate process,
writes its own log and result to `summary.json`, and runs after earlier groups
fail. The default execution budget is 10,800 seconds (three hours), with shorter
per-group caps. For a campaign with multiple invocations, give each invocation
a unique direct child output directory and pass the same `--campaign-root`;
the runner then subtracts all earlier run time from that campaign's
three-hour budget. It also counts pressure-test `duration_seconds` and named
profile summaries such as `mixed-summary.json`. New E2E summaries count Redis
preflight and audit time; older summaries without run time fall back to their
recorded group durations. Setup failures that produced no duration remain
unmeasured, so use `--budget-seconds 10500` for a five-minute reserve when
combining E2E and pressure runs. Run
campaign invocations serially. Do not immediately retry
a failed group during the first pass: collect all results, classify failures,
then fix and rerun the
affected groups within the same overall campaign budget. Tests use unique
resource names and clean up in `finally`/context managers.

| Level | Independently reported groups | Main assertions |
|---|---|---|
| L0 | `sdk-contracts` | Create, command/env/cwd/timeout, stdin/process state, binary filesystem and missing-path errors, info/resources, repeatable delete |
| Standalone | `lifecycle`, `auth`, `cli-contract`, `io`, `pty-close`, `network-url`, `network-tunnel`, `network-policy`, `runtime-integration`, `storage-quota`, `network-policy-matrix` | Detached name and conflicting spec, bad API key, CLI resources/list/exec/delete, no-checkpoint reload, output and directory copy, PTY resize and connection-close cleanup in the guest, checkpoint/reload, declared-port URL, reverse tunnel, dynamic policy, quota, ACL replacement |
| Multi-VM | `node-placement` | Two distinct pinned nodes can execute isolated sandboxes; unknown node is rejected |
| Full | `redis-lifecycle`, `placement-affinity`, `admission`, `concurrent-claim`, `unknown-create-result`, `create-transport-disconnect`, `custom-image`, `dockerfile`, `port-forward`, `storage-sources`, `tenant-isolation`, `key-lifecycle`, `scheduler-admin`, `scheduler-maintenance`, `fault-process-restart`, `fault-node-loss`, `fault-checkpoint-shared`, `fault-checkpoint-local` | Persisted node assignment, runtime-aware placement, deletion/idle resource release, activity extending idle lifetime, failed-create cleanup, same-spec concurrent claim, lost create result with same-ID retry, real TCP reply loss after confirmed create, actual HTTP over a published port and route withdrawal after deletion, OCI and Dockerfile launch, OCI private writes and inherited entrypoint, OCI/S3 read-only mounts, S3 rootfs, GPU, cross-tenant denial, key expiry/revocation, scheduler-administration permissions and opt-in node maintenance, opt-in process restart, node loss, and checkpoint storage matrix |

`redis-lifecycle` needs a test-only Redis endpoint via `AKERNEL_REDIS_HOST`,
optional `AKERNEL_REDIS_PORT`, `AKERNEL_REDIS_PASSWORD`, and
`AKERNEL_REDIS_NAMESPACE` (default `akernel`). Its log never prints these
values. It verifies release of resource reservations and reports Deleted
terminal records separately; the latter are currently retained by ADX.
When `AKERNEL_REDIS_HOST` is set, the runner also writes read-only
`redis-before.json` and `redis-after.json` snapshots and compares held
reservations in `summary.json`. A newly held reservation, an existing held
reservation newly entering `Failed`, or an unavailable Redis audit fails the
run even if individual cases pass. `no-new-held` means only that this
run added no reservation; `cluster_clean=false` records any earlier held
reservation. Deleted terminal records without held resources are not leaks.
Formal capacity runs require `cluster_clean=true` before starting and after
draining. `--require-clean-redis` enforces that gate: it stops before running
cases when Redis is unavailable or already has held reservations, and fails if
any reservation remains afterward. Use it for formal Full Deployment gates;
diagnostic runs against a known-dirty cluster may omit it while retaining the
before/after audit. Never remove Redis fields directly to make an audit pass;
ownership and runtime cleanup must reconcile through the control plane.
The `admission` group belongs to Full Deployment because it deliberately
exercises failed create paths and must run on an isolated, disposable test
deployment. A 2026-09-23 run showed that an unsupported runtime could leave a
new held `Failed` record; its two case assertions passed but the Redis audit
correctly failed the overall run. An isolated 2026-09-24 rerun with the local
adxlet correction passed and left no held reservations. The shared cn-north-4
cluster still has earlier held records and cannot pass the clean-Redis gate.
`concurrent-claim` requires `AKERNEL_TEST_DESTRUCTIVE_CLAIMS=1` and an isolated
deployment. It starts two detached creates with the same name and specification
at the same instant, requires both handles to converge to one Sandbox ID, then
deletes that name. The Redis before/after audit is mandatory for this group;
do not run it against a shared cluster that already has uncertain creates.
`unknown-create-result` uses the same opt-in and Redis requirement. It performs
a real create, discards the first confirmed result inside the SDK, and verifies
that its retry sends the same Request ID and name. This covers a lost client
result and server replay; it does not simulate a broken TCP connection or a
gateway restart, which need separate fault-injection acceptance.
`create-transport-disconnect` uses the same isolated-deployment opt-in. A
test-only loopback proxy forwards the request to the real API, waits for a
confirmed-running final result, then closes the SDK's TCP connection before
delivering that result. The SDK must reconnect with the same Request ID and
name and return the original Sandbox ID. The proxy forwards subsequent
readiness, command and cleanup requests. On the isolated 2026-09-24 standalone
deployment the targeted case passed 1/1 and the Redis audit found no held
reservations. The initial SDK request observed a real disconnected socket;
readiness briefly observed the known route-publication delay before succeeding.
This test does not restart a gateway.

`port-forward` needs a resolvable Python HTTP-server image in
`AKERNEL_TEST_HTTP_IMAGE`; the standalone `network-url` group verifies the declared
port URL without pulling an external image. The group also includes two
Sandboxes exposing the same guest port and checks that repeated requests keep
their instance-specific payloads separate; the isolated standalone run passed
both original port-forward cases (2/2) with the pinned SWR image. The newly
added withdrawal case keeps one Sandbox serving while it deletes a
second Sandbox on the same guest port. It requires the deleted URL to stop
routing while the survivor URL remains usable. A 2026-09-24 isolated
standalone targeted run passed 1/1 using the local derived ADX image; the
earlier 2/2 result remains the baseline for the original cases.

`AKERNEL_TEST_PORT_HOST_DOMAIN` enables the separate Host-subdomain assertion:
two Sandboxes expose the same port and receive requests at
`<sandbox-id>-<port>.<domain>`. The test connects to the configured Gateway
address with an explicit Host header, so DNS setup cannot mask a routing
failure. The isolated 2026-09-24 run reached Gateway and returned HTTP 404
for the Host route on the earlier image (1 failed); Redis had no held resources
afterward. With ADX Buildkite #85 (`4deac54`) and fresh isolated standalone
state, the same real Gateway case passed 1/1 in 5.258 seconds. Both Sandboxes
used port 18081, returned their own payload through distinct Host headers, and
left no held Redis reservations. A transient route-cache 503 during command
setup was retried with the same Request ID. This validates Host-header routing
without requiring wildcard DNS; public DNS and certificate provisioning remain
deployment concerns.

`placement-affinity` uses test-only Redis access to check that two explicit
node pins become the persisted assignments and execute on distinct nodes.
Runtime affinity reads each eligible node's persisted sandboxd runtime inventory
and automatically selects one capable and one incompatible, live node.
`AKERNEL_TEST_RUNTIME_CLASS` optionally selects a runtime other than
`AKERNEL_TEST_RUNTIME` (default `runsc`); `AKERNEL_TEST_RUNTIME_NODE_ID` and
`AKERNEL_TEST_RUNTIME_INCOMPATIBLE_NODE_ID` optionally pin a specific verified
pair. `AKERNEL_TEST_RUNTIME_IMAGE` supplies a compatible image when needed.
The test checks the failed request's persisted ownership and resource hold
before cleanup, then checks that unpinned creates land only on capable nodes. A homogeneous
cluster reports a skip. A group with one pass and one skip is marked `partial`
in `summary.json`, not fully passed. Set `AKERNEL_REQUIRE_RUNTIME_AFFINITY=1`
for a formal heterogeneous-runtime gate; missing inventory or a missing
capable/incompatible pair then fails the case. A missing `runtime_classes`
field is unknown inventory and is never treated as proof of incompatibility.
The 2026-09-24 cn-north-4 rerun passed the two-node persisted pin case
(1 passed) and skipped runtime affinity because its node records did not
report runtime inventories (1 skipped). Its Redis audit found no new held
resources, but the shared cluster had 10 preexisting held records. On
2026-09-26 the isolated `akernel-adx-test` deployment ran the runtime-affinity
case with one `runc`-capable worker and two `runsc`-only workers: 1/1 passed,
0 skipped, and no held Redis reservation remained. The temporary `runc`
worker was removed after the run. The idle client-exit case leaves a command
running, exits the client without deleting the detached Sandbox, and requires
idle reclamation. It failed on the earlier isolated standalone image: the
record did not reach Deleted within 45 seconds; explicit cleanup released its
reservation. With ADX Buildkite #85 and fresh isolated standalone state, the
same case passed 1/1 in 14.729 seconds, reached Deleted, and left no held Redis
reservation. Reusing the previous standalone AOF with the new package failed
before either case ran: Coordinator logged Redis recovery unavailable and its
node catalog stayed closed. The cause of that reused-state startup failure is
not yet confirmed; the fresh-state pass does not resolve it. A read-only copy
of the old AOF had 10,587 `environment:` fields in the control hash (about
42.6 MB), while an isolated Redis `HGETALL` completed in 0.063 seconds. This
does not establish which Coordinator recovery operation timed out.

`storage-sources` uses
`AKERNEL_TEST_IMAGE`, `AKERNEL_TEST_MOUNT_IMAGE`,
`AKERNEL_TEST_ENTRYPOINT_IMAGE`,
`AKERNEL_S3_ENDPOINT`, `AKERNEL_S3_BUCKET`, `AKERNEL_S3_ROOTFS_OBJECT`,
`AKERNEL_S3_MOUNT_OBJECT`, optional S3 credentials, and
`AKERNEL_TEST_GPU_XPU`. Each test without its required asset is reported as
skipped, not as covered. Firecracker is a separate run with
`AKERNEL_TEST_RUNTIME=firecracker` on a KVM worker; it is not implied by a
runsc result. Full deployment also requires deployment/fault suites outside
this public SDK runner; do not label this SDK profile as the complete platform
Full Deployment gate.

The three OCI source methods passed on the isolated standalone deployment.
The two S3 EROFS methods were executed against an isolated MinIO fixture on
2026-09-26 but failed before their file-content and read-only assertions:
the sandboxd image daemon did not start. A later check on an EROFS-capable
worker failed at the same stage, so those methods remain unverified. The
temporary fixture and worker were removed and the Redis reservation audit
returned to zero held records. The whole-GPU method remains skipped because
the five cn-north-4 nodes have no advertised GPU/NPU resource. This AKernel
SDK currently accepts `gpu` XPU requests only; ADX NPU execution therefore
needs separate public-ADX-SDK coverage on an NPU-equipped worker.

`scheduler-admin` requires `AKERNEL_TEST_MANAGE_KEYS=1` on an isolated
deployment. It creates a temporary tenant key and checks that the tenant
cannot read the central waiting queue or pause/resume a registered node, while
the administrator can read the queue. `scheduler-maintenance` additionally
requires `AKERNEL_TEST_SCHEDULER_MAINTENANCE=1` and an explicit
`AKERNEL_TEST_SCHEDULER_NODE_ID`. It creates a running Sandbox on that test
worker, pauses new scheduling through the public API, verifies that the
existing Sandbox still executes commands, then restores normal scheduling in
`finally`. Do not enable this case on a shared worker.
`tenant-isolation` separately checks that another tenant cannot look up, list,
or delete the owner's running Sandbox, and that a denied delete leaves the
owner's command path usable.
The list-isolation case passed 1/1 on the isolated two-worker deployment on
2026-09-26. The scheduler-administration cases initially received HTTP 404
because the AKernel Ingress route override omitted `/global-scheduler`. After
the Chart route correction, both targeted cases passed 1/1 with no skip or
held Redis reservation. The maintenance case also verified that the node
returned to normal scheduling status.

`fault-process-restart` is destructive and requires an isolated deployment.
Set `AKERNEL_TEST_DESTRUCTIVE_FAULTS=1`, `AKERNEL_TEST_FAULT_NODE_ID`, and
absolute executable hook paths in `AKERNEL_TEST_RESTART_SANDBOXD` and
`AKERNEL_TEST_RESTART_NODE_MANAGER`. Each hook receives the target node ID as
its only argument and must return only after the service has restarted. The
cases create a pinned Sandbox, write a marker, restart one service, and require
the same Sandbox ID, command access, and file contents to survive. Missing
hooks are reported as skipped. These cases cover fast process restart.
Heartbeat-expired node loss and cross-node failover use separate fault cases
and storage fixtures below.

`fault-node-loss` checks the failover-enabled, no-checkpoint branch: one pinned worker is stopped
past the heartbeat limit while another worker remains responsive. The old
Sandbox must become `Failed` and stay failed after the worker recovers. It
requires distinct `AKERNEL_TEST_FAULT_NODE_ID` and
`AKERNEL_TEST_HEALTHY_NODE_ID`, plus absolute executable
`AKERNEL_TEST_STOP_WORKER` and `AKERNEL_TEST_RECOVER_WORKER` hooks. Each hook
receives the target node ID. The recovery hook runs in `finally` even when the
test fails. The 2026-09-25 isolated `akernel-adx-test` deployment passed 1/1
when the test worker's Coordinator heartbeat was blocked while its Pod and
runtime remained running. A Kubernetes Pod deletion follows a different,
graceful shutdown path and does not validate this failure contract.

`fault-checkpoint-shared` and `fault-checkpoint-local` are separate opt-in groups. Run them only on an
isolated two-worker deployment with `AKERNEL_TEST_DESTRUCTIVE_FAULTS=1`,
`AKERNEL_REDIS_HOST`, distinct `AKERNEL_TEST_FAULT_NODE_ID` and
`AKERNEL_TEST_HEALTHY_NODE_ID`, and the executable
`AKERNEL_TEST_STOP_WORKER` / `AKERNEL_TEST_RECOVER_WORKER` hooks described above.
The guest image must expose the Execd Unix socket at
`AKERNEL_TEST_CHECKPOINT_SOCKET` (default `/run/akernel/execd.sock`). The
default client uses `curl`; set `AKERNEL_TEST_CHECKPOINT_CLIENT=python` with a
guest image containing Python 3 to send the same HTTP request over the Unix
socket. `AKERNEL_TEST_CHECKPOINT_IMAGE` selects that guest image. Set
`AKERNEL_TEST_CHECKPOINT_STORAGE=shared` and
`AKERNEL_TEST_SHARED_CHECKPOINT_ALIAS` to the actual object-store alias for a
shared-store deployment, then run `--case fault-checkpoint-shared`. On a separate
local-store deployment, set `AKERNEL_TEST_CHECKPOINT_STORAGE=local` and run
`--case fault-checkpoint-local`. The shared case creates without `node_id`:
that option is a hard placement constraint and would prevent cross-node
recovery. It reads the initial owner from the committed assignment and applies
the fault to that worker; the hooks must accept either configured worker ID.
The same Sandbox ID must resume on the other worker under a higher generation,
with checkpoint-era file contents preserved. The local case keeps a pinned
source and requires a terminal Failed result on the old owner, unchanged
generation and no held resources.
The recovery hook runs after either fault outcome, including when the stop
hook fails. On 2026-09-25 the isolated two-worker deployment passed the
local-only case 1/1 with Redis clean and no fault-hook residue. On 2026-09-26,
the isolated shared S3 case passed 1/1 against ADX Buildkite #100 (`aa17667`):
same-ID takeover, higher generation, checkpoint marker preservation and no
new held Redis resources. A default skip does not count as coverage.

For `port-forward`, set `AKERNEL_TEST_HTTP_IMAGE` to an operator-provided,
digest-pinned linux/amd64 image with Python 3 and `http.server`. The 2026-09-24
cn-north-4 run used such an image to start the server inside a Sandbox and
verified the exact payload through the published URL (1/1 passed, 0 skipped;
Redis audit `no-new-held`). Configure sandboxd's registry pull credentials;
a Docker login on the test client is not a substitute. The tested image also
included `/bin/bash` and `apt-get`, which the historical
`runtime-integration` tests require. Set `AKERNEL_TEST_INTEGRATION_IMAGE` to
an image with those tools when `AKERNEL_TEST_IMAGE` points to a smaller fixture.

For x86-64 OCI source tests, use a digest-pinned fixture with
`/etc/os-release`, a shell, and a successful short-lived ENTRYPOINT.
Set `AKERNEL_TEST_IMAGE`, `AKERNEL_TEST_MOUNT_IMAGE`, and
`AKERNEL_TEST_ENTRYPOINT_IMAGE` to that same image to exercise the three OCI
assertions in `storage-sources`; `custom-image` also accepts `AKERNEL_TEST_IMAGE`.
The E2E runner preserves the caller's image selection. If the registry needs
authentication, configure sandboxd's `registry_auths.json`; Docker's host
login alone is insufficient for sandboxd's own image pull. The isolated
acceptance executed all three OCI assertions and the custom-image example
against a pinned fixture, with zero new Redis reservations. S3 and GPU
fixtures remain separate requirements.

`tenant-isolation` requires a valid key for a different tenant in
`AKERNEL_SECOND_TENANT_TOKEN`, or it can create and revoke a temporary tenant
key when `AKERNEL_TEST_MANAGE_KEYS=1`. `key-lifecycle` needs that same explicit
management flag and an administrator `AKERNEL_TOKEN`; it creates unique keys
and revokes them in cleanup. The keys are never written to E2E logs or result
files. Without either source of a second tenant key, isolation is skipped.
The tenant suite checks lookup and DELETE isolation separately, and with key
management enabled also checks that a tenant key cannot create administrator
keys. In the isolated ADX #100 deployment, the admin-create denial passed
1/1 with HTTP 403 and no held resources. The first DELETE run exposed an SDK
error-mapping inconsistency: the server returned 403 but the SDK raised a raw
HTTP error. After mapping DELETE 403 to the public `PermissionDenied` error,
the isolated targeted regression passed 1/1: the foreign DELETE was denied,
the owner executed a command successfully, and Redis held remained zero.

The first campaign records functional gaps before tuning performance.
Worker-loss without a checkpoint and local-only checkpoint failover passed on
the isolated two-worker `akernel-adx-test` deployment with ADX #97. Shared
checkpoint failover passed with ADX #100 after the S3 startup and workload
checkpoint publication fixes. The temporary object store was removed and the
workers were restored to local checkpoint storage; the final Redis audit found
only `Deleted` records and no held resources.
Two isolated S3 EROFS rootfs/read-only mount cases were also attempted with a
temporary object store and uploaded runtime image. Both failed before their
file or read-only assertions because sandboxd reported an unavailable image
daemon. A second targeted run after correcting the fixture endpoint format
failed at the same boundary. These cases remain unverified; their failure is
not evidence that the file or mount semantics were exercised.
Transport-level result loss has an independent case; live GPU/NPU scheduling
still needs real devices. A skipped case does not count as coverage.

## Public SDK coverage map

This map describes executable cases, not a claim that the currently deployed
version has passed them. The `summary.json` for a specific run is the result
of record. A skipped case remains uncovered for that environment.

| Public surface | Case groups | Remaining functional evidence |
|---|---|---|
| Default create, runtime, CPU/memory request and limit, node pin | `sdk-contracts`, `admission`, `redis-lifecycle`, `node-placement`, `placement-affinity` | Two-node persisted pin and heterogeneous `runc`/`runsc` placement passed in the isolated cn-north-4 deployment |
| Detached name, kill/delete, idle TTL, reload | `lifecycle`, `redis-lifecycle`, `runtime-integration`, `concurrent-claim`, `unknown-create-result`, `fault-checkpoint-shared` | Idle client-exit with an unfinished command passed with Buildkite #85; shared checkpoint cross-node recovery passed with #100; persisted-state upgrade remains |
| Commands, stdin, process list/kill, cwd/env, stderr/exit, timeout | `sdk-contracts`, `io`, `runtime-integration` | Cancellation while a command is in flight; timeout behavior must pass its E2E assertion |
| File read/write/list/metadata/rename/remove and local copy | `sdk-contracts`, `io` | Permission denial and large directory limits |
| PTY create/resize, independent sessions, interrupt, connection close | `runtime-integration`, `pty-close` | Actual guest `stty size` assertion passed in isolated standalone; `pty-close` passed 1/1 on the isolated two-worker cluster on 2026-09-26, checking remote process exit and continued Sandbox command access; connection loss and slow-consumer cleanup remain |
| Declared port URL, real HTTP forwarding, reverse tunnel | `network-url`, `network-tunnel`, `port-forward`, `runtime-integration` | Path route, withdrawal, and Host-subdomain route passed in isolated standalone; wildcard DNS/TLS and reconnect under failure remain |
| Network policy creation and dynamic update | `network-policy`, `network-policy-matrix` | Existing-flow behavior and default cluster CIDR isolation |
| OCI image, entrypoint, Dockerfile, mounts, S3 rootfs | `custom-image`, `dockerfile`, `storage-sources` | The pinned SWR OCI fixture passed in isolated standalone; two S3 EROFS cases failed before file/mount assertions at sandboxd image-daemon startup |
| Writable quota, GPU device, runtime config | `storage-quota`, `storage-sources` | Real device worker and runtime-specific configuration behavior |
| API Key, tenant isolation, resource query, CLI | `auth`, `tenant-isolation`, `key-lifecycle`, `sdk-contracts`, `cli-contract` | Isolated admin-create and foreign-DELETE denials passed; the DELETE case used the corrected ADX SDK source, so release-package regression remains |

The first full run also compares Redis ownership before and after all groups.
The 2026-09-23 `akernel` campaign found `Failed` records still holding resource
reservations; the count can change as tests run, so use each run's
`redis-after.json` rather than a fixed number. They must be reconciled before
formal performance or capacity acceptance. Per-run `no-new-held` does not
clear this prerequisite. Deleted records without reservations are terminal
history. Revoked API Key records are permanent revocation tombstones that
prevent bootstrap replay from restoring a revoked key; they are not active
credentials or resource reservations.

## PR78 command and PTY regressions

The standalone IO group requires a short background-command wait to return a running result with `exit_code=None`, `status=RUNNING`, and `error_code=WAIT_TIMEOUT`; a later wait must return the final output and exit code. The PTY group retains 100 completed session wrappers and verifies that completed waits remove them from the manager, then checks that the same Sandbox still serves commands. These use the selected real runtime and do not infer runsc or Kata coverage from a runc run.
