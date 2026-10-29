# S3 checkpoints with native gVisor reflink

For checkpoint-only deployments, use ADX's existing S3 backend with node-local
capture and restore inputs. An S3-compatible Ceph RGW endpoint can serve this
role without JuiceFS or PostgreSQL. The existing distributed_fs mode remains
available when a shared filesystem is independently required. Its direct
shared paths do not satisfy native gVisor FICLONE against a local XFS filestore.

## Data path and configuration

The node captures on its local filesystem, publishes the complete artifact to
S3, and acknowledges the durable recovery point after publication. Another
node downloads and verifies the complete artifact before passing local inputs
to sandboxd. gVisor background restore begins after that materialization. This
is not remote demand paging or filesystem-internal cloning. Local reflink also
does not provide dirty-page incremental capture.

Place the filestore and checkpoint cache on the same native XFS volume with
reflink enabled. A loop filesystem under the filestore directory does not meet
that condition. The chart probes real FICLONE support before starting the node:

```yaml
adx:
  checkpointStorage:
    kind: s3
    alias: akernel-checkpoints-s3
    prefix: akernel/checkpoints-v1
    s3:
      bucket: YOUR_CHECKPOINT_BUCKET
      region: us-east-1
      endpoint: https://YOUR_RGW_ENDPOINT
      credentialsSecret: akernel-checkpoint-s3
      cacheRoot: /home/akernel/adx/checkpoint-s3-cache
      cacheBudgetBytes: 53687091200
      cacheMaxEntries: 500
node:
  config:
    sandboxd:
      nativeFilestore: true
      runscCheckpointMode: reflink
```

The existing Secret contains AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, with
an optional AWS_SESSION_TOKEN. Only node and Coordinator containers receive
these values. The node launcher imports them into the ADX system service.
Instance metadata fallback is disabled. Rotate credentials through the bucket's
identity manager, update the Secret, then drain and restart the consumers.
Credentials never belong in Helm values, artifact records or Terraform state.
HTTP endpoints require explicit s3.allowHttp=true; restrict such endpoints to
an accepted private network boundary. A public or untrusted network requires
TLS. An empty endpoint selects the SDK's regional S3 endpoint.

Node and Coordinator share alias, bucket, region, endpoint and prefix. Only
nodes receive the cache configuration; Coordinator reclaims remote objects.
Terraform forwards these settings through checkpoint_storage.s3. The local
backend can also use native reflink, but its recovery points remain local.

Running executions pin restore inputs independently of the unused-entry cache
limits. The current stock S3 transport uploads logical file contents, including
zero-filled holes, and its downloads can allocate the full logical size. Budget
network traffic, remote capacity and local disk headroom accordingly. Sparse
encoding is a separate potential optimization, not a reason to introduce a
filesystem layer without measuring the whole deployment tradeoff.

## Control, business and storage pools

Keep Coordinator single-active, with Kubernetes reconstruction and an accepted
control-service interruption. Spread at least two Ingress/API replicas across
zones. Business workers keep their private writable filestores and restore
inputs local. Their count can be reduced independently of storage replication.

For a self-managed RGW backend, use three storage nodes in separate zones,
three MONs, two MGRs, two RGWs and one independent raw Block OSD PVC per zone.
Three 1 TiB OSDs with three replicas provide about 1 TiB logical capacity.
Alert at 80 percent and retain recovery headroom. Dedicated labels and taints
exclude sandbox workers from the storage pool.

The existing charts/juicefs chart can provide Ceph RGW only by setting
juicefs.enabled=false. This omits filesystem clients, formatting, readiness and
managed PostgreSQL. It preserves the Ceph cluster, object store and existing
bucket resources. An [ObjectBucketClaim example](examples/ceph-s3-bucket.yaml)
uses the provisioner's bucket-specific credentials. Keep claims and Secrets
managed in the storage namespace, and deliver the credential Secret to the
application namespace through the deployment's secrets workflow.

Install pinned Rook before its resources. With Rook 1.21.0, disabling the CSI
operator and CSI resources still leaves a ClientProfile API dependency in the
filesystem controller. Install the six API CRDs from the verified bundled CSI
subchart, without deploying its controllers or drivers for RGW-only use.
For a new Ceph 20.2.4 cluster, verify all ceph auth dump-keys entries use aes256k
before setting ceph.allowedCiphers=[aes256k]. Do not restrict existing kernel
clients without qualifying their key requirements.

charts/state-services supplies three Redis instances, three Sentinels and two
role-checked HAProxy replicas across control zones. Use independent 20 GiB cloud
PVCs, AOF always, no eviction and at least one healthy replica for writes.
Select postgres.enabled=false when checkpoint storage uses S3. CloudNativePG
is only needed for a separately selected filesystem deployment. Redis is
asynchronous: neither the replica constraint nor the proxy establishes zero
RPO or network-partition fencing. Qualify reconnection and state reconciliation.

On ACK, exclusive ENI workers do not support NetworkPolicy, including as sources
of policies protecting shared-ENI state Pods. This mixed topology must use an
accepted VPC/security-group boundary plus service authentication and disable the
state chart's ineffective NetworkPolicy. It does not provide namespace-level
isolation. Shared-ENI pools are required if that isolation is a deployment goal.

Enable prometheusServer.config.sharedStorage.enabled for Ceph and Redis/Sentinel
metrics and alert rules. Set sharedStorage.postgresEnabled=false without active
PostgreSQL consumers. Verify actual targets and samples; notification routing
remains the monitoring deployment's responsibility.

## Migration and acceptance

For existing ACK clusters, deploy/terraform/aliyun-node-pools manages only the
new storage pools. Review the saved plan and keep state and private profiles
outside Git. Pin the qualified runtime and service images by digest.

Before changing checkpoint identity, quiesce ingress and drain executions.
Set adx.ingressApi.publicService.maintenance=true to withhold public traffic
without deleting the Service or its load balancer. This does not drain existing
executions; inventory and explicit consumer cleanup remain required. Keep this
setting through upgrades, then clear it after internal acceptance succeeds.
Preserve the prior configuration. Select a fresh Redis namespace, remote prefix
and node data directory on the same native disk so that local journals cannot
replay state from the previous backend. Keep the container paths consistent
across nodes. Qualify one worker before expanding to the remaining workers.

When retiring filesystem clients, first verify that no Coordinator or worker
Pod mounts the shared path. Then disable the clients and format/readiness Jobs.
Only after all clients stop may the metadata database be hibernated. Preserve
its PVCs and old object data for rollback rather than deleting data during the
backend switch. Restoring the previous core requires restoring its clients and
metadata service before restarting consumers. For later client upgrades, retain
OnDelete and drain consumers before explicitly replacing clients.

The maintained native probes are deploy/tests/native_restore/shared_checkpoint_probe.py
and shared_checkpoint_soak.py. The capture phase intentionally retains its
source for an operator-owned outage test. Other phases explicitly delete their
detached instances with kill(delete_remote=True). Keep a recorded Snapshot until
its observation process ends; a detached handle's ordinary kill only closes
local transports.

Acceptance includes source-host loss with uncached targets, memory and business
state verification, repeated and concurrent restore isolation, SDK lifecycle and
reference reclamation. Measure freeze time separately from durable publication
and restore RPC latency. Add component/storage/network/disk/mount failures,
background-input protection under cache eviction, a recoverable backup set of
control state and referenced objects, isolated recovery and a full 24-hour
observation. Report pending scenarios explicitly. Successful samples or render
tests alone do not establish full production acceptance.
