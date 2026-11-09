# Control-plane capacity and placement

Use the core chart's [values-production.yaml](../akernel/charts/core/values-production.yaml)
as an **opt-in sizing example** for larger installations. The values file owns
the resource allocations; this guide owns deployment, placement and migration.
The profile is not a measured sandbox-count or connection-count capacity guarantee.
It leaves the lightweight defaults, runtime configuration and worker pool unchanged.

## Resource budget and placement

| Main container | Replicas | CPU request / limit | Memory request / limit |
| --- | --- | --- | --- |
| Coordinator | 1 | 4 / 8 | 16Gi / 32Gi |
| Ingress + API Server | 4, expandable to 8 | 4 / 8 each | 8Gi / 16Gi each |
| Managed Redis | 1 | 4 / 8 | 32Gi / 64Gi |

These containers request 24 CPU / 80Gi with four API replicas, or 40 CPU / 112Gi
with eight. Budget separately for collectors, system reservations, monitoring,
image distribution and workers. Limits permit bursts only where physical
capacity remains available; requests are scheduling reservations, not usage caps.

API replicas require distinct hosts: four eligible nodes for four replicas,
eight for eight. Provision additional headroom to retain that replica count
through a node failure. Zone spread is preferred. The existing rolling update
uses zero surge / one unavailable replica, and the PDB permits one voluntary
disruption; neither setting proves that active connections drain gracefully.

Select a stable control pool through each role's `nodeSelector`, `tolerations`
and `affinity`, rather than placing control services on elastic execution-only
nodes. Redis placement must also satisfy its PV topology. Keep Coordinator and
Redis on separate hosts where feasible. Coordinator remains single-active with
Recreate; additional API replicas do not remove Coordinator or Redis outages.

## Applying the profile

Prepare site values for the actual image/digest, TLS/bootstrap Secret, control
pool and an explicitly selected SSD-backed StorageClass. Use the existing
[Kubernetes installation contract](../README.md#2-kubernetes-helm) for credentials,
images and public Services. The profile does not enable E2B or alter its ingress
policy; protocol enablement remains a separate deployment choice.

Render the core chart from the repository root, merging site values last:

```bash
helm template akernel deploy/akernel/charts/core -n akernel \
  -f deploy/akernel/charts/core/values-production.yaml \
  -f /path/to/site-values.yaml
```

The umbrella chart expects these values under `core`. Generate a wrapper with
Python/PyYAML, then render with your complete umbrella site values:

```bash
python3 - <<'PY' > /path/to/production-umbrella.yaml
import yaml
with open('deploy/akernel/charts/core/values-production.yaml') as source:
    print(yaml.safe_dump({'core': yaml.safe_load(source)}, sort_keys=False))
PY
helm template akernel deploy/akernel -n akernel \
  -f /path/to/production-umbrella.yaml -f /path/to/site-umbrella-values.yaml
```

Both cloud Terraform modules accept `core_values_override_files`: append the
**unwrapped** profile and site values after any existing overrides, in that order.
These files override module-generated Pod values, not cloud-node sizing; inspect
the full plan. To expand APIs, set `adx.ingressApi.replicas: 8` (under `core` for
the umbrella chart) after qualifying the extra hosts and backend capacity.
The profile installs no HPA and makes no changes to worker scaling policies.

## Redis storage and existing releases

The profile selects one Redis instance, AOF `appendfsync=everysec`, and a 200Gi
ReadWriteOnce PVC. Set `adx.redis.persistence.storageClassName` explicitly to a
qualified SSD-backed cloud-disk class. The empty value uses the cluster default,
which does not establish SSD performance. Verify the class's topology, binding,
expansion and reclaim behavior, and qualify latency/IOPS for persistence.

An existing StatefulSet's `volumeClaimTemplates` cannot simply change to 200Gi
through Helm. Preserve **all existing immutable claim-template fields** in site
overrides, including size, StorageClass and access modes. Expand the actual bound
PVC through a supported CSI procedure and verify filesystem capacity; retain the
template overrides on later upgrades. Do not shrink an already larger claim.
If expansion or a required class change needs new storage, plan an offline
backup/restore with data verification and retain the original volume. Do not
delete a PVC to make a Helm upgrade succeed.

Before modifying a live release, save its complete chart/values/rendering chain
and verify current Secret and PVC identities. Offline rendering cannot recover
the Redis password preserved by live Helm lookup. Review server-side dry-run
and live differences, drain affected consumers for Coordinator/Redis maintenance,
and pause/resume scaler actions as needed without clearing their state. Verify
Redis authentication/persistence, each API Pod and real SDK traffic before
restoring traffic. Roll back only reviewed resource/replica/placement fields;
preserve credentials, data and the actual PVC size. Helm rollback cannot undo
volume expansion, and must never start a second active Coordinator.

## Capacity qualification

Single-instance Redis is an accepted availability boundary, not HA. `everysec`
is not zero-RPO, and AOF on one volume is not an independent backup. Maintain
and test backups separately. Redis CPU reservations do not parallelize individual
commands. Monitor RSS, dataset growth and fork copy-on-write during persistence;
the 32Gi request is not a dataset cap and 64Gi does not guarantee every rewrite
fits. Do not introduce metadata eviction to hide memory pressure.

Qualify inventory, active connections and create/delete churn independently,
including retained snapshots, paused sandboxes and persistence/rewrite cycles.
Exercise both SDKs, streams, reconnect/drain behavior, worker scaling and the
accepted Coordinator/Redis interruption and recovery. Measure scheduling and
API latency/errors, CPU throttling, Redis memory/command latency, disk growth/I/O,
and host/LB connection and file-descriptor limits. Set workload-specific acceptance
limits before claiming supported capacity; chart rendering is not a load test.
