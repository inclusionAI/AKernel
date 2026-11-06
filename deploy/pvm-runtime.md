# PVM-backed Firecracker nodes

PVM is an experimental opt-in Firecracker backend. Use
`Sandbox(runtime="firecracker-pvm")` on a dedicated PVM node profile. The
default `firecracker` class requires hardware KVM. Sandboxd probes the actual
`/dev/kvm` ABI before advertising either class; ADX forwards the runtime
request and consumes that inventory. Static labels cannot replace probing.

## Versioned inputs and ownership

Use the existing distribution host/L1 kernel with a matched out-of-tree
`kvm.ko` and `kvm-pvm.ko`. Keep the host and guest sources independent:

| Input | Authoritative record |
| --- | --- |
| Host source, OOT baseline, target kernel/config/compiler, provisioning bounds and fixtures | [`pvm/host-profile.json`](./pvm/host-profile.json) |
| Approved preparation and module build identities | [`pvm/host-build.json`](./pvm/host-build.json), checksum-bound by the host profile |
| Host shell exports | [`pvm/oot-host.env`](./pvm/oot-host.env), derived from the profile |
| PVM guest bundle, source provenance and archive digest | `third_party/firecracker-pvm.json` in the [sandboxd submodule](../src/sandboxd) at the AKernel gitlink |
| Guest source and build configuration | Firecracker's [`pvm-kernel-versions.env`](https://github.com/akernel-dev/firecracker/blob/develop/resources/akernel/pvm-kernel-versions.env) and resolved `resources/akernel/kernel/pvm-guest.config` for the selected bundle source |

The host-only OOT source does not contain the PVM guest implementation. Use
the repositories and revisions selected by these records, including reviewed
adaptation sources; replacing a repository name is not an upstream migration.
Generic host and guest changes belong in the corresponding `virt-pvm/linux`
lines, while AKernel packaging belongs in `akernel-dev/firecracker`. Moving a
pin to upstream requires source/archive verification and renewed qualification.

AKernel maintainers own the host profile, security-backport selection, target
ABI/compiler compatibility, paired module builds, fixture versions and
acceptance evidence. Sandboxd owns the generic ownership guard, ABI and restore
checks without another copy of host pins. The selected OOT baseline includes
the OSPKE guard and NX recovery-worker retry fixes; updating the distribution
kernel alone does not update that baseline's KVM security fixes.

The committed receipt is a projection of retained build evidence:
`original_evidence_sha256` identifies the original record, whose logs and
local resource details remain outside Git. Reusing the unchanged module pair
reuses its receipt. A rebuild requires a new receipt and hashes, a synchronized
profile update, review of source/target changes and renewed qualification.
The receipt digest is an approved input, not a value chosen by a runner marker.

## Prepare the host module pair

The profile's `target` defines the supported distribution kernel, architecture,
headers, generated configuration, `Module.symvers` and compiler identity. Other
kernel ABIs/configurations and AMD execution require separate qualification.
Apply the target profile to a fresh source export and build against the running
distribution kernel; do not rebuild or replace the host kernel binary.

KVM must be unloadable modules (`CONFIG_KVM=m`). A built-in core cannot be
replaced this way. The OOT build enables its PVM backend privately; the
distribution configuration does not need `CONFIG_KVM_PVM`. Do not force-load
modules or bypass symbol-version/signature checks. Module signing must satisfy
the host's existing policy.

