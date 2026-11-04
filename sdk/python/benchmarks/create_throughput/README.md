# Sandbox Create throughput

This benchmark measures completed Sandbox creation, not HTTP acceptance alone.
The measured interval starts before the Create request and ends when its SSE
`final` event reports `running`. Direct `healthz` validation, deletion, and the
wait for an empty instance directory happen outside that interval and remain
release gates.

Each concurrency phase creates a fixed-size batch and reports:

- successful creates divided by batch makespan;
- per-request P50, P95, and P99 Create latency;
- HTTP/Create failures, route-readiness failures, and cleanup failures;
- reported and persisted node placement;
- residual instances and persisted resource ownership after cleanup.

## Performance objective

The warm single-worker release target is:

- concurrency 1: Create P99 below 200 ms;
- concurrency 100: zero Create, route, and cleanup errors, with Create P99 no
  higher than 120% of the concurrency-1 P99;
- concurrency 128 and above: exploratory overload points, not release targets.

Create still ends only when the SSE `final` event reports `running`; the target
does not weaken runtime readiness, local route binding, or durable state
publication. Record a result as a passing baseline only when every request and
all cleanup/storage audits succeed.

## Topologies

Hard-pin one node to measure node-local admission, sandboxd/runtime startup,
RRT readiness, and route publication without placement variation:

```bash
sdk/python/benchmarks/create_throughput/run_k8s.sh \
  --kubeconfig "$KUBECONFIG" \
  --target-node 192.168.10.156 \
  --batch-size 128 \
  --concurrency 1,8,32,64,100,128 \
  --cpu 100 \
  --memory 128 \
  --cpu-limit 1000 \
  --memory-limit 2048
```

Omit `--target-node` to measure automatic placement and cluster-wide scaling:

```bash
sdk/python/benchmarks/create_throughput/run_k8s.sh \
  --kubeconfig "$KUBECONFIG" \
  --batch-size 64 \
  --concurrency 4,8,16,32
```

Use a runtime/rootfs already present on every target node for the warm Create
baseline. Image distribution and cold-cache creation are separate cases and
must record cache state and transferred bytes.

The request values (`--cpu`, `--memory`) drive admission and scheduling. The
limit values (`--cpu-limit`, `--memory-limit`) configure the runtime cgroup.
Keep them explicit in performance baselines: using a 100 millicore request as
the runtime limit measures startup under CPU throttling rather than scheduler
capacity. A zero limit preserves the public API's default behavior.

Each load worker reuses one HTTPS connection across its closed-loop requests,
matching a long-lived SDK client. TLS handshake cost is therefore paid once per
worker and is excluded from steady-state Create latency after warm-up; measure
connection establishment separately when it is part of the product objective.

The Kubernetes wrapper audits only the Environment IDs created by the current
run with Redis `HMGET`. It does not copy the complete control hash, so retained
Deleted records do not make benchmark startup or evidence collection depend on
the total cluster history.

## Capacity stages

1. Run the closed-loop matrix above to locate useful concurrency and obvious
   saturation.
2. Repeat selected points for at least 60 seconds or multiple fixed batches.
3. Add fixed arrival-rate phases below, at, and above the selected throughput.
   Report scheduled, admitted, queued, timed out, and completed requests rather
   than hiding overload behind client concurrency.
4. Run mixed Create/Delete and steady Direct Command traffic to verify that
   lifecycle churn does not starve existing Sandboxes.

Do not compare this result directly with Edge `healthz` or Direct Command QPS.
Create includes admission, scheduling, sandboxd/runtime startup, RRT readiness,
state persistence, and route publication.
