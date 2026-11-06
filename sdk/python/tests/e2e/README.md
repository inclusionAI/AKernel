# SDK end-to-end tests

Run these tests against a real deployment. Unit tests remain under `tests/unit/`;
E2E directories separate deployment requirements:

| Level | Environment | Groups |
| --- | --- | --- |
| L0 | One ready node; fast public SDK smoke | `sdk-contracts` |
| Standalone | One Linux node, configured runtime and reachable image fixtures | `direct-ingress`, `lifecycle`, `auth`, `cli-contract`, `io`, `pty-close`, `network-url`, `network-tunnel`, `network-tunnel-block`, `network-policy`, `runtime-integration`, `storage-quota`, `network-policy-matrix` |
| Multi-VM | At least two registered workers | `node-placement` |
| Full | Multi-worker deployment with storage/device/admin/fault fixtures as selected | `redis-lifecycle`, `placement-affinity`, `admission`, `concurrent-claim`, `unknown-create-result`, `create-transport-disconnect`, `custom-image`, `dockerfile`, `port-forward`, `storage-sources`, `tenant-isolation`, `key-lifecycle`, `scheduler-admin`, `scheduler-maintenance`, `fault-runc-delete-retry`, `fault-process-restart`, `fault-node-loss`, `fault-checkpoint-shared`, `fault-checkpoint-local` |

The runner includes lower levels. `standalone/`, `multi_vm/` and `full/` contain
functional cases; `fault/` contains opt-in destructive cases. The existing
`tests/integration/` suite includes runtime-heavy checkpoint/tunnel coverage.
A public SDK Full run is not the entire platform deployment acceptance suite.
Most cases request 1 CPU / 2 GiB per Sandbox; concurrent fixtures need capacity
for multiple instances.

## Run

Install the SDK from this checkout and configure its
[connection environment](../../README.md). From `sdk/python/`:

```bash
export AKERNEL_RUN_INTEGRATION=1
export AKERNEL_TEST_RUNTIME=runsc
# Configure AKERNEL_SERVER_ADDRESS, AKERNEL_TOKEN and, if needed,
# AKERNEL_GATEWAY_ADDRESS for the test deployment.
python -m tests.e2e.run --level l0 --output out/e2e/l0
python -m tests.e2e.run --level standalone --output out/e2e/standalone
python -m tests.e2e.run --level multi-vm --output out/e2e/multi-vm
python -m tests.e2e.run --level full --require-clean-redis --output out/e2e/full
python -m tests.e2e.run --level standalone --case direct-ingress --case io \
  --output out/e2e/selected
```

Each group runs in a separate process and writes its own log. `summary.json`
records status, elapsed time, test count, skips and Redis audit. Groups continue
after a prior group fails. A skip or `partial` result does not prove coverage.
Tests use unique names and context managers/finally cleanup.

The default total budget is 10,800 seconds, with shorter per-group limits.
Use `--budget-seconds` to change it. Multiple serial invocations can share a
`--campaign-root`; each output must be a unique direct child of that root.
Earlier E2E/pressure summary durations consume the same campaign budget.
Collect the first pass before rerunning affected groups.

## Images and public ingress

The node must be able to pull the selected images, including private-registry
credentials. Set `AKERNEL_TEST_IMAGE` for OCI and custom-image tests;
`AKERNEL_TEST_DOCKERFILE_IMAGE` selects the Dockerfile base image.
`AKERNEL_TEST_HTTP_IMAGE` supplies a Python 3 HTTP-server image.

`direct-ingress` checks HTTPS resources authentication (401 without a key,
200 with a key), real PTY output/resize/exit status, and guest HTTP traffic
through the separate data listener. Its default HTTP image is `python:3.12-slim`.
PR CI includes this group through standalone discovery.

`port-forward` checks actual payloads, same-port instance isolation and route
withdrawal after deletion. Its Host-header case additionally requires
`AKERNEL_TEST_PORT_HOST_DOMAIN` to match the configured ADX port-host domain;
it does not configure public wildcard DNS or certificates.

The `network-tunnel-block` standalone group covers default WS/HTTP 8765/8766 and
custom 18765/18766 listeners with creation-time `NetworkPolicy.block()` and live
block/clear/reblock transitions, including consecutive updates before waiting
for tunnel recovery. It uses the same public tunnel handle throughout and
requires each probe to recover within 10 seconds after a policy replacement.
It also checks command/file/PTY/published-port access under block and verifies
that a bounded ordinary outbound TCP attempt is denied. Normal standalone
inventory and teardown audits apply to these cases too.

## Fixture requirements

