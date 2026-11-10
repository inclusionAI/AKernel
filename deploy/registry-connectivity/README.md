# Registry connectivity guard

Install this companion Helm release alongside a private Registry that uses
Bearer token authentication and serves Nydus images. It does not own or change
the Registry Deployment, storage, resources or placement. Keep it installed
when moving or upgrading the Registry.

## Network boundary

NetworkPolicy creation is **off by default**. Before setting
`networkPolicy.enabled=true`, review every current Registry client, including
BuildKit, runtime nodes and direct push/pull clients. Existing policies must
already permit their traffic. Set `networkPolicy.existingClientsReviewed=true`
only after that review. The first ingress policy selecting a Pod makes it
ingress-isolated: five passing seed checks do not prove other clients retain
access. Keep both flags explicit when upgrading an installation that already
owns this policy; omitting them would remove that Helm-managed policy.

When enabled, the NetworkPolicy selects Registry Pods and permits TCP 5000
(Registry) and 5001 (token authentication) from **one peer containing both selectors**:

- namespace label `kubernetes.io/metadata.name: dragonfly-system`;
- Pod labels `app: dragonfly` and `component: seed-client`.

These selectors are an AND, not separate namespace-wide / Pod-wide allowances.
Existing ingress policies remain additive. CIDRs alone are insufficient when
the CNI enforces source Pod identity across node pools. Change the selectors
and container ports to match the deployed services; service-facing ports may
differ from container ports. This policy grants no access to unrelated
Registry management ports. Empty port lists and invalid port numbers are rejected;
they must never render as an unrestricted `ports: null`. The policy does not
configure source-side egress policies.

## Automatic verification

A CronJob checks Registry generation, ready Pod UIDs, container identities and
restart counts, plus the identities of the five named seed Pods, every minute.
A change triggers a full check after the rollout becomes ready. An unchanged
deployment is checked again every ten minutes. A change during verification
invalidates the result and is retried. Missing/unready seeds fail the check.

Each of the five seeds must:

1. Receive an unauthenticated Registry Bearer challenge.
2. Obtain a scoped pull token from an explicitly trusted token-service origin.
3. Fetch the configured immutable manifest and verify its SHA-256.
4. Fetch its actual Nydus bootstrap layer directly from the Registry; verify
   digest and size, and read the regular `image/image.boot` tar member.
5. When proxy checking is enabled, read the same layer through the seed's local
   Dragonfly HTTP proxy and verify identical content. Direct-only results
   explicitly report `proxy_checked: false` and do not validate the proxy path.

Every curl transfer must exit successfully, even if HTTP 200 and the expected
content bytes were received. Responses and bootstrap files are capped at 1 MiB,
decompressed tar archives at 2 MiB, with at most two seeds checked concurrently.
Keep the dedicated probe small; the default 256 MiB Job is not intended to
inspect arbitrary task-image bootstraps. Oversized probes fail without advancing
success state. A peak-RSS regression exercises the allowed bounds.

Only a complete five-seed success advances `<release>-state`. A failed Job
logs redacted per-seed results and exits nonzero. Subsequent scheduled Jobs
retry; this is a post-rollout check, not a Registry readiness gate or automatic
rollback. Monitor failed Jobs and the age of the state ConfigMap's `checked_at`.
History keeps two successful and three failed Jobs; completed Jobs expire after
one day. A successful `unchanged` Job alone is not a new network verification.

The checker uses the Kubernetes API and execs only in the five configured seed
Pods. Seeds require `sh`, `head`, `curl` and `base64`, but do not require Python.
No seed files or process state are changed. Secret contents, Basic credentials
and Bearer tokens pass through exec stdin and are omitted from command arguments
and **checker** logs. Other components have independent logging behavior;
credential safety of their logs must be verified separately.
The checker ServiceAccount cannot read Secrets. It mounts one existing Secret
containing a pull-only credential. Its exec permission is restricted to the
five Pod names; assess that permission as part of the deployment trust boundary.

## Prepare the checker and probe

Build `Dockerfile` with an approved remote BuildKit builder and publish the
checker image; pin its manifest digest in Helm values. The client running
Buildx must reach both the exporter Registry and its token-service realm.
Forwarding only BuildKit gRPC does not provide token-service connectivity.
Do not build images on an operator workstation when remote builds are required.

Create a small real Nydus probe using the deployed Nydus-capable BuildKit:

```dockerfile
FROM scratch
COPY probe.txt /probe.txt
```

Use its native image exporter with `oci-mediatypes=true,compression=nydus` and
publish to a dedicated probe repository. Pin a **single-platform Nydus manifest
digest**, not an index. Retain that manifest, bootstrap and referenced blobs;
exclude this fixture from task-image garbage collection. The checker does not
create artifacts, retain task images, or modify Registry garbage collection.
Registry backup/restore must preserve this fixture and its pull authorization.

Provision a Secret in the Registry namespace through the platform's Secret
manager, with a `credentials.json` key containing `username` and `password`.
Use a pull-only identity, preferably restricted to the probe repository. Never
put credentials in Helm values, command arguments or version control. The
checker does not need the token signer's private key or a push credential.

## Proxy logging and HTTPS prerequisites

Dragonfly client v1.3.0 logs complete proxy requests, including Authorization,
at INFO level. Checker log redaction does not protect those logs. Before
sending a real pull token through the proxy:

1. Disable request-dump logging (for unmodified v1.3.0, `--log-level=warn` removes
   the INFO/DEBUG dumps), or use a verified redacting implementation. Coordinate
   this with the owner of Dragonfly; this chart does not roll the seed Pods.
2. Send a **dummy**, non-production Authorization value through each seed's
   proxy. Search for that marker in console output, daemon log files and any
   collected logs after allowing the logging pipeline to flush. Do not use a
   real token to test redaction. Repeat after logging/version changes.
3. Only after verifying the log sinks, set
   `dragonfly.proxyLoggingVerified=true`. Both Helm and the checker reject
   enabled proxy probes without this explicit verification. If it cannot yet
   be verified, use `dragonfly.proxyCheckEnabled=false` for direct-only checks
   or `suspend=true` to pause the guard.

For an HTTPS Registry, curl uses CONNECT to the **HTTP** Dragonfly proxy.
Dragonfly terminates TLS and signs a replacement endpoint certificate. Configure
`proxy.server.caCert` and `proxy.server.caKey` in Dragonfly with its signing CA;
its default ephemeral self-signed certificate is unsuitable for this verified
probe. Configure Dragonfly's upstream Registry trust separately, for example
`proxy.registryMirror.cert` in v1.3.0.

Use these paths **inside every seed**, provisioned by the platform:

- `registry.caFile`: curl's CA bundle for a direct private HTTPS Registry.
- `registry.authCAFile`: curl's CA bundle for the private HTTPS token service.
- `dragonfly.proxyCAFile`: the Dragonfly signing CA bundle, explicitly required
  for HTTPS proxy checks. This is curl's target `cacert` for the intercepted
  connection, not `proxy-cacert` (the proxy transport itself is HTTP).

Direct requests can use curl's native trusted roots when their CA path is empty.
Trusting only the Registry CA does not trust Dragonfly's generated certificate.
No insecure TLS flag is added. These settings concern Registry traffic inside
seeds; SDK `AKERNEL_VERIFY_TLS` controls the separate AKernel API connection and
is neither required nor consumed by this chart. Using an HTTP API endpoint does
not require that SDK setting; it does not enable HTTPS or change the URL scheme.

Sources: [Dragonfly proxy logging and CONNECT handling](https://github.com/dragonflyoss/client/blob/v1.3.0/dragonfly-client/src/proxy/mod.rs),
[proxy and Registry CA configuration](https://github.com/dragonflyoss/client/blob/v1.3.0/dragonfly-client-config/src/dfdaemon.rs).

## Deploy

Supply operator-owned values (keep private addresses out of Git):

```yaml
image: registry.example/operations/registry-connectivity@sha256:<checker-digest>
registry:
  deployment: registry
  podSelector: {app: managed-registry}
  url: https://registry.example
  allowedAuthOrigins: [https://registry-auth.example]
  probeRepository: operations/nydus-connectivity
  probeDigest: sha256:<nydus-manifest-digest>
  pullSecret: registry-connectivity-pull
dragonfly:
  # Start with direct-only checks until proxy logging/trust has been verified.
  proxyCheckEnabled: false
# Defaults select the five dragonfly-seed-client-{0..4} Pods.
# NetworkPolicy is enabled separately after auditing existing client ingress:
# networkPolicy: {enabled: true, existingClientsReviewed: true}
```

Configure `imagePullSecrets`, placement and tolerations as needed. Enable proxy
checks only after completing the prerequisites above; for HTTPS add
`dragonfly.proxyCAFile`. Plain HTTP is supported where deployment policy permits
it. The chart creates the state ConfigMap without `data.state.json`; the Job
owns that field, so Helm server-side apply must not initialize/reset it on an
upgrade. Retain the state through subsequent releases.

```bash
helm upgrade --install registry-connectivity ./deploy/registry-connectivity/chart \
  --namespace <registry-namespace> -f /secure/operator-values.yaml
kubectl -n <registry-namespace> create job \
  --from=cronjob/registry-connectivity registry-connectivity-initial
kubectl -n <registry-namespace> logs job/registry-connectivity-initial
```

If a policy with the configured name already exists, compare its full spec
before adopting that policy into this release. Do not adopt or replace the
Registry Deployment/PVC as part of installing the guard. Preserve other
operators' placement and resource settings.

After moving or rolling the Registry, inspect the next Job and confirm a new
`verified` event, **five** `passed: true` results, matching bootstrap hashes and
a fresh `checked_at` in `registry-connectivity-state`. To force an immediate
recheck without restarting production services:

```bash
kubectl -n <registry-namespace> create job \
  --from=cronjob/registry-connectivity registry-connectivity-manual
# A periodic check runs automatically even if no Registry identity changed.
# To bypass the ten-minute reuse window, add FORCE_CHECK=1 to a copy of the
# CronJob's Job template before creating that manual Job.
```

The local transport regressions require `curl` and `openssl` in addition to
Python, PyYAML and Helm. They exercise truncated HTTP and HTTPS CONNECT without
changing a cluster. Validate changes with:

```bash
python3 -m unittest discover -s deploy/registry-connectivity/tests -v
```

Also run `helm lint` with complete operator/example values and review the
rendered policy. Test a rollout trigger using a disposable watched Deployment
rather than restarting a shared Registry solely to exercise the checker.