```bash
set -euo pipefail
source deploy/pvm/oot-host.env
: "${PVM_HOST_SOURCE:?select a new host-module source checkout path}"
: "${PVM_HOST_PREPARED_SOURCE:?select a fresh target module build path}"
test "$(uname -r)" = "$PVM_HOST_QUALIFIED_KERNEL"
test ! -e "$PVM_HOST_SOURCE"
test ! -e "$PVM_HOST_PREPARED_SOURCE"
git init "$PVM_HOST_SOURCE"
git -C "$PVM_HOST_SOURCE" fetch --depth=1 \
    "$PVM_HOST_SOURCE_REPOSITORY" "$PVM_HOST_SOURCE_COMMIT"
git -C "$PVM_HOST_SOURCE" checkout --detach FETCH_HEAD
test "$(git -C "$PVM_HOST_SOURCE" rev-parse HEAD)" = "$PVM_HOST_SOURCE_COMMIT"
git -C "$PVM_HOST_SOURCE" fetch --depth=1 \
    "$PVM_HOST_SOURCE_REPOSITORY" "$PVM_HOST_BASELINE_COMMIT"
python3 "$PVM_HOST_SOURCE/tools/pvm-oot/prepare.py" \
    --profile "$PVM_HOST_SOURCE/$PVM_HOST_TARGET_PROFILE" \
    --source "$PVM_HOST_SOURCE" --kernel-build "/lib/modules/$(uname -r)/build" \
    --cc gcc-15 --output "$PVM_HOST_PREPARED_SOURCE"
make -C "/lib/modules/$(uname -r)/build" \
    M="$PVM_HOST_PREPARED_SOURCE/arch/x86/kvm" PVM_OOT_MODE=1 CC=gcc-15 -j2 modules
```

Use the exact compiler required by the selected profile; the command above
uses its compiler executable. Preparation verifies baseline contents, patch
hashes/order and exact target inputs, rejecting changes rather than inferring
compatibility. Retain `.pvm-oot-prepared.json`, build logs and both module hashes.
A new distribution ABI needs its own reviewed profile and qualification.

