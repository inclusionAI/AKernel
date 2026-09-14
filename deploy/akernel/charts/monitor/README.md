# AKernel operations dashboards

The monitor chart provisions dashboards from `dashboards/*.json` into Grafana.
Existing Grafana users can open them through **Dashboards**, subject to their
normal organization/folder permissions. Authentication is required; this chart
change does not create users or enable anonymous access.

| Dashboard | UID | Use |
|---|---|---|
| AKernel Data Plane | `akernel-data-plane` | Edge/Node Proxy readiness, scrape freshness, HTTP responses and latency, sessions, H2 connections, streams, throughput, routing and overload errors |
| AKernel Process Resources | `akernel-process-resources` | CPU cores, resident/virtual memory, FD, threads and uptime for control/data-plane processes, sandboxd and collector |

The layout follows **AKernel Node Resources**: two-column aggregate trends,
Last/Max legends, dashed limit curves where a real limit exists, and a full-width
current snapshot. Process details use one table with expandable Pod rows.
Each Pod row shows its Node, Namespace and Environment; expand it to inspect
Component, PID and resource metrics for that Pod. Data-plane diagnostics are in a collapsed row. Definitions
and metric boundaries are available in panel descriptions.

Both dashboards use the existing `prometheus` datasource UID and link to the
Pod/PVC and sandbox dashboards. Filter by environment, namespace, node and Pod;
the process dashboard also supports component selection.

## Enable metric collection

Set the following in the **core** chart values, using the actual monitor
namespace and service:

```yaml
monitoring:
  prometheusEndpoint: prometheus.akernel-monitor.svc.cluster.local:9090
  processMetrics:
    enabled: true
    interval: 15s
  dataPlaneMetrics:
    enabled: true
    interval: 5s
```

The Prometheus server must accept remote write. These dashboards do not change
retention, storage capacity, scrape receivers or the configured workload limits.

## Access Grafana through Edge

Set the origin URL in the **core** chart values (without a `/grafana` suffix):

```yaml
dataPlane:
  edge:
    grafanaURL: http://grafana.akernel-monitor.svc.cluster.local:3000
```

The core chart mounts an Edge route file and sets
`YR_DATA_PLANE_EDGE_FRONTEND_PROXY_ROUTES_FILE`. Edge reads this configuration
at startup; route changes roll Frontend. The image startup helper respects an
explicit route file instead of overwriting it.

Set the public URL in the **monitor** chart values:

```yaml
grafanaServer:
  env:
    rootUrl: https://your-edge-host/grafana/
    serveFromSubPath: true
```

After upgrading, open `/grafana/d/akernel-data-plane` or
`/grafana/d/akernel-process-resources` using the existing Grafana login.
The provisioned dashboards inherit Grafana's existing permissions. Use the
normal Grafana user/team administration workflow when assigning operators.

## Reading the panels

- CPU is measured in logical cores from user + system CPU seconds; it is not a
  percentage of the node or the Pod quota. Compare Pod CPU throttling and limits
  in the Pod/PVC dashboard.
- Process resource statistics and curves include samples updated within 45s,
  assuming the default 15s collection interval. If changing that interval,
  adjust this freshness window as well. Resource totals and the per-process
  snapshot use the same freshness condition. Gateway snapshots retain sample
  age next to readiness, since a stopped collector can leave cached readiness.
- FD is a count. No per-process FD limit is collected, so the dashboard does not
  calculate an FD utilization percentage. Compare the same load before/after
  churn and inspect the PID-specific curves for recovery.
- RSS is resident memory; virtual memory is address space rather than physical
  consumption. Legends retain component, Pod and PID to separate restarts.
- Edge HTTP latency ends when the response object is returned. Full streaming
  and file-transfer duration must be measured at the client.
- No traffic may leave latency/response ratios undefined. Missing telemetry is
  shown as no data, not silently replaced with zero or marked healthy.
- A selected Node Pod has no Edge metrics, and vice versa. Restore **All** to
  compare the roles. Independent etcd Pods remain in the Pod resource dashboard.

Before a load or soak run, verify recent samples from every selected Pod and
confirm that the capacity/load driver records end-to-end timing, retries and
content integrity independently of Grafana.
