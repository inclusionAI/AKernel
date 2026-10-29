# Checkpoint backup sets

This chart is disabled by default. Deploy it only after a real backup and an
isolated restore have qualified the selected image and storage configuration.
Rendering or the local protocol tests do not establish a recoverable cloud
backup. The develop rollout can open its application ingress independently;
record any outstanding backup acceptance explicitly.

Build `Dockerfile` using its pinned inputs and hash-locked Python requirements.
Supply a qualified Redis tools image through `REDIS_TOOLS_IMAGE`, and deploy
the resulting tools image by digest. The script is mounted from the chart.
The scheduled Job consumes the existing JuiceFS host mount and never formats
the live volume. Give it the same storage alias, checkpoint prefix and Redis
namespace as Coordinator. The schedule defaults to 02:00 Asia/Shanghai.

The Job pauses Redis writes for at most 45 seconds, exports an RDB and clones
the checkpoint namespace within that window. It checks primary identity and
rejects an expired window. It then validates every referenced artifact and
streams the RDB, catalog, recovery configuration, PostgreSQL logical dump and
sparse checkpoint archive to OSS. Only a completed manifest commits a set.
Incomplete uploads cannot be selected for recovery. A failed JuiceFS clone is
retained outside the Coordinator GC prefix for investigation; inspect and
remove only a confirmed task-owned abandoned clone after resolving the error.

Redis must have an initialized replication backlog and a live replica. The
state chart sets `repl-diskless-sync-delay 0` so opening an RDB export does not
consume the default five-second diskless replication delay. Redis write pause
also blocks checkpoint/catalog mutation during the consistency window. A
validation error always attempts to unpause; Redis expires the pause even if
the Job dies. Measure this pause under representative load before enabling a
schedule for a large state space.

Use RRSA/OIDC with an audience of `sts.aliyuncs.com` and a trust policy restricted
to this chart's ServiceAccount. `deploy/terraform/aliyun-backup` creates an
independent private ZRS bucket with encryption and HTTPS enforcement, plus a
write-only role. It neither imports the existing ACK cluster nor enables RRSA.
Enable RRSA separately only with the cluster owner's authorization: ACK does
not support disabling it afterward. Explicit short-lived STS Secrets are
supported for a validation Job; they are not a permanent scheduled credential
solution. The Job never falls back to a host instance role or static AccessKey.

The Terraform retention rules expire completion records after seven days and
payloads after eight. The six-hour Job deadline and the extra day keep payloads
available throughout their manifest's retention. Incomplete multipart uploads
expire after three days. The backup identity cannot read or delete backups;
use a separate short-lived reader for recovery. List exact required Secrets
and ConfigMaps in `config.recoveryObjects`: RBAC permits only `get` on those
names. Recovery material contains credentials and must stay private.

For an isolated recovery drill:

1. Select a completed manifest, download every payload using the reader, and
   verify each recorded size and SHA256 before restoring any component.
2. Use a separate namespace, Redis instance, PostgreSQL database, object-store
   bucket and JuiceFS volume. Never connect restored PostgreSQL metadata to
   the live RGW bucket. Restore the PostgreSQL dump separately for metadata
   inspection; this backup is a logical checkpoint-fileset export, not an RGW
   object-level snapshot suitable for reusing all original chunk addresses.
3. Format the isolated volume, import the sparse archive under the original
   checkpoint prefix, and retain the original ADX storage alias. Restore the
   RDB and compatible service configuration. Keep original node registrations
   fenced out of the isolated cluster and use independent control credentials.
4. Restore a known Snapshot through the public SDK on an isolated worker and
   verify process memory, files and business state. Record source and target
   identities and timings, then remove only drill-owned resources.

The legacy develop data does not need a new backup when the owner permits a
reset. This does not qualify backups of the newly deployed state or replace
the isolated restore requirement for production acceptance.