Keep the complete stock networking/storage module packages. AKernel still
requires TUN, IPv4/IPv6 filter tables, conntrack, connmark/CONNMARK, bridge
filtering and ipset; a trimmed PVM module tree does not replace these
dependencies. See the [deployment requirements](README.md#network-acls).

PVM does not require VMX/SVM, including in an L1 without nested hardware
virtualization. FSGSBASE, RDTSCP and CMPXCHG16B remain required; CPUID faulting
is required when interception is enabled. Apply the profile's boot settings
and CPU exposure constraints, including `nokaslr pti=off` and no FRED. KASAN
is unsupported. OOT removes host binary customization, not boot or CPU
prerequisites; changing boot parameters can still require a controlled reboot.

Use a dedicated node or owned disposable L1. Drain workloads and stop sandboxd
and every VMM before switching modules. After verifying there are no KVM users:

```bash
set -euo pipefail
for vendor in kvm_intel kvm_amd; do
    if test -d "/sys/module/$vendor"; then modprobe -r "$vendor"; fi
done
if test -d /sys/module/kvm; then modprobe -r kvm; fi
modprobe irqbypass
insmod "$PVM_HOST_PREPARED_SOURCE/arch/x86/kvm/kvm.ko"
insmod "$PVM_HOST_PREPARED_SOURCE/arch/x86/kvm/kvm-pvm.ko"
```

Restart sandboxd to reprobe before accepting workloads, and keep the matched
pair fixed for its lifetime. Never combine the OOT core with stock VMX/SVM
modules. This is a two-module replacement, not an independent `pvm.ko` attached
to the stock core. AKernel does not automatically change host kernels,
boot settings, modules, networks or existing clusters.

## Guest and clock safety

Build a PVM guest with Firecracker's `AKERNEL_KERNEL_PROFILE=pvm` or candidate
workflow `kernel_profile=pvm` selection. The resolved guest configuration must
include `CONFIG_KVM_GUEST=y`, `CONFIG_PVM_GUEST=y`, `CONFIG_X86_PIE=y`,
`CONFIG_X86_INTEL_MEMORY_PROTECTION_KEYS=y` and AKernel's common filesystem,
network and virtio requirements. An upstream minimal PVM config is insufficient.
Package the configuration, source provenance, licenses and checksums together;
build the guest-agent initrd from the consuming sandboxd revision.

Guest MPK aligns guest/host `XCR0.PKRU` on PKU-capable hosts. Verify actual xstate
masks rather than assuming one CPU's mask is portable. Host PKU policy is
separate: the OSPKE guard is required for host PKRU accesses and temporary
guest-PKU overrides, including hosts booted with `nopku`. A guest update cannot
repair an older host module pair. Enabling guest MPK does not qualify guest
`pkey_mprotect` enforcement, which remains incomplete in this PVM revision.

Strict TSC is the selected guest's default. It marks TSC unstable, disables
deadline/adjust features, ignores pvclock's stable-TSC fast path and clamps
negative pvclock deltas. Mode selection follows command-line append/override
before kvmclock initialization; the last option wins, and empty/unknown values
use strict mode. `pvm_tsc=unsafe` retains the original clock/timer selection and
must not qualify restores across TSC-domain changes.

Strict mode protects the qualified POSIX clock/timer path, not raw
RDTSC/RDTSCP continuity. Raw TSC can move backwards after an L1 restart despite
matching frequency. PVM has no TSC frequency scaling; heterogeneous CPU
migration remains unqualified. Assess clock-heavy workloads and record guest
arguments during acceptance. A guest digest or argument change affects
checkpoint compatibility: retain the matching old stack or drain and recreate
sandboxes rather than bypassing checks.

## Bundle selection and promotion

`FIRECRACKER_KERNEL_PROFILE=pvm` selects sandboxd's independent PVM descriptor.
Its current `distribution=release` selects a public promoted archive; it needs
neither a GitHub token nor an unexpired Actions artifact. Ordinary KVM keeps its
own release manifest. The guest profile does not select the host backend.

```bash
FIRECRACKER_KERNEL_PROFILE=pvm AKERNEL_TARGETARCH=amd64 \
    deploy/scripts/build-image.sh --repository pvm-validation --tag candidate
```

For a future unreleased candidate, pin its exact source/run, Actions artifact
and archive digests in the descriptor. Sandboxd's
`third_party/prepare-firecracker-pvm.py` at the same gitlink validates
provenance, archive contents, inner binary/configuration digests,
architecture and required PVM options before staging original bytes.
`PVM_CANDIDATE_ARCHIVE` can supply a downloaded copy but cannot replace locked
digests. Actions downloads require `GH_TOKEN`; keep it outside the Docker build
context and never forward it to signed blob storage. A failed verification is
fatal, with no fallback to another archive or hardware KVM.

Promote only after sandboxd and AKernel qualification pass for the exact
candidate and host profile. Preserve the Firecracker develop/manual release
gates and publish the original archive and checksum/provenance sidecars without
rebuilding. Download the public assets without credentials and recheck their
digests and provenance before changing the descriptor to the release URL.
Advance AKernel's sandboxd gitlink to the accepted source and qualify that final
combination. Record run links, revisions, artifact/image/initrd digests and
results in qualification evidence rather than duplicating pins in this guide.
PVM guest bundles require `linux/amd64`; ARM64 retains runsc/optional runc.

## Deployment and storage profile

Standalone selects `AKERNEL_FIRECRACKER_BACKEND=pvm` with a PVM-enabled image.
The generated configuration selects `firecracker-pvm`,
`[plugin.runtime.firecracker_pvm]` and removes Kata. The entrypoint rejects
missing PVM guest prerequisites; sandboxd independently verifies the host ABI.
Runsc systrap remains the default. This profile does not qualify Kata or
gVisor's KVM platform on PVM.

Helm selects `node.config.sandboxd.firecrackerBackend=pvm`
(`core.node.config.sandboxd.firecrackerBackend` in the umbrella chart):

```bash
helm template pvm-preview deploy/akernel/charts/core \
    --set node.config.sandboxd.firecrackerBackend=pvm \
    --show-only templates/node/configmap.yaml \
    --show-only templates/node/daemonset.yaml
```

Both selectors default to `kvm`. The setting applies to the node DaemonSet and
does not create separate KVM/PVM pools. Use dedicated nodes and `node.affinity`
or independently managed profiles in a mixed cluster. Do not convert a running
KVM pool by changing the value. Custom templates using automatic transformation
must retain the standard Firecracker section and binary entry.

For the dedicated qualification layout, place the actual writable filestore
and checkpoint paths on XFS with `filestore_dir_size=""` and
`filestore_xfs_enabled=true`, while sandboxd metadata and ADX/Redis state remain
on ext4. Check effective paths and mounts inside the node and L1: mounting only
`/home/akernel/sandboxd` does not relocate `/home/akernel/filestore`, and a
loop-backed filestore does not inherit its parent directory's filesystem.
Shared backing-device I/O limits still affect control and checkpoint disks;
separate filesystems alone do not isolate I/O.

Standalone defaults to EROFS with virtio-fs disabled. OCI/Nydus directory roots
require explicit `plugin.runtime.firecracker.virtiofs_enabled=true` in the
source profile before PVM transformation, and a compatible virtio-fs stack.
The dedicated PVM CI configuration omits unrelated runsc/Kata handlers.
Mixed-runtime acceptance must separately exercise degraded inventory and
recovery: a failing handler closes that class's advertisement/admission;
successful polling reopens it. Core metadata/storage failures can still close
global readiness.

Both image profiles resolve the bundled standalone deployment and select
sandboxd's resource socket for scheduling capacity. The PVM image additionally
applies `prepare-adx-config.py --pvm`, setting coordinator/adxlet
`rpc_timeout_seconds=90`. Custom or external ADX configurations must carry the
override themselves. The guest-agent startup budget remains 15 seconds;
extending an RPC cleanup budget does not authorize increasing that deadline.

The aggregate sandbox memory policy remains disabled by default for PVM until
its finite-cap profile is qualified on the matching maintained host. Standalone
can explicitly select `AKERNEL_PVM_MEMORY_LIMIT_ENABLED=true`; Helm uses
`node.config.sandboxd.pvmMemoryLimitEnabled=true` (under `core` in the umbrella
chart). Both default to false and apply only to the PVM backend. Select a
`total_reserve` suitable for the actual L1 in its sandboxd configuration before
opting in, then run the matching bounded PVM qualification. Ordinary runsc tests
and static profile checks do not qualify this combination, and the 16 GiB
deployment reserve is not a universal host OOM safety budget.

## Isolated qualification workflow

Ordinary sandboxd hardware-KVM CI and AKernel's runsc-only standalone image do
not qualify PVM. The independent [PVM workflow](../.github/workflows/pvm-e2e.yml)
requires a maintainer dispatch. Its unprivileged request job resolves allowed
maintained-branch or open-PR refs to immutable source/profile SHAs before
assigning an L1. Canonical workflow branches are `sandboxd:main` and
`AKernel:develop`; an explicit `refs/pull/N/head` must be open and target the
corresponding maintained branch. Fork dispatches can qualify their exact SHA.
A maintained-branch push registers the workflow without assigning a privileged
runner.

```bash
gh workflow run pvm-e2e.yml --repo inclusionAI/AKernel --ref develop
gh workflow run pvm-e2e.yml --repo inclusionAI/sandboxd --ref main
```

Use `-f source_ref=refs/pull/N/head` for a source PR. Sandboxd's
`host_profile_ref` selects AKernel develop or an open develop-targeting profile
PR; do not reuse an old PR number as a permanent profile input.

Provision a fresh owned L1 from the committed profile's `provisioning`,
`boot` and `fixtures` sections, retaining outer CPU/memory/I/O bounds and a
private Docker daemon. Register a repository-scoped ephemeral
`pvm-l1-ubuntu7` runner only inside that guest. The workflow does not provision
or modify the physical runner; all module/network operations stay inside the
L1. Keep VMX/SVM hidden for the required no-nested-virtualization coverage.

Stage the matched modules, original build receipt, ownership marker and
resolved local fixture addresses at the paths declared by the profile. Seed
the owned registry with the exact OCI/Nydus digests. Before loading modules,
the guard compares source/baseline, running kernel/image, target/prepared tree,
compiler/header evidence, original receipt digest and both module hashes
against the committed expected profile. It also verifies the ownership and
resource boundary and rejects substituted image versions, mixed registries
and unrelated network fixtures. A runner marker cannot choose accepted pins.

Initialize the pinned sandboxd submodule and clean its ignored build files
in the disposable CI checkout (`git -C src/sandboxd clean -ffdx`). Cleaning
only the parent leaves stale staged bundles. Record module-load intent before
the first `insmod`. Failures must remain failures; never fall back to hardware
KVM. Retain the expected profile, original receipt, actual source/ABI,
configuration, artifact hashes and test results.

Acceptance must cover:

- Boot/exec/delete; Full, XFS Incremental/SoftDirty and virtio-fs restore chains;
  writable host mounts, ACLs/DNAT and actual OCI/Nydus image-provider paths.
- ADX/SDK integration and bounded pressure with strict deletion checks, followed
  by empty control/runtime inventories and no held/failed-held resources.
  ADX currently requests Full snapshots explicitly; changing sandboxd's default
  does not qualify incremental snapshots through ADX.
- Backend advertisements, both cross-backend rejection directions, untagged
  legacy and missing/mismatched-frequency rejection, and same-PVM recovery.
- Strict/unsafe clock controls, memory integrity and POSIX timer progress with
  actual guest arguments and TSC-domain boundaries recorded. Expected unsafe
  negative controls are not qualified unsafe restores.
- Kernel WARN/Oops checks and owned-resource teardown. Ordinary unit/build tests
  and source inspection cannot replace these runtime checks.

Cleanup must remain available through a boundary-only ownership check when a
version check fails. Stop the owned deployment, network fixtures and runtime
mounts before unloading the owned module pair. The outer provisioner must stop
the L1 on success, failure or cancellation and preserve unrelated containers,
physical-host modules and backing images. Current qualification is limited to
the committed Intel/kernel/L1 profile; AMD, other kernels, GPU/S3, multi-node
cloud acceptance and broader workloads require separate evidence. A passing
restore does not guarantee uninterrupted tunnels or an application-wide
performance result.

## Checkpoint compatibility and rollback

Snapshots record the actual backend and VMM, guest kernel, initrd and virtiofsd
digests. Reject a different backend before starting the VMM. PVM refuses
untagged legacy artifacts; recreate them with the current stack rather than
relabeling manifests. Hardware KVM retains legacy behavior, so operators must
verify the origin of old untagged snapshots.

PVM requires a verifiable source TSC frequency and the same target frequency.
This gate does not establish raw-TSC continuity or heterogeneous compatibility.
The baseline is the same host/L1 boot and TSC domain with matching software;
any broader restore claim needs independent clock/CPU qualification.
Caller-owned writable host exports and their original backing files must
survive local checkpoint/restore.

On a host exposing VMX/SVM, drain workloads, stop PVM users, preserve artifacts,
unload `kvm_pvm` and the OOT `kvm`, then `modprobe kvm_intel` or
`modprobe kvm_amd` to restore the complete stock pair. Restart sandboxd and
accept workloads only after its hardware-KVM probe succeeds. An L1 without
nested virtualization has no hardware-KVM fallback; use an eligible hardware
node or runsc systrap. PVM snapshots cannot restore on rollback KVM.

## Optional DEBUGCTL module variant

The [reference patch](pvm/kvm-debugctl-backend-scope.patch) moves the common
`host_debugctl = get_debugctlmsr()` save into the VMX/SVM execution paths. PVM
already saves its host DEBUGCTL state in `pvm_vcpu_load()`; avoiding a redundant
MSR read can matter in an L1. The guest-bundle builder does not apply this
patch, and the committed host profile has `debugctl_applied=false`.

Prepare a separate clean export from the same pinned host profile:

```bash
set -euo pipefail
: "${PVM_HOST_OPTIMIZED_SOURCE:?select a fresh optimized module build path}"
PVM_DEBUGCTL_PATCH="$PWD/deploy/pvm/kvm-debugctl-backend-scope.patch"
python3 "$PVM_HOST_SOURCE/tools/pvm-oot/prepare.py" \
    --profile "$PVM_HOST_SOURCE/$PVM_HOST_TARGET_PROFILE" \
    --source "$PVM_HOST_SOURCE" --kernel-build "/lib/modules/$(uname -r)/build" \
    --cc gcc-15 --output "$PVM_HOST_OPTIMIZED_SOURCE"
git -C "$PVM_HOST_OPTIMIZED_SOURCE" apply --check "$PVM_DEBUGCTL_PATCH"
git -C "$PVM_HOST_OPTIMIZED_SOURCE" apply "$PVM_DEBUGCTL_PATCH"
make -C "/lib/modules/$(uname -r)/build" \
    M="$PVM_HOST_OPTIMIZED_SOURCE/arch/x86/kvm" PVM_OOT_MODE=1 CC=gcc-15 -j2 modules
```

Load both modules from that export using the dedicated-node procedure and
qualify the new pair independently. Record the patch and exact module identities
separately; preserve hardware-KVM restoration and never mix the optimized core
with stock vendor modules. Microbenchmarks are not production throughput or
AMD/SVM qualification. Record storage limits and control-plane contention when
measuring checkpoint latency; retain durability barriers and distinguish
instrumentation effects from runtime failures.
