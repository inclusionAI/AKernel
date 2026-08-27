# Harbor Deep-SWE checkpoint/restore E2E

This is a manual acceptance test for restoring a real, active developer
workload. It complements the small checkpoint/restore integration test in
`sdk/python/tests/integration/test_sandbox.py`; it is not part of the default
CI suite.

## Scope

The primary case runs Harbor inside a Firecracker AKernel sandbox. Harbor then
uses nested Docker to run one Deep-SWE task. The outer sandbox is checkpointed
while the oracle is paused inside the task container, restored from that
checkpoint, and allowed to finish.

Use tracked source revisions for Harbor and Deep-SWE and record them with the
test result. The initial fixture is:

- Deep-SWE task: `tasks/abs-stepped-slices`
- agent: `oracle`
- outer runtime: `firecracker`
- outer resources: 1 vCPU, 4 GiB memory, and 8 GiB writable storage
- inner task resources: 1 vCPU and 2 GiB memory

The Firecracker guest must support nested Docker, including overlay or
fuse-overlayfs, cgroups, bridge networking, conntrack, iptables, and nftables.
Use a prebuilt guest kernel containing those features when the bundled kernel
does not provide them.

## Fixture preparation

Work on copies of both repositories. Do not modify the source checkouts used
to record their revisions.

1. Change every task phase to the same `public` network mode. AKernel network
   policy is fixed when the outer sandbox is created.
2. Reduce both Deep-SWE task environments to the inner resource values above.
3. Add a deterministic barrier to the oracle solution before it applies the
   solution patch:

   ```bash
   mkdir -p /logs/agent
   touch /logs/agent/checkpoint-ready
   while [ ! -f /logs/agent/checkpoint-release ]; do
       sleep 1
   done
   ```

4. Configure nested Docker with a non-conflicting bridge and address pool.
   Validate `docker info`, `docker compose version`, and the selected runtime
   before starting Harbor.
5. Pull and inspect the Deep-SWE image before the timed workload begins. A
   cold image pull must not be confused with checkpoint or restore latency.

Harbor must be able to execute commands in its nested Docker containers on the
selected guest kernel. Treat an execution workaround as a Harbor or guest
kernel prerequisite; do not patch sandboxd or the YuanRong control plane in
the acceptance run.

## Test flow

Create the outer sandbox with explicit request and limit values:

```python
source = Sandbox(
    runtime="firecracker",
    cpu=1000,
    cpu_limit=1000,
    memory=4096,
    mem_limit=4096,
    storage_mb=8192,
    idle_timeout=14400,
    schedule_timeout=120,
)
```

Inside it, start nested Docker as a background command. Start Harbor with the
oracle agent and the prepared task as a second background command. Wait until
`checkpoint-ready` exists, then record all of the following values:

- outer sandbox ID
- nested dockerd PID
- Harbor PID
- running Deep-SWE container ID
- absolute `checkpoint-ready` path
- contents of a caller-created filesystem nonce

Flush filesystem state, create a live checkpoint with
`leave_running=True`, and assert that the source still accepts commands.
Restore the checkpoint and wait until the restored sandbox accepts commands.
Only then terminate the source sandbox.

The restored sandbox must have a new outer ID. The other recorded values must
be byte-for-byte equal before and after restore. Also require `docker info` to
succeed before releasing the barrier. These checks prove that restore
continued the existing dockerd, Harbor, and task-container processes instead
of recreating the application from files.

Touch `checkpoint-release` in the restored sandbox and wait for Harbor to
finish. The test passes only when all of these conditions hold:

- Harbor exits with status zero.
- Its job result reports one completed trial and zero errored trials.
- The Deep-SWE reward is exactly 1.
- F2P and P2P both report all six tests passing.
- `artifacts/model.patch` exists and is non-empty.

Always download Harbor results before cleanup. On failure, also capture the
Harbor log, nested dockerd log, container list, result JSON files, and any
verifier output. Terminate both outer sandboxes and explicitly delete the
checkpoint in a `finally` path.

## Direct Harbor AKernel backend check

Run a second, smaller check directly from Harbor through its AKernel backend.
It validates the supported image-based path independently of nested Docker.

Use `runsc` for the supplied Deep-SWE OCI image. Firecracker intentionally
rejects an OCI image root and requires a prebuilt EROFS image distributed
through AKernel's image-file path. Use Harbor's shared verifier mode because
the AKernel backend launches prebuilt images and does not build the separate
`tests/Dockerfile`.

After applying the same uniform network policy to a task copy and setting
`[verifier].environment_mode = "shared"`, run:

```bash
harbor run \
  --path /path/to/abs-stepped-slices-copy \
  --agent oracle \
  --env akernel \
  --environment-kwarg runtime=runsc \
  --override-cpus 1 \
  --override-memory-mb 4096 \
  --override-storage-mb 8192 \
  --n-concurrent 1 \
  --yes
```

The direct check has the same reward, F2P, P2P, artifact, and error-count
acceptance criteria. A first cold OCI import may exceed a control-plane
deadline; report that separately and do not count an unobserved background
creation as a passing trial.