| Groups | Additional configuration |
| --- | --- |
| `redis-lifecycle`, `placement-affinity` | Test Redis access: `AKERNEL_REDIS_HOST`, optional `AKERNEL_REDIS_PORT`, `AKERNEL_REDIS_PASSWORD`, `AKERNEL_REDIS_NAMESPACE` (default `akernel`) |
| `node-placement` | At least two live workers; explicit pins must execute on different nodes |
| Runtime affinity in `placement-affinity` | One capable and one incompatible live worker with persisted runtime inventory; optional `AKERNEL_TEST_RUNTIME_CLASS`, `AKERNEL_TEST_RUNTIME_NODE_ID`, `AKERNEL_TEST_RUNTIME_INCOMPATIBLE_NODE_ID`, `AKERNEL_TEST_RUNTIME_IMAGE`; `AKERNEL_REQUIRE_RUNTIME_AFFINITY=1` makes missing capability fail instead of skip |
| `concurrent-claim` | Isolated Redis/deployment and `AKERNEL_TEST_DESTRUCTIVE_CLAIMS=1` |
| `storage-sources` | OCI/mount/entrypoint fixtures: `AKERNEL_TEST_IMAGE`, `AKERNEL_TEST_MOUNT_IMAGE`, `AKERNEL_TEST_ENTRYPOINT_IMAGE`; S3 fixtures: `AKERNEL_S3_ENDPOINT`, `AKERNEL_S3_BUCKET`, `AKERNEL_S3_ROOTFS_OBJECT`, `AKERNEL_S3_MOUNT_OBJECT`, optional `AKERNEL_S3_ACCESS_KEY`/`AKERNEL_S3_SECRET_KEY`; real GPU: `AKERNEL_TEST_GPU_XPU` |
| `tenant-isolation` | `AKERNEL_SECOND_TENANT_TOKEN`, or administrator key with `AKERNEL_TEST_MANAGE_KEYS=1` to create a temporary foreign key |
| `key-lifecycle`, `scheduler-admin` | Administrator key and `AKERNEL_TEST_MANAGE_KEYS=1`; creates and removes temporary tenant keys |
| `scheduler-maintenance` | Isolated worker, `AKERNEL_TEST_SCHEDULER_MAINTENANCE=1` and `AKERNEL_TEST_SCHEDULER_NODE_ID`; pauses/resumes new scheduling while checking the existing instance remains usable |

Kata/Firecracker need a KVM-capable worker and separate runs with
`AKERNEL_TEST_RUNTIME`. Missing images, storage or hardware produce skips, not
successful coverage. Run checkpoint cases only on supporting runtimes.

## Redis and cleanup

When `AKERNEL_REDIS_HOST` is configured, the runner records read-only
`redis-before.json` and `redis-after.json`. New held reservations, existing
reservations newly entering Failed, or unavailable audits fail the run.
`no-new-held` does not mean the deployment was initially clean.
`--require-clean-redis` requires Redis access and a clean reservation ledger
both before and after the run. State records and held resource reservations
are reported separately; also inspect actual runtime backends after faults.

`unknown-create-result` and `create-transport-disconnect` exercise retries with
the same request/environment identity. The latter drops a real TCP response
after confirmed creation; it is distinct from merely discarding a return value.
These cases run their own isolated transport fixtures.

## Faults

Fault groups require an isolated deployment and
`AKERNEL_TEST_DESTRUCTIVE_FAULTS=1`. Hooks must be absolute executable paths;
they receive the target node ID and must verify the requested operation.

- `fault-runc-delete-retry`: use an otherwise empty, task-owned standalone Docker container with native runc enabled. Set `AKERNEL_TEST_CONTAINER`, its full `AKERNEL_TEST_CONTAINER_ID`, direct test Redis access and `AKERNEL_TEST_FAULT_OUTPUT`. The case atomically installs a default-pass launcher backed by an unchanged copy of the real runc binary. A five-second token fails only the next exact `delete --force` for the selected physical sandbox and original sandboxd process; it is consumed before returning the injected error. Other commands, subsequent deletion attempts and restarted daemons use real runc normally, with no test-process restoration required. Retention is captured at the following real deletion attempt, then the case only polls physical/Redis cleanup without another public delete. Final catalog retirement is recorded separately. Evidence is written even when cleanup raises. Destroy the exclusive fixture after this group; its launcher and audit files remain available for inspection. Run only this group with `--case fault-runc-delete-retry` and at least 120 seconds of remaining campaign budget.
- `fault-process-restart`: set `AKERNEL_TEST_FAULT_NODE_ID`,
  `AKERNEL_TEST_RESTART_SANDBOXD` and/or `AKERNEL_TEST_RESTART_NODE_MANAGER`.
  Hooks return after a new process and working RPC are confirmed; node recovery
  also needs reconciliation readiness. For sandboxd, let the instance run
  stably before SIGKILL. Keep that wait and daemon recovery within the hook's
  90-second budget. Graceful shutdown may deliberately delete backends.
  The case checks the original ID, marker file and command access.
- `fault-node-loss`: set distinct `AKERNEL_TEST_FAULT_NODE_ID` and
  `AKERNEL_TEST_HEALTHY_NODE_ID`, plus `AKERNEL_TEST_STOP_WORKER` and
  `AKERNEL_TEST_RECOVER_WORKER`. The loss must exceed the heartbeat deadline;
  recovery runs in `finally`. No checkpoint must produce a terminal Failed
  result. Graceful Pod deletion is a different scenario.
- `fault-checkpoint-shared` / `fault-checkpoint-local`: use the same two-worker
  hooks and Redis access. Set `AKERNEL_TEST_CHECKPOINT_STORAGE=shared` with
  `AKERNEL_TEST_SHARED_CHECKPOINT_ALIAS`, or `local` on a separate local-store
  deployment. `AKERNEL_TEST_CHECKPOINT_IMAGE` must support the default curl
  client, or select `AKERNEL_TEST_CHECKPOINT_CLIENT=python` with a Python image.
  `AKERNEL_TEST_CHECKPOINT_SOCKET` defaults to `/run/akernel/execd.sock`.
  Shared recovery must retain the logical ID, advance generation and restore
  checkpoint-era contents on another node; local-only must fail on the lost
  owner and release its reservation. A hard node pin prevents cross-node recovery.

Restore scheduling and workers, delete owned resources, and audit the ledger
on failure as well as success. Save version-specific results and evidence in
the run output directory, rather than treating old runs as current coverage.
