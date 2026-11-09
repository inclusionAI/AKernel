# Production control-plane capacity profile

The core chart's [values-production.yaml](../akernel/charts/core/values-production.yaml)
is an opt-in starting allocation for installations targeting roughly 100,000
running sandboxes and 10,000 active connections. These targets are **not measured
capacity guarantees**. The default values remain suitable for smaller installations;
this profile does not change standalone deployment or provision cloud nodes.

Ingress and API Server run together in each API Pod. Both protocols continue to
share one control plane and worker pool. This profile neither enables E2B nor
changes TLS, credentials, public Services, worker resources or checkpoint storage.

## Allocation and placement

Each row describes the main container; budget separately for collectors, system
services, monitoring, image distribution and workload nodes.

| Component | Replicas | CPU request / limit | Memory request / limit |
| --- | --- | --- | --- |
| Coordinator | 1 | 4 / 8 | 16Gi / 32Gi |
| Ingress + API Server | 4, expandable to 8 | 4 / 8 each | 8Gi / 16Gi each |
| Redis, persistent single instance | 1, no replicas or Sentinel | 4 / 8 | 32Gi / 64Gi |

The four-API profile requests 24 CPU and 80Gi total for these main containers;
eight APIs request 40 CPU and 112Gi. These sums exclude sidecars and node
reservations. Limits allow bursts only where physical capacity remains available.

API Pods have **required hostname anti-affinity**: at least four eligible nodes
are necessary for four replicas, and eight for eight. Zone spread is preferred,
not required. Provision node capacity for eight API replicas and a node failure
before accepting the corresponding traffic. Scaling the replica count does not
provision that capacity, and readiness probes do not qualify connection draining
or client reconnection. The chart updates multi-replica APIs with zero surge and
one unavailable replica; its PDB retains `replicas - 1` during voluntary eviction.

Use site values to select a stable control pool for Coordinator, APIs and Redis;
keep them off autoscaled execution-only nodes. Each role accepts `nodeSelector`,
`tolerations` and `affinity`. Account for Redis PV zone/node constraints, and place
Coordinator and Redis on separate hosts where feasible. A single Coordinator
must remain single-active and uses Recreate; the chart rejects multiple replicas.

## SSD and Redis persistence

The profile requests a **200Gi ReadWriteOnce PVC** and AOF with
`appendfsync=everysec`. It intentionally leaves `storageClassName` empty because
there is no portable SSD StorageClass name. **Set it explicitly to a qualified
SSD-backed cloud-disk class before installation**; the cluster default is not
proof of SSD performance. Verify topology, binding mode, expansion support,
latency/IOPS and reclaim policy for that class.

Single-instance Redis means a Redis/host/zone outage can interrupt the entire
control plane until recovery; this is not HA. `everysec` is not zero-RPO, and AOF
on one volume is not an independent backup. Maintain and test backups separately.
Four requested CPU cores reserve scheduling capacity but do not make one Redis
command execute on four cores. Monitor process RSS, dataset size, memory peaks
and fork copy-on-write during persistence/rewrite; the 32Gi request is not a
Redis dataset cap, and a 64Gi Pod limit is not proof that every rewrite fits.
Preserve metadata rather than introducing an eviction policy to hide pressure.

## Install and render

Prepare protected site values with the actual image/digest, existing TLS/bootstrap
Secret contract, eligible control-pool placement and SSD class. No image,
credential or provider-specific disk class is supplied by this profile.
From the repository root, review the core chart first:

```bash
helm template akernel deploy/akernel/charts/core -n akernel \
  -f deploy/akernel/charts/core/values-production.yaml \
  -f /secure/site-values.yaml
```

For the umbrella chart, values must be nested under `core`. Generate a wrapper
using Python with PyYAML, then merge the site values **last**:

```bash
python3 - <<'PY' > /secure/production-umbrella.yaml
import yaml
with open('deploy/akernel/charts/core/values-production.yaml') as source:
    print(yaml.safe_dump({'core': yaml.safe_load(source)}, sort_keys=False))
PY
helm template akernel deploy/akernel -n akernel \
  -f /secure/production-umbrella.yaml -f /secure/site-umbrella-values.yaml
```

Both cloud Terraform modules also accept `core_values_override_files`, whose files
are **unwrapped core values**. Append the profile and site overrides after the
module-generated values, in that order. Preserve any existing override files;
review the full Terraform plan because guided node sizing is independent of
these Pod requests. Never feed an umbrella `core:` wrapper into this input.

To expand APIs, override `adx.ingressApi.replicas: 8` (or
`core.adx.ingressApi.replicas` in umbrella values) after qualifying the additional
hosts, Redis/Coordinator capacity and connection behavior. This profile does not
install an HPA or change the worker autoscaler's policies.

## Existing-release migration and rollback

This is not a drop-in live upgrade. Save the current release/chart, complete
rendering chain, values, resource identities and tested rollback configuration in
a protected location. Review live credentials and storage before making changes;
never replace them with defaults. Offline `helm template` cannot recover a Redis
password that the chart normally preserves through live Secret lookup.

For an existing Redis StatefulSet, changing `volumeClaimTemplates` size from its
current value to 200Gi is immutable, even if the StorageClass supports expansion.
Do not apply this profile blindly. Use an explicitly reviewed storage migration:
retain the existing claim-template value in site overrides, expand the **actual
bound PVC** through the supported CSI procedure, and verify filesystem expansion
and usable capacity. Keep the template override for subsequent upgrades. If the
class cannot expand or must change, plan an offline backup/restore to new storage
with data validation and retention of the original volume; do not delete the old
PVC to make Helm succeed. Never request a shrink for an existing larger claim.

Drain affected consumers before single-active Coordinator or Redis maintenance,
pause worker scaling actions as appropriate, and preserve scaler state. Review
server-side Helm dry-run and live differences before applying during a maintenance
window. Verify Redis authentication/persistence, API readiness on each host and
actual SDK traffic before resuming scaling and traffic. Existing sessions may
reconnect or be interrupted; test that behavior for the deployed package.

Rollback resource/replica/placement fields using fresh values while preserving
credentials, data and the current PVC size. A storage expansion is not undone by
Helm rollback. Do not start two Coordinators or replay an old release that changes
credentials or immutable storage fields.

## Qualification before claiming target capacity

Measure increasing sandbox inventory separately from connection count and churn.
Exercise both SDKs, command/file/PTY/port streams, reconnects, create/delete bursts,
worker scale-out/in and checkpoint-driven lifecycle load. Include paused instances,
snapshots and metadata retention in Redis sizing. Hold representative steady
traffic and persistence/rewrite cycles; inject API/worker failure and measure the
accepted single-instance Redis/Coordinator interruption and recovery.

Observe Redis command latency/RSS, persistence/fork peaks, disk growth and I/O;
Coordinator reconcile/scheduling latency; per-API active connections, CPU limiting,
memory and errors; host file-descriptor/connection-tracking budgets, LB limits and
network throughput. Set load-specific acceptance limits and verify graceful
connection drain. Rendering tests establish configuration consistency only.
