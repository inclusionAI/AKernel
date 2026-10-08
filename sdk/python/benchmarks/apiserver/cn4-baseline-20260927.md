# cn-north-4 API Server baseline — 2026-09-27

## Scope

This run measures steady raw HTTP read throughput. It does not use either SDK
and does not create a Sandbox. The lifecycle create path is intentionally kept
out of this baseline because admission, scheduling, sandboxd, runtime startup,
RRT readiness, route publication, and cleanup would dominate the result.

The tested deployment had one combined Ingress/API Server Pod with a 1 CPU and
1 GiB limit. The load Pod requested 2 CPUs, could use up to 4 CPUs, and was
scheduled on a different Kubernetes node. Requests used persistent HTTP/1.1
connections. Authenticated cases used one administrator key and a two-second
warmup, so the figures represent the warm authentication-cache path.

Deployment inputs:

- AKernel revision: `41f9e2bfad3612bf1f5137e460c2cd1b8bb2a858`
- ADX Buildkite: `#104`, revision `408ac8712773656710a8f3c9270c5608f0588d3f`
- Image digest: `sha256:53225d8d45038451760b7f4c9a27c96ca4dd8151f1beb6b305ae57ddb20160f9`
- Namespace: `akernel`
- API Server node: `192.168.10.179`
- Concurrency: `1,4,16,32,64,128`
- Per level: 2 seconds warmup and 10 seconds measurement

Current response cardinality was four resource items, an empty instance list,
and an empty scheduling queue. List throughput must be measured again with
representative instance and queue sizes before it is used for capacity planning.

## Full sweep

All requests returned a valid response and all 24 phases had zero failures.

| Case | C1 | C4 | C16 | C32 | C64 | C128 |
|---|---:|---:|---:|---:|---:|---:|
| health | 11,013 | 16,547 | **16,942** | 16,219 | 15,694 | 15,326 |
| resources | 6,144 | **12,388** | 11,890 | 10,368 | 6,277 | 256 |
| instances | 1,501 | 1,816 | **1,818** | 1,511 | 865 | 95 |
| scheduling queue | 3,842 | 9,485 | **11,925** | 11,167 | 7,595 | 279 |

Values are successful requests per second. At concurrency 128 the load process
used its complete 4 CPU limit for all three business cases. Those collapsed
figures are generator-limited and are not API Server capacity results.

## Peak confirmation

Each effective peak was repeated for 20 seconds after a two-second warmup.

| Case | Concurrency | RPS | P99 | Client cores | Pod CPU seconds / observed window | Pod memory peak |
|---|---:|---:|---:|---:|---:|---:|
| health | 16 | 16,148.55 | 2.454 ms | 1.350 | 1.904 / 27.220 s | 146.22 MiB |
| resources | 4 | 12,193.70 | 0.585 ms | 1.280 | 14.712 / 29.588 s | 159.80 MiB |
| instances | 4 | 1,773.30 | 3.256 ms | 0.157 | 21.910 / 29.582 s | 170.03 MiB |
| scheduling queue | 16 | 12,082.85 | 2.766 ms | 1.493 | 18.049 / 29.612 s | 170.03 MiB |

The cgroup observation window includes Kubernetes Job scheduling and startup,
so its average CPU is a conservative lower bound during active load. Comparing
CPU seconds with the 22-second active phase shows that `instances` reaches the
single-CPU deployment limit. `resources` and `scheduling queue` do not prove a
pure CPU limit and need profiling before an implementation bottleneck is named.

## Interpretation and follow-up

- Use `resources` at about 12.2k RPS and `scheduling queue` at about 12.1k RPS
  as current single-replica warm-cache baselines, not release SLOs.
- Use `instances` at about 1.77k RPS only for an empty directory. Repeat with
  representative 100, 1,000, and 10,000 visible entries.
- Do not use the concurrency-128 values. A future scale test needs multiple
  load Pods or a compiled asynchronous client and must report achieved client
  CPU and connection counts.
- Before setting a regression threshold, repeat the same immutable image at
  least three times and compare medians and variation on a quiet cluster.
- Creation throughput, mixed lifecycle load, and multiple API Server replicas
  remain separate scenarios.

After the run the API Server Pod was Ready with zero restarts, and the temporary
benchmark Jobs and ConfigMaps were removed.
