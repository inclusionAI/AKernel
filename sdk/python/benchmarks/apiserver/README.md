# API Server raw HTTP throughput

This benchmark measures the deployed API Server over raw persistent HTTP/1.1,
without the AKernel or ADX SDK. It is a closed-loop throughput test: each worker
keeps one connection and sends the next request after the previous response.

The Kubernetes runner creates a temporary ConfigMap and one Job per case. Jobs
use the deployed image, are placed away from the Ingress/API Server Pod, and
read the API key directly from a Kubernetes Secret. The key is not included in
the Job arguments, logs, manifests, or result JSON.

The default cases separate four costs:

| Case | Path | Measures |
|---|---|---|
| `health` | Pod health port `/healthz` | generator, cluster network, and HTTP server ceiling |
| `resources` | `/api/sandbox/v1/resources` | TLS, bearer authentication, API routing, and cached node-directory serialization |
| `instances` | `/api/instances` | TLS, bearer authentication, and cached ownership-directory reads |
| `scheduling-queue` | `/global-scheduler/scheduling_queue` | TLS/auth plus API Server to Coordinator RPC |

These results do not represent Sandbox creation throughput. Lifecycle creation
also includes admission, scheduling, sandboxd, runtime startup, RRT readiness,
route publication, and cleanup, and must be measured as a separate workload.

Run against cn-north-4:

```bash
sdk/python/benchmarks/apiserver/run_k8s.sh \
  --kubeconfig /Users/robb/.cache/yr-huawei-clusters/active-cce.kubeconfig \
  --namespace akernel \
  --output-dir out/performance/apiserver-cn4
```

Defaults are concurrency `1,4,16,32,64,128`, two seconds of warmup, and ten
seconds of measurement per level. The runner captures the Deployment, server
and client Pod manifests, client logs, result JSON, and server cgroup snapshots.
Temporary Jobs and the ConfigMap are removed unless `--keep` is specified.
Use `--cases resources,instances` to select cases. The aggregate result also
reports API Server Pod CPU and memory across each selected case window.

Driver tests do not require Kubernetes:

```bash
PYTHONPATH=sdk/python python3 -m unittest discover \
  -s sdk/python/benchmarks/apiserver/tests -v
```

The first cn-north-4 baseline is recorded in
[`cn4-baseline-20260927.md`](./cn4-baseline-20260927.md).
