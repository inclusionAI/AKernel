# cn-north-4 Direct Command baseline — 2026-09-27

## Scope

This baseline measures raw foreground command execution after Sandboxes are
running. The request path is `POST /direct/{sandbox_id}/invoke`, action
`process.exec`, command `true`. Sandbox creation, scheduling, route readiness,
and deletion are excluded from QPS. Every request uses a unique request ID and
must return HTTP 200, exit code zero, and empty stdout and stderr.

All Sandboxes used `runsc`, reserved 1 CPU and 2 GiB, and were pinned to node
`192.168.10.156`. The in-cluster load Job ran away from both the target node and
the Ingress/API Server node. The deployed image was:

```text
swr.cn-north-4.myhuaweicloud.com/openyuanrong/cluster-all-in-one:
akernel-adx-b104-46e0ac5-20260927-r2@
sha256:53225d8d45038451760b7f4c9a27c96ca4dd8151f1beb6b305ae57ddb20160f9
```

## Sustained results

The capacity values below use 3 seconds of warmup, 20 seconds of measurement,
and 5 seconds of recovery between phases. The client remained below two CPU
cores, so these selected points did not reach its four-core limit.

| Sandboxes | Total concurrency | QPS | P50 | P95 | P99 | Client CPU |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4 | 242.55 | 12.17 ms | 39.94 ms | 47.48 ms | 0.04 cores |
| 1 | 32 | 410.30 | 93.59 ms | 158.56 ms | 171.60 ms | 0.09 cores |
| 1 | 64 | **586.60** | 92.27 ms | 269.66 ms | 363.56 ms | 0.14 cores |
| 2 | 8 | 459.90 | 12.68 ms | 55.84 ms | 66.66 ms | 0.07 cores |
| 2 | 64 | 874.25 | 12.61 ms | 268.80 ms | 344.58 ms | 0.19 cores |
| 2 | 128 | **1,051.80** | 95.16 ms | 421.99 ms | 490.12 ms | 0.26 cores |
| 24 | 96 | 3,551.90 | 24.56 ms | 52.93 ms | 65.01 ms | 0.65 cores |
| 24 | 128 | 5,633.60 | 18.95 ms | 52.20 ms | 68.39 ms | 1.11 cores |
| 24 | 168 | 5,731.85 | 24.63 ms | 64.16 ms | 82.49 ms | 1.14 cores |
| 24 | 192 | 7,099.30 | 23.37 ms | 56.12 ms | 76.82 ms | 1.39 cores |
| 24 | 216 | **7,319.70** | 25.98 ms | 59.22 ms | 82.37 ms | 1.45 cores |

The observed same-node aggregate maximum was 7,319.70 commands/s at 24
Sandboxes and total concurrency 216. This is an observed peak for the tested
deployment, not a hardware-independent limit.

Higher concurrency reduced sustained throughput. Separate 20-second overload
checks produced 275.05 QPS for one Sandbox at concurrency 128, 523.05 QPS for
two at concurrency 256, and 4,162.05 QPS for 24 at concurrency 256. The practical
aggregate knee is therefore between concurrency 216 and 256. For latency-sensitive
use, the low-concurrency rows are more representative than the maximum-QPS rows.

## Short sweep

An initial 8-second sweep established the scaling search range. Its per-size
observed peaks were:

| Sandboxes | Concurrency | QPS | P99 |
|---:|---:|---:|---:|
| 1 | 8 | 448.25 | 77.00 ms |
| 2 | 16 | 1,074.62 | 77.52 ms |
| 4 | 32 | 2,187.62 | 78.12 ms |
| 8 | 64 | 3,619.62 | 86.04 ms |
| 16 | 128 | 6,141.75 | 80.68 ms |
| 24 | 128 | 7,398.88 | 46.92 ms |

These values are exploratory burst points. Repeated high-concurrency runs showed
that short samples can overstate sustainable capacity, especially for one or
two Sandboxes.

## Validation and limits

- All selected phases completed with zero command failures.
- Redis showed no newly held resources after each run.
- Persisted assignments proved that every created Sandbox ran on
  `192.168.10.156`.
- The public instance directory was empty after cleanup, and no benchmark Job
  or ConfigMap remained.
- Kubernetes Metrics API was unavailable. Server and target-node CPU could not
  be correlated with each phase; only generator CPU and the throughput/latency
  curve are available. Do not claim a CPU root cause from this baseline.
- The cluster showed material short-window variation. Capacity regression gates
  should repeat the 20-second selected points and compare medians before fixing
  a numeric threshold.

## Layered diagnosis

`process.exec("true")` is not an ingress-only request. Every call creates and
reaps a guest process and records its result in RRT. A Direct RRT health run was
therefore added to retain the same ingress, route lookup, Node Proxy, and RRT
HTTP path while removing EXECD and process creation.

| Path | Sandboxes | Generators | Concurrency | QPS | P99 | Generator CPU |
|---|---:|---:|---:|---:|---:|---:|
| `process.exec("true")` | 1 | 1 | 64 | 586.60 | 363.56 ms | 0.14 cores |
| RRT `healthz` | 1 | 4 | 32 | 14,886.94 | 52.18 ms | 1.85 cores |
| `process.exec("true")` | 24 | 1 | 216 | 7,319.70 | 82.37 ms | 1.45 cores |
| RRT `healthz` | 24 | 4 | 256 | 28,886.89 | 39.16 ms | 3.98 cores |
| RRT `healthz` | 24 | 8 | 384 | **44,989.83** | 26.85 ms | 7.49 cores |

At 8 generator processes, increasing concurrency to 504 and 648 reduced health
throughput to 42,469.14 and 41,462.00 QPS. The current lightweight routed-path
knee is therefore about 45k QPS for this deployment. It matches the earlier
multi-process routed-request range and shows that the 7.3k command result is
not an API ingress ceiling. The one-Sandbox command ceiling belongs primarily
to guest process execution; the 24-Sandbox aggregate additionally approaches
shared routing and node-side limits.

Primary evidence:

- `out/ci/direct-command/cn4-sustained-knee-20260927/summary.json`
- `out/ci/direct-command/cn4-node-peak-20260927/summary.json`
- `out/ci/direct-command/cn4-peak-confirm-20260927/summary.json`
- `out/ci/direct-command/cn4-full-20260927/summary.json`
- `out/ci/direct-command-layering/cn4-health/summary.json`
- `out/ci/direct-command-layering/cn4-health-mp4/summary.json`
- `out/ci/direct-command-layering/cn4-health-mp8/summary.json`
