# cn-north-4 Create throughput baseline — 2026-09-27

This baseline measures a public Create request from the instant before the HTTP
request until the SSE `final` event reports `running`. Route validation, Delete,
and resource-release audit are mandatory gates, but remain outside the measured
Create interval.

It answers two separate questions:

- pinned placement: how many Sandboxes one Node Manager and runtime host can
  bring to `running`;
- automatic placement: how the complete API Server, Coordinator,
  ShardScheduler, Node Manager, sandboxd, RRT, persistence, and route publication
  chain scales across the deployed workers.

The tests used warm `runsc` runtime artifacts, 100 millicores and 128 MiB per
Sandbox. They ran from an in-cluster Job against the ClusterIP API endpoint.
Each phase started with no benchmark Sandbox and deleted every created Sandbox
before the next phase.

## Environment

- namespace: `akernel`
- Kubernetes: v1.33.5
- workers with ADX Node Manager: `192.168.10.48`, `192.168.10.156`,
  `192.168.10.179`, `192.168.10.192`
- pinned worker: `192.168.10.156`
- runtime: `runsc`
- placement: hard node affinity or automatic ShardScheduler placement
- client load Pod: 4 CPU limit, scheduled away from the API Server and pinned
  runtime worker where applicable

The cluster did not expose the Kubernetes Metrics API. Consequently this run
does not report client or API Server CPU utilization. A capacity/SLA run must
add process or cgroup resource sampling before using the results for sizing.

## Pinned single-worker result

Each phase created 32 Sandboxes.

| Concurrency | Successful | Create/s | P50 | P95 | P99 | Batch makespan |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 32/32 | 1.285 | 795.953 ms | 863.050 ms | 895.911 ms | 24.912 s |
| 2 | 32/32 | 2.527 | 791.046 ms | 933.948 ms | 992.190 ms | 12.662 s |
| 4 | 32/32 | 4.431 | 823.106 ms | 1085.404 ms | 1087.947 ms | 7.223 s |
| 8 | 32/32 | 6.972 | 1067.253 ms | 1239.742 ms | 1326.165 ms | 4.590 s |
| 16 | 32/32 | 8.927 | 1410.015 ms | 2307.771 ms | 2309.519 ms | 3.585 s |

Throughput still increases at concurrency 16, while P99 has risen to 2.31 s.
This is the highest measured single-worker point, not a declared capacity.

## Automatic four-worker result

Each phase created 64 Sandboxes. The persisted assignments were evenly split:
16 Sandboxes on each of the four Node Managers in every phase.

| Concurrency | Successful | Create/s | P50 | P95 | P99 | Batch makespan |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 64/64 | 4.768 | 838.786 ms | 913.990 ms | 972.065 ms | 13.422 s |
| 8 | 64/64 | 8.906 | 847.981 ms | 993.313 ms | 1042.822 ms | 7.186 s |
| 16 | 64/64 | 14.861 | 963.429 ms | 1163.839 ms | 1186.496 ms | 4.307 s |
| 32 | 64/64 | 21.070 | 1258.773 ms | 1499.403 ms | 1635.204 ms | 3.038 s |
| 64 | 128/128 | 34.361 | 1494.282 ms | 2042.593 ms | 2258.368 ms | 3.725 s |
| 128 | 128/128 | 49.806 | 2005.946 ms | 2292.656 ms | 2415.881 ms | 2.570 s |
| 256 | 256/256 | 58.888 | 3065.437 ms | 3821.161 ms | 3868.934 ms | 4.347 s |

All phases completed without platform errors. Increasing concurrency from 128
to 256 improved throughput by about 18%, while P99 increased by about 60%.
This places the practical warm-Create saturation region around 50–60 Create/s
for this four-worker deployment. It is an observed operating region rather than
a product capacity guarantee; a fixed-arrival-rate test is still required.

## Correctness gates

Both runs passed all of the following gates:

- every accepted request reached the `running` SSE final state;
- every created Sandbox passed the routed `/direct/<id>/healthz` check;
- every Delete completed;
- the public instance directory returned to its pre-run count;
- Redis assignment records matched the requested topology;
- all persisted records reported `resources_held=false` after cleanup;
- the benchmark Job and ConfigMap were removed.

Evidence is stored under `out/ci/create-throughput/cn4-matrix/`,
`out/ci/create-throughput/cn4-cluster/`,
`out/ci/create-throughput/cn4-high-concurrency/`, and
`out/ci/create-throughput/cn4-c256/`.

## Remaining capacity tests

Closed-loop concurrency cannot demonstrate overload behavior because the
client waits before issuing more requests. The next capacity run must use a
fixed arrival rate below, around, and above the observed 50–60 Create/s region and
report submitted, queued, timed out, rejected, running, and cleaned counts.
Repeat the selected rates for at least 60 seconds, then add:

1. cold OCI/rootfs cache versus this warm-runtime baseline;
2. Create/Delete churn while existing Sandboxes serve Direct Command traffic;
3. resource sampling for API Server, Coordinator, Node Manager, sandboxd, Redis,
   and load generator;
4. per-stage latency or trace evidence for admission, scheduling, runtime
   startup, RRT readiness, persistence, and route publication.
