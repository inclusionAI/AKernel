# Direct Command throughput

This benchmark measures foreground no-op command execution after Sandboxes are
running. The measured request is the same raw route preferred by
`adx-sandbox`:

```text
POST /direct/{sandbox_id}/invoke
{"action":"process.exec","args":{"cmd":"true",...},"requestId":"..."}
```

Every request carries a unique request ID. This is required because EXECD
deduplicates retries; reusing one ID would measure result replay rather than new
command execution. A successful sample must return HTTP 200, exit code zero,
and empty stdout and stderr.

Sandbox creation, route readiness, and deletion are outside the QPS window.
They are still part of the runner so it can pin every Sandbox to one node and
clean up all resources after the sweep. The runner compares the Redis inventory
before and after the run, rejects newly held resources, and verifies every
created Sandbox's persisted assignment against the requested node.

## Matrix

The default run incrementally creates 1, 2, 4, 8, 16, and 24 Sandboxes on one
node. For each size `N`, it measures total closed-loop concurrency `N`, `2N`,
`4N`, and `8N`, capped at 128. Each worker keeps its client-to-Ingress HTTP/1.1
connection alive; Ingress still establishes the platform's normal per-command
Direct route to EXECD.

Each Sandbox reserves 1 CPU and 2 GiB by default. Select a target node with
enough free resources for the requested largest size. The load Job is forced
away from both the target node and the Ingress/API Server node.

```bash
sdk/python/benchmarks/direct_command/run_k8s.sh \
  --kubeconfig /path/to/kubeconfig \
  --target-node 192.168.10.156
```

For a contract smoke before a full sweep:

```bash
sdk/python/benchmarks/direct_command/run_k8s.sh \
  --kubeconfig /path/to/kubeconfig \
  --target-node 192.168.10.156 \
  --sandbox-counts 1 \
  --concurrency-factors 1,2 \
  --max-concurrency 2 \
  --warmup-seconds 0.5 \
  --duration-seconds 1
```

Use `--operation health` to keep the same Edge, Node Proxy, and RRT route while
removing EXECD and guest process creation from the measured work. Use multiple
generator processes when establishing an ingress ceiling; one Python process
can become the first bottleneck:

```bash
sdk/python/benchmarks/direct_command/run_k8s.sh \
  --kubeconfig "$KUBECONFIG" \
  --target-node "$NODE" \
  --operation health \
  --generator-processes 8 \
  --load-cpu 8 \
  --sandbox-counts 24 \
  --concurrency-factors 16,21,27 \
  --max-concurrency 648
```

Evidence is written under `out/performance/direct-command-*`. `summary.json`
contains every phase, per-Sandbox successful counts, total QPS, latency,
generator CPU, cleanup state, placement proof, and Redis resource audit.
`resource-samples.tsv` records the load, Ingress/API Server, and target Node Pod
usage when Metrics Server is available.

The highest QPS in one sweep is an exploratory saturation point. Treat it as a
node capacity baseline only when the error rate is zero, P99 remains acceptable,
the generator is not at its configured CPU limit, and a longer repeat at the selected
concurrency is stable. Otherwise add load processes or use a compiled client before
attributing the ceiling to ADX.

The runner waits two seconds after each phase by default. Use a longer
`--cooldown-seconds` for sustained saturation work so a prior overload phase
does not immediately feed the next measurement.
