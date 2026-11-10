# Registry connectivity guard

Install this companion Helm release alongside a private Registry that uses
Bearer token authentication and serves Nydus images. It does not own or change
the Registry Deployment, storage, resources or placement. Keep it installed
when moving or upgrading the Registry.

## Network boundary

The NetworkPolicy selects Registry Pods and permits TCP 5000 (Registry) and
5001 (token authentication) from **one peer containing both selectors**:

- namespace label `kubernetes.io/metadata.name: dragonfly-system`;
- Pod labels `app: dragonfly` and `component: seed-client`.

These selectors are an AND, not separate namespace-wide / Pod-wide allowances.
Existing ingress policies remain additive. CIDRs alone are insufficient when
the CNI enforces source Pod identity across node pools. Change the selectors
and container ports to match the deployed services; service-facing ports may
differ from container ports. This policy grants no access to unrelated
Registry management ports. It does not configure source-side egress policies.

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
5. Read the same layer through the seed's local Dragonfly HTTP proxy and verify
   identical content.

Only a complete five-seed success advances `<release>-state`. A failed Job
logs redacted per-seed results and exits nonzero. Subsequent scheduled Jobs
retry; this is a post-rollout check, not a Registry readiness gate or automatic
rollback. Monitor failed Jobs and the age of the state ConfigMap's `checked_at`.
History keeps two successful and three failed Jobs; completed Jobs expire after
one day. A successful `unchanged` Job alone is not a new network verification.

The checker uses the Kubernetes API and execs only in the five configured seed
Pods. Seeds require `sh`, `head`, `curl` and `base64`, but do not require Python.
No seed files or process state are changed. Secret contents, Basic credentials
and Bearer tokens pass through exec stdin, never command arguments or logs.
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
# Defaults select the five dragonfly-seed-client-{0..4} Pods.
```

Configure `imagePullSecrets`, placement and tolerations as needed. Registry
and token-service certificate validation uses curl's native trust in each seed;
private HTTPS endpoints need their trusted CA installed there. Plain HTTP is
supported for deployments whose network/security policy permits it. The
checker never adds an insecure TLS flag.

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

Validate changes with:

```bash
python3 -m unittest discover -s deploy/registry-connectivity/tests -v
```

Also run `helm lint` with complete operator/example values and review the
rendered policy. Test a rollout trigger using a disposable watched Deployment
rather than restarting a shared Registry solely to exercise the checker.
