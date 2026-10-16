# PVM-backed Firecracker nodes

PVM is an opt-in backend of the Firecracker adapter. Use `Sandbox(runtime="firecracker-pvm")` on a dedicated PVM node profile. The default `firecracker` class requires hardware KVM; sandboxd probes the actual `/dev/kvm` ABI before advertising either class. Agent DX forwards the requested runtime and consumes sandboxd's reported runtime inventory; no static label override replaces backend probing.

This profile requires the sandboxd change that adds `firecracker-pvm`, and a validated PVM guest bundle from `akernel-dev/firecracker`. Merge and pin the tested sandboxd revision before shipping an AKernel image. Default runtime release pins remain unchanged until a candidate passes sandboxd and AKernel validation and its exact bytes are promoted.

## Host and guest prerequisites

Use the existing distribution host/L1 kernel with a matched out-of-tree `kvm.ko` and `kvm-pvm.ko`. [`pvm/oot-host.env`](./pvm/oot-host.env) pins the host source, its generic 6.12 OOT baseline and the ordered Ubuntu target patch series. Apply that profile to a fresh source export before building. No PVM patch, rebuild or replacement of the host kernel binary is required. The qualified target is the original Ubuntu `7.0.0-30-generic` kernel on Intel x86-64; other kernels/configurations and AMD hosts need separate build and runtime qualification.

The OOT host branch does not contain the PVM guest implementation. The [Firecracker fork's `resources/akernel/pvm-kernel-versions.env`](https://github.com/akernel-dev/firecracker/blob/develop/resources/akernel/pvm-kernel-versions.env) independently pins strict-TSC guest source at `ca68979e554da5c483c627ced55cb7a55f605b11` on `hky1999/linux`. Use each source for its own role; do not substitute the host-only branch into the guest builder.

Install the running distribution kernel's headers, generated configuration and `Module.symvers`, and use its compiler version. KVM must be unloadable modules (`CONFIG_KVM=m`); a built-in KVM core cannot be replaced this way. The OOT build enables its PVM backend privately, so the distribution host config does not need `CONFIG_KVM_PVM`. The tested compiler is GCC 15.2.0. BTF was skipped because the target `vmlinux` was unavailable; no forced loading or symbol-version bypass was used.

```sh
source deploy/pvm/oot-host.env
: "${PVM_HOST_SOURCE:?select a new host-module source checkout path}"
: "${PVM_HOST_PREPARED_SOURCE:?select a fresh target module build path}"
test ! -e "$PVM_HOST_SOURCE"
git init "$PVM_HOST_SOURCE"
git -C "$PVM_HOST_SOURCE" fetch --depth=1 \
    "$PVM_HOST_SOURCE_REPOSITORY" "$PVM_HOST_SOURCE_COMMIT"
git -C "$PVM_HOST_SOURCE" checkout --detach FETCH_HEAD
test "$(git -C "$PVM_HOST_SOURCE" rev-parse HEAD)" = "$PVM_HOST_SOURCE_COMMIT"
git -C "$PVM_HOST_SOURCE" fetch --depth 1 \
    "$PVM_HOST_SOURCE_REPOSITORY" "$PVM_HOST_BASELINE_COMMIT"
python3 "$PVM_HOST_SOURCE/tools/pvm-oot/prepare.py" \
    --profile "$PVM_HOST_SOURCE/$PVM_HOST_TARGET_PROFILE" \
    --source "$PVM_HOST_SOURCE" --kernel-build "/lib/modules/$(uname -r)/build" \
    --cc gcc-15 --output "$PVM_HOST_PREPARED_SOURCE"
make -C "/lib/modules/$(uname -r)/build" \
    M="$PVM_HOST_PREPARED_SOURCE/arch/x86/kvm" PVM_OOT_MODE=1 CC=gcc-15 -j2 modules
```

Preparation verifies the baseline contents, patch hashes/order and exact target config, `Module.symvers`, release and compiler. Changed inputs fail rather than selecting an inferred compat path. Retain `.pvm-oot-prepared.json` with the build logs and module hashes. A new Ubuntu ABI, including `-31`, needs its own profile and qualification. The profile replaces kernel-version conditions in the baseline; it does not automatically inherit newer stock KVM fixes.

Keep the complete distribution networking/storage module package available. AKernel standalone needs IPv4/IPv6 legacy filter tables, conntrack, connmark/CONNMARK, bridge filtering, ipset and TUN. These remain stock distribution modules; do not replace them with a trimmed PVM kernel/module tree. An earlier custom-host experiment failed at `modprobe iptable_filter` after omitting these dependencies. The stock-kernel OOT qualification retains them without rebuilding the host.

PVM does not require VMX/SVM, including inside an L1 without nested hardware virtualization. The L1 still needs FSGSBASE, RDTSCP and CMPXCHG16B; CPUID faulting is required when interception is enabled. The tested host uses `nokaslr pti=off` with FRED not exposed, and KASAN is unsupported. A current boot that fails these constraints may require a boot-parameter change and reboot of the same kernel; OOT removes the host binary customization requirement, not these prerequisites. Module signing must satisfy the existing host policy.

Prepare a dedicated node or isolated L1, drain workloads and stop sandboxd and all VMMs before switching modules. PVM and the stock VMX/SVM vendor modules are mutually exclusive. After confirming no KVM users remain, load the matched pair:

```sh
for vendor in kvm_intel kvm_amd; do
    if test -d "/sys/module/$vendor"; then modprobe -r "$vendor"; fi
done
if test -d /sys/module/kvm; then modprobe -r kvm; fi
modprobe irqbypass
insmod "$PVM_HOST_PREPARED_SOURCE/arch/x86/kvm/kvm.ko"
insmod "$PVM_HOST_PREPARED_SOURCE/arch/x86/kvm/kvm-pvm.ko"
```

Restart sandboxd to reprobe the backend before accepting workloads and keep the pair fixed for its lifetime. The OOT core must not be combined with distribution VMX/SVM modules. It is still a two-module replacement, not a single independent `pvm.ko` attached to the stock KVM core. This AKernel profile does not automatically change host kernels, boot settings, modules, networks or existing clusters.

Build the guest bundle with Firecracker's `AKERNEL_KERNEL_PROFILE=pvm` option or candidate workflow `kernel_profile=pvm` choice. The builder verifies PVM guest options and common AKernel filesystem, network and virtio requirements. Its guest config, source provenance, licenses and checksums are packaged together. Build the initrd from the consuming sandboxd revision so host and guest agent protocols match.

The guest must enable `CONFIG_KVM_GUEST=y`, `CONFIG_PVM_GUEST=y`, `CONFIG_X86_PIE=y`, and `CONFIG_X86_INTEL_MEMORY_PROTECTION_KEYS=y`, plus the common AKernel guest fragment. Use Firecracker's resolved `resources/akernel/kernel/pvm-guest.config`, not an unmodified upstream minimal PVM config: the latter can omit AKernel's filesystem/virtio requirements and disable MPK. On a PKU-capable host, enabling guest MPK aligns guest and host `XCR0.PKRU`; otherwise a nested deployment can incur two intercepted `XSETBV` instructions on each guest/host transition. The tested machine reports an xstate mask of `0x2ff` in both kernels. Check the actual xstate masks on the target CPU; `0x2ff` is not a portable CPU requirement. The pinned OOT baseline includes the upstream OSPKE guard fix from `virt-pvm/linux` commit `f157a4af83471636664d26457b048cf63ead7c43`: the host PKRU wrappers and temporary guest-PKU override require OS enablement. Earlier module pins can fault at `RDPKRU` with host `nopku`; updating only the guest kernel does not fix the host modules. Keep guest MPK enabled and treat host PKU policy separately. Enabling the guest xstate bit does not qualify enforcement of guest `pkey_mprotect` permissions, which remains incomplete in this PVM revision.

## Optional OOT-module DEBUGCTL optimization

The frequently described "one-line optimization" is the removal of this unconditional save from the common host KVM `vcpu_enter_guest()` path in `arch/x86/kvm/x86.c`:

```c
vcpu->arch.host_debugctl = get_debugctlmsr();
```

The value is consumed by VMX/SVM, while PVM already saves its own host DEBUGCTL state in `pvm_vcpu_load()`. An L1 can incur an outer-hypervisor MSR exit for each unnecessary `IA32_DEBUGCTL` (`0x1d9`) read, including first-write faults during exact dirty-page tracking. The [backend-scoped reference patch](pvm/kvm-debugctl-backend-scope.patch) moves the save into `vmx_vcpu_run()` and `svm_vcpu_run()`, preserving those source paths. It applies to the pinned OOT source and changes the module source only. OOT mode builds the core and PVM backend, excluding VMX/SVM; the guest-bundle builder does not apply the patch.

To build this optional variant, prepare a separate clean target export from the same pinned profile and build the matched modules against the unchanged distribution host:

```sh
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

Load both modules from `PVM_HOST_OPTIMIZED_SOURCE` during the dedicated-node procedure above and requalify the optimized pair. Preserve hardware-KVM restoration; do not discard its saved value or mix the OOT core with stock vendor modules. To return to hardware KVM, stop PVM users, unload both OOT modules and load the complete original stock pair. The stock kernel and its vendor modules remain unchanged. SVM hardware execution was not covered by the Intel-only experiment.

An earlier controlled nested microbenchmark on a Cascade Lake host measured 96 MiB first dirty writes at about 61.5 ms with guest MPK alone and 35.9 ms with MPK plus this backend-scoped patch, compared with about 50 ms for nested hardware KVM. These earlier in-tree results do not qualify OOT performance, AKernel service performance or direct bare-metal behavior. Current stock-kernel OOT qualification uses the unoptimized pair. Record the patch and exact module identities separately when validating an optimized pair.

## Candidate image and node selection

The current PVM pin selects the published `v1.16.1-akernel.4` archive, verified byte-identical to the qualified candidate. Its public download requires no token or Actions artifact. To validate a future unreleased PVM bundle, use sandboxd's separate `third_party/firecracker-pvm.json`. The candidate helper verifies the exact Actions run, artifact/archive digests, source provenance, binary/configuration digests and required PVM options before staging the original bytes. Caller environment variables cannot replace those pins. Keep the candidate distinct from ordinary KVM release pins until the exact bytes pass both repositories and are promoted. The selected guest profile does not change the host backend.

```sh
FIRECRACKER_KERNEL_PROFILE=pvm AKERNEL_TARGETARCH=amd64 \
    deploy/scripts/build-image.sh --repository pvm-validation --tag candidate
```

After validation and promotion, pin the published URL and verified digest in sandboxd and advance AKernel's gitlink. Do not rebuild the candidate during promotion. PVM guest images require `linux/amd64`; ARM64 retains the mainline runsc/runc profile.

Standalone accepts `AKERNEL_FIRECRACKER_BACKEND=pvm` alongside an image containing the PVM bundle. Its generated config selects `firecracker-pvm`, uses `[plugin.runtime.firecracker_pvm]`, and removes Kata. The entrypoint rejects a PVM selection when guest prerequisites are absent; sandboxd independently verifies the host ABI. Runsc remains the default runtime on systrap. This profile does not qualify Kata or gVisor's KVM platform on PVM.

Helm selects `node.config.sandboxd.firecrackerBackend=pvm` (`core.node.config.sandboxd.firecrackerBackend` in the umbrella chart). Render before applying:

```sh
helm template pvm-preview deploy/akernel/charts/core \
    --set node.config.sandboxd.firecrackerBackend=pvm \
    --show-only templates/node/configmap.yaml \
    --show-only templates/node/daemonset.yaml
```

The profile is shared by the node DaemonSet; it does not automatically create separate KVM and PVM pools in one release. Use dedicated nodes and `node.affinity` in an isolated deployment, or independently managed node profiles in a mixed cluster. Do not convert a running KVM pool by changing this value. Labels come from initialized handlers; manually setting `sandbox.runtime` cannot make a backend available. Both selectors default to `kvm`. Custom templates using automatic transformation must retain the standard Firecracker section and binary entry.

## Checkpoint and rollout boundaries

New snapshots record the actual backend and software digests. A different backend is rejected before starting the VMM. PVM conservatively refuses untagged legacy artifacts. Hardware KVM retains legacy behavior; operators must confirm provenance because old experimental PVM snapshots also lack a backend tag. Recreate those snapshots using the new runtime rather than relabeling manifests.

The tested PVM backend has no TSC frequency scaling. PVM snapshots need a verified source frequency and the target must report the same frequency; the gate rejects a mismatch or unverifiable frequency. This does not qualify heterogeneous CPU migration. The baseline is restore within the same host/L1 boot and TSC domain with identical VMM, kernel, initrd and virtiofsd; qualify cross-host CPU compatibility separately. Writable host exports remain caller-owned and require local checkpoint/restore.

Before expansion, run sandboxd Full, XFS Incremental, local virtio-fs and shared host-mount suites; check both advertisements, both cross-backend rejection directions, legacy/missing-frequency rejection and same-PVM recovery. Then run SDK integration with `AKERNEL_TEST_RUNTIME=firecracker-pvm` and pressure with `--runtime firecracker-pvm`. OCI/Nydus requires real image-provider tests; directory roots do not cover that path.

For hardware-KVM rollback on a host that exposes VMX/SVM, drain workloads, stop PVM users, preserve caller-owned artifacts, unload `kvm_pvm` and the OOT `kvm`, then `modprobe kvm_intel` or `modprobe kvm_amd` to restore the complete stock pair. Restart sandboxd and select the default runtime after its ABI probe succeeds. An L1 without nested hardware virtualization has no hardware-KVM fallback; use an eligible hardware node or the existing runsc systrap profile. PVM snapshots require a compatible PVM stack and cannot restore on rollback KVM. Cluster deployment remains a deliberate operator action.

## Qualification without nested hardware virtualization

Boot an isolated L1 with the original distribution kernel and no VMX/SVM exposure, for example QEMU `-cpu host,-vmx,-svm`. Keep the other PVM CPU prerequisites exposed and use the supported host boot settings. Check the L1 CPUID bits, verify the stock vendor module cannot load, then load the OOT pair and probe the PVM ABI. Perform all module operations inside the L1; the physical test server's kernel, KVM modules and active cluster stay untouched.

Opening `/dev/kvm` alone is insufficient. Run boot/exec/delete, Full and Incremental/SoftDirty checkpoint/restore, actual OCI/Nydus image-provider tests, the complete private control plane/SDK suite, and bounded pressure with strict deletion checks. Record the stock-kernel hash, module hashes, CPU exposure, guest bundle/agent digests and tested revisions. Check kernel dmesg and restore/unload cleanup. CI runtime runners must create their own disposable L1 rather than replacing the runner's live KVM core.

The stock-kernel qualification also confirms the matched pair does not require nested VT-x/AMD-V. It does not cover every distribution ABI, CPU architecture, Secure Boot setup, AMD execution, heterogeneous migration or the complete DeepSWE oracle workload. The historical first 15-second guest-agent timeout remains unlocated. Large checkpoint/reload can briefly exceed the tunnel heartbeat budget before automatic reconnection; passing restore tests do not guarantee uninterrupted connections.

## Fork development and TSC assessment

The current PVM integration is maintained on `hky1999/AKernel:feat/pvm-candidate-adx` and `hky1999/sandboxd:feat/pvm-candidate-adx`. The sandboxd gitlink and `.gitmodules` URL both refer to the fork. The host profile pins the OSPKE-fixed OOT source and the NX recovery-worker retry backport on `hky1999/linux`; no upstream AKernel/sandboxd PR or default runtime release is promoted by this branch.

The companion Firecracker fork now selects strict-TSC guest source `ca68979e554da5c483c627ced55cb7a55f605b11`, containing the three TSC patches and the effective-command-line parser fix. Strict mode is the default: TSC is marked unstable, deadline/adjust features are disabled, pvclock's stable-TSC fast path is ignored and negative pvclock TSC deltas are clamped. Mode selection runs after command-line append/override, before kvmclock initialization; the last occurrence wins and empty/unknown values fall back to strict. `pvm_tsc=unsafe` now works, retains the original clock/timer selection and must not be used to qualify TSC-domain-changing restores. Raw RDTSC/RDTSCP can still move backwards; strict mode does not provide TSC scaling or heterogeneous CPU migration support.

The default runtime release tuple in sandboxd remains unchanged. Setting `FIRECRACKER_KERNEL_PROFILE=pvm` selects the independently locked, published PVM bundle through the candidate helper; ordinary KVM builds keep their existing release manifest. The descriptor now uses the persistent `v1.16.1-akernel.4` Release URL and the unchanged qualified archive digest. Future updates must promote the tested original bytes and replace temporary Actions pins before shipping. Do not replace ordinary KVM defaults with a PVM archive. The older guest still requires the same host/L1 boot and TSC domain; matching frequency alone does not establish continuity after an L1 restart. The strict guest source is separate from the OOT host pin.

Assess clock-heavy workloads before rollout: isolated fast-path measurements increased high-resolution `clock_gettime` cost from about 28 ns to 382 ns per call in strict mode, while coarse reads retained their fast path. This is a per-clock-read cost, not an application-wide slowdown. A new guest digest or kernel argument changes checkpoint compatibility; retain the old stack for old snapshots or drain and recreate sandboxes rather than bypassing the gates. DEBUGCTL remains an optional host-module patch.

## PVM CI assessment

The default sandboxd Actions matrix exercises hardware KVM; AKernel's standalone Actions image excludes Firecracker. Neither constitutes PVM runtime qualification. Keep those jobs intact. PVM needs a separate opt-in qualification workflow that provisions a bounded disposable L1, loads the matched OOT pair only inside it, and runs sandboxd Full, Incremental and virtio-fs checks followed by AKernel SDK, real OCI/Nydus and bounded pressure checks. Record the target kernel/config/symbol-table/compiler, host-module and guest-bundle hashes, actual PVM ABI, restore-integrity results and complete teardown. Failure to prepare the PVM backend must fail the job, not silently run hardware KVM.

A pinned final PVM candidate and the isolated-L1 provisioning inputs are prerequisites for that workflow. Build-only and unit-test jobs can run on ordinary runners; runtime jobs must retain VMX/SVM-hidden coverage and never install replacement KVM modules on a shared runner or active cluster host. Promote the exact downloaded candidate bytes only after both repositories pass. The fork now has independent `.github/workflows/pvm-e2e.yml` jobs in sandboxd and AKernel. They require a separately provisioned, ephemeral `pvm-l1-ubuntu7` runner inside the owned disposable L1, the exact marker/module hashes, the private Docker socket, and pinned local OCI/Nydus fixtures. The provisioner registers one repository-scoped runner per job and stops the L1 afterward; these jobs do not provision or modify the physical runner themselves. The existing hardware-KVM matrix remains intact, and DEBUGCTL is not enabled by either job.

The OSPKE-fixed host profile was requalified on the original Intel Ubuntu `7.0.0-30-generic` L1 with VMX/SVM hidden. The old matched pair reproduced the `nopku` RDPKRU invalid-opcode fault; the new pair passed five load/unload cycles and three 96 MiB Full/Diff/merged-memory/new-VMM restore cases per host mode, with and without `nopku`. Each case checked the expected marker on every workset page twice, and fixed-mode kernel logs contained no WARN/Oops. All eight negative preparation cases were rejected, and Git-source/source-export preparation produced the same tree. No DEBUGCTL patch was applied. This guard-fix run does not replace final bundle, SDK/image-provider, pressure, other-kernel or AMD qualification.

## Strict-TSC guest qualification (2026-10-14)

The strict guest at `ca68979e554da5c483c627ced55cb7a55f605b11` was built with GCC 11.4 and the PVM bundle configuration, retaining KVM/PVM guest, PIE and MPK support. Its immutable source archive SHA-256 is `f3ed347a283bd55d7c8def667d5e9c24e7226c88f6eebf64e5001c81963b8d46`. Default, built-in append and built-in override configurations compiled successfully; the changed setup/kvmclock objects also compiled with `CONFIG_PVM_GUEST=n`.

BareMetal1 qualification ran entirely inside a bounded disposable L1 (stock Ubuntu `7.0.0-30-generic`, VMX/SVM hidden, OSPKE-fixed OOT modules from source `0994bb5a0808c2c0ea0c58e8fe21828f1afe29df`, no optional DEBUGCTL patch). Eleven boot cases verified default/explicit strict, actual unsafe, empty/unknown values, duplicate-option precedence and built-in append/override behavior. Full+Diff restores on 1/2-vCPU strict guests and a 2-vCPU unsafe guest verified all 24,576 pages of a 96 MiB marker region and timer progress within the same TSC domain.

After restarting the owned L1, both 1/2-vCPU strict snapshots observed raw TSC moving backwards by roughly 17 minutes at the same verified `2,499,998` kHz frequency. MONOTONIC/REALTIME/BOOTTIME continued advancing, the 100-by-1-ms sleep loop completed in about 107 ms and all memory markers remained intact. Both unsafe snapshots with `clocksource=tsc` instead stopped advancing their POSIX clocks and timed out the sleep loop after 8 seconds. This confirms that strict mode protects the tested kernel-clock/timer path; it does not establish raw-TSC continuity or general cross-host migration.

Targeted sandboxd EROFS Full and virtio-fs Incremental/SoftDirty suites passed ten-checkpoint and checkpoint-after-restore chains, including missing-base fallback. The private control plane/SDK passed 20 OCI/Nydus checkpoint/reload checks with zero failures or skips. These tests reused the qualified VMM and sandboxd/agent runtime code at `fff3dfa2583c21107639f4fe74b0c4f27571a8ff`, replacing only the guest with the verified new kernel. They are manual strict-guest qualification, not an Actions-produced bundle promotion, a fresh full pressure campaign or Agent DX E2E.

The same three Full+Diff integrity/timer cases also passed after booting that L1 with `nopku`, confirming the strict guest works with the OSPKE-fixed OOT host when OS protection keys are disabled. Module unload completed in both host modes, and dmesg contained no kernel BUG/Oops/WARN_ON reports. The physical host kernel/module hashes and loaded module addresses stayed unchanged; all five pre-existing AKernel node containers retained their PIDs/start times and zero restarts. The disposable L1 and its monitor were stopped after collecting evidence.

## Pinned PVM bundle and Agent DX integration

The integration branch includes the Agent DX environment-API changes from AKernel develop, with platform and Execd source pinned at `ce80a189f352f1ee2ebb6a9f30ee218f2ae896d3`. AKernel and sandboxd changes remain on `hky1999/AKernel:feat/pvm-candidate-adx` and `hky1999/sandboxd:feat/pvm-candidate-adx`; the gitlink records the exact sandboxd input. No upstream AKernel/sandboxd branch is submitted or merged by this work.

The canonical [Firecracker develop candidate](https://github.com/akernel-dev/firecracker/actions/runs/38717545796) built `v1.16.1-akernel.4` from `6315fe1ca7f1847cfc682590be2e05e97d76fb22` after [Firecracker PR #9](https://github.com/akernel-dev/firecracker/pull/9) landed. The original archive SHA-256 is `150571da7a370d647e94011768acceac1b6920a2f2e7349f0e86b05b340e112f`, its VMM is `41133331123c05d635a1a4a61a1eb41f078e9a216b5a197c1398e4e64e957cbf`, and its guest is `739c76932c12b1c66317207fae27cbc1f97a23b64027b24fd1c60e3589a0a0b0`. Candidate provenance and profile are independently locked in sandboxd. The VMM, guest and resolved guest configuration match the preceding feature candidate byte for byte; the new archive records the canonical source/run provenance. Promotion retains the develop/manual gates and never rebuilds the bundle. The [promotion run](https://github.com/akernel-dev/firecracker/actions/runs/38873086859) published those original bytes as [the opt-in PVM prerelease](https://github.com/akernel-dev/firecracker/releases/tag/v1.16.1-akernel.4) after [sandboxd PVM CI](https://github.com/hky1999/sandboxd/actions/runs/38849187026), [Agent DX CI](https://github.com/hky1999/AKernel/actions/runs/38849234836) and the NX-fixed-module Full/SoftDirty/SoftDirty regression passed. The archive and both sidecars were downloaded through their public URLs without credentials and verified before advancing this gitlink.

Selecting `FIRECRACKER_KERNEL_PROFILE=pvm` reads sandboxd's independent `third_party/firecracker-pvm.json` and stages the verified original archive through `prepare-firecracker-pvm.py`. Actions downloads require `GH_TOKEN`; promoted public release downloads require no token or unexpired Actions artifact. The token remains outside the Docker build context and is not forwarded to signed blob storage. `PVM_CANDIDATE_ARCHIVE` accepts a previously downloaded copy without replacing any locked digest. The build checks archive, provenance, inner binary/configuration digests and required PVM options. Ordinary KVM keeps its release manifest. Build the guest-agent initrd from the consuming sandboxd revision.

Generic PVM host and guest fixes belong in `virt-pvm/linux`: host fixes on its OOT host line and guest strict-TSC fixes on its guest line. The current `hky1999/linux` inputs are temporary reviewed sources until PVM maintainers accept them. Repin upstream commits and source-archive digests after that review and requalify the resulting bundle. AKernel-specific packaging belongs in `akernel-dev/firecracker`; creating another permanent Linux fork merely to rename the temporary source does not resolve maintenance ownership.

## Qualified PVM deployment profile

The tested profile is an unchanged Ubuntu `7.0.0-30-generic` L1 with VMX/SVM hidden, four visible vCPUs and 8 GiB guest RAM, bounded outside the L1 to two CPUs and 10 GiB. A follow-up audit found that the outer container also inherited block-I/O weight 100 and both read/write limits of 20 MiB/s on the physical backing device; the previous qualification record omitted those limits. Both L1 disks share that backing device and budget, so ext4/XFS separation does not isolate control-plane I/O from snapshot writes. It loads the OSPKE-fixed matched OOT pair from `0994bb5a0808c2c0ea0c58e8fe21828f1afe29df` only inside the L1. The actual PVM ABI reports `2499998` kHz and no TSC scaling. DEBUGCTL is unapplied. The outer host's stock kernel/KVM and existing nodes are outside all module and network operations.

Use XFS for the actual writable filestore and checkpoint paths. In the qualification fixture, `/home/akernel/filestore` and `/home/akernel/adx/checkpoints` bind to the same XFS disk with `filestore_dir_size=""` and `filestore_xfs_enabled=true`; sandboxd metadata and ADX/Redis state stay on ext4. Binding only `/home/akernel/sandboxd` does not relocate the filestore. The develop default creates a separate 50 GiB ext4 loop-backed filestore, so check the effective configuration and mount types rather than assuming the data-directory filesystem applies everywhere.

The dedicated PVM qualification omits runsc and Kata runtime entries from its temporary configuration. The repository's ordinary runtime defaults remain intact. A deployment that needs to reproduce this qualification must explicitly select `firecracker-pvm` and configure only the qualified handlers. The original mixed-runtime qualification observed a `runsc list` timeout extending housekeeping beyond its five-second process-health threshold, rejecting PVM reloads. The follow-up sandboxd change polls handlers concurrently with independent deadlines and closes only the failed class's admission/advertisement; successful polls reopen it and deletion remains available. Local storage stalls and other core-module failures still affect process readiness. Mixed-runtime acceptance must distinguish backend inventory failures from shared storage/control-plane pressure.

The PVM image selects `builder/config/adx-pvm.yaml`, setting coordinator and adxlet `rpc_timeout_seconds` to 90. Custom/externally supplied ADX configurations must carry these overrides themselves. The ordinary KVM image keeps its original ADX profile. This is a bounded runtime cleanup/reload budget: a recorded cleanup took 30.63 seconds and exceeded the old 30-second limit. The guest-agent startup budget stays at 15 seconds.

## Real Actions qualification

The [independent sandboxd PVM job](https://github.com/hky1999/sandboxd/actions/runs/38558942312), source `54d7fd878abe93db48b755f9063f7fdf3bc3d728`, passed Full, Incremental and virtio-fs suites with ten consecutive checkpoints each. It covered checkpoint-after-restore chains, OCI image-provider paths, network ACLs/DNAT, quota and EROFS/directory writable host mounts, rotation and read-offset preservation. All three exits were zero; host-kernel WARN/Oops checks and module cleanup passed. The external Redis DNAT client now waits for the suite's explicit Redis-ready stage before starting the existing DNAT readiness budget; earlier checkpoint work no longer consumes that budget.

The [Agent DX PVM job](https://github.com/hky1999/AKernel/actions/runs/38573566452), source `433ffe5df1c4282b1452bb5b39b5ce6ca7990904`, built and verified the complete node image and passed all 13 standalone groups: 37 unittest cases plus two examples, with no skips. The OCI and Nydus integration paths each included a successful 2 GiB Full checkpoint/reload and reverse-tunnel recovery; the separate Nydus run passed all nine tests. OCI and Nydus pressure each completed 60/60 create/execute/delete transactions with at most four concurrent sandboxes, 512 MiB per guest and a requested 60-second campaign. Control/runtime inventories were empty afterward; a read-only audit of the actual Redis control hash found no remaining held or failed-held resources.

The qualified node image ID is `sha256:5d0c0064115c0a90c53c8807fddea98978cec640828eb6234ad74562a5037bbd`. Its initrd SHA-256 is `360a0b3bc1faf51fefd2e389208622d1343e8e020aa271a306cf8fbf698cad1b`. The image's VMM, guest and configuration matched the original candidate. The fork workflows use a separately provisioned ephemeral `pvm-l1-ubuntu7` runner and private Docker socket; they do not install replacement modules on a shared physical runner. They require real PVM preparation and fail instead of falling back to hardware KVM. The current qualification covers this Intel/kernel/L1 profile, not AMD, other host kernels, GPU/S3 or multi-node cloud acceptance.

## Remaining limitations

The ADX runtime adapter currently sends `snapshot_type="Full"` explicitly in `platform/adxlet/src/sandboxd.rs`; changing the sandboxd default to Incremental does not override the request. Exposing a configurable snapshot type through ADX remains separate work, even though sandboxd's independent Incremental suite passes.

A successful 2 GiB Full checkpoint in the bounded layered-QCOW L1 took about 103.5 seconds (54.3 seconds snapshot, 49.0 seconds seal); its restore took about 4.2 seconds. Earlier incorrect storage/mixed-runtime fixtures also produced admission/readiness failures. Preserve those failed artifacts and distinguish their configuration from the successful dedicated profile. The inherited outer 20 MiB/s read/write budget is material: writing 2 GiB at that rate alone is about 102.4 seconds. These results do not establish production throughput or attribute the measured disk/writeback latency to DEBUGCTL. The follow-up diagnostics split `seal` into manifest, base adoption, state persistence and writeback submission; debug timings distinguish state-file `fsync` from manifest publication and native snapshot work from cgroup moves. The optional DEBUGCTL patch remains separate.

The historical one-off 15-second guest-agent readiness timeout has not been reproduced or assigned a confirmed root cause. Registry-resolution timeouts, missing CI credentials, interrupted sandboxd startup and the independently fixed deadline race are separate findings; none proves that historical event fixed.

## Formal-bundle TSC-domain recheck

The same Actions-produced VMM and guest bytes passed seven effective command-line mode cases and three 1/2-vCPU same-domain Full+Diff restore cases with a dedicated 256 MiB clock-probe VM and a 96 MiB marker workset. This clock initrd is separate from the qualified SDK guest-agent initrd. After restarting only the disposable L1, the host ABI and `2499998` kHz frequency remained identical while raw TSC moved backward by about 4.1 hours. Both 1/2-vCPU strict restores preserved all 24,576 workset pages, nondecreasing POSIX clocks and 100 timer iterations in about 107–108 ms. Both explicit unsafe/TSC controls froze their clocks and hit the expected eight-second timer timeout. The negative controls are expected failures, not qualified unsafe restores. Raw RDTSC/RDTSCP remains outside the strict POSIX-clock guarantee. No DEBUGCTL patch was applied.

## Checkpoint latency and runtime isolation follow-up (2026-10-15)

A matched comparison used the same Intel Ubuntu `7.0.0-30-generic` disposable L1, four visible vCPUs, 8 GiB RAM, outer two-CPU/10-GiB limits and the inherited 20 MiB/s read/write cap. VMX was exposed in both arms to permit the hardware-KVM baseline; only the drained L1's matched KVM module pair and runtime class changed. Every test guest used one vCPU, 2 GiB RAM, a 256 MiB writable-layer limit, the same pinned OCI fixture and identical VMM/guest/initrd/sandboxd binaries. The actual backend probe distinguished hardware KVM from PVM. DEBUGCTL was not applied.

| Checkpoint storage | Hardware KVM Full checkpoint RPC | PVM Full checkpoint RPC | Validation |
| --- | --- | --- | --- |
| XFS on the capped backing disk | 79.059 / 71.926 / 83.740 seconds | 65.384 / 96.512 / 94.022 seconds | Three Full checkpoint/restore cases per backend; restored file markers verified |
| tmpfs diagnostic, outer cap unchanged | 4.324 seconds | 4.414 seconds | One 2 GiB Full checkpoint per backend; snapshot-only control |

The XFS restores took 2.224–2.662 seconds on hardware KVM and 2.441–2.868 seconds on PVM. These small, storage-limited samples do not establish a backend throughput ranking. The tmpfs control kept the filestore on XFS and the control state on ext4; mount types were checked both inside the node and in the L1. It isolates checkpoint-storage waits and is not a production storage recommendation or a RAM-backed restore qualification.

The new PVM third-round timings reproduced a 49.155-second `seal`: manifest work was 4 ms, base adoption under 1 ms, state persistence 49.150 seconds and writeback queue submission under 1 ms. The atomic-write diagnostics and syscall trace located the persistence wait in the small `state.json` file's `fsync`. Another PVM case spent 27 seconds reading/digesting small components and 25 seconds persisting state. The recorded VM-state and virtio-fs state components were only about 14 KiB and 445 bytes; guest memory and the writable layer are not hashed by this sealing path. Shared backing-device throttling, queued writeback and metadata/read waits explain the long tail; the original 103.5-second run was not itself microprofiled. No durability barrier was removed.

A delayed `runsc list` was injected without changing its production ten-second inventory budget or the 15-second guest-agent budget. The isolated runtime fault left sandboxd `SERVING` and PVM advertised while removing runsc. A PVM checkpoint on tmpfs succeeded in that state. A separate real Agent DX/SDK check completed three 512 MiB PVM create/execute/delete transactions while runsc remained degraded; a direct new runsc request returned `Unavailable`. After the fault was removed, runsc advertisements recovered. Runtime inventories were empty and the actual Redis control hash had no held or failed-held resources. Core metadata/storage stalls under the deliberately shared I/O cap can still correctly close global readiness; the change isolates runtime inventory failures without suppressing those core health checks.

The accepted comparison used `strace -f -b execve` and verified Firecracker `TracerPid: 0`. Earlier probes that also traced the VMM produced startup/restore timeouts and are excluded from qualification; the exact failed snapshot later restored successfully. All six final Full restores passed with the guest-agent timeout unchanged. This does not close the separately recorded historical one-off 15-second timeout. The experiment used the formal candidate VMM/guest bytes with an instrumented sandboxd derived from runtime code commit `3040dd5265ce938cc7fa2c40aafb64af7c0da12b`; it did not promote a new release or rerun the complete DeepSWE workload.

### NX recovery-worker startup fix

The OOT host baseline now includes upstream Linux commit `916b7f42b3b3b539a71c204a9b49fdc4ca92cd82`. A non-fatal signal during the first `KVM_RUN` can interrupt creation of the NX recovery worker; the old core converted this error to permanent `ENOMEM`. The backport preserves the original error and retries initialization on a subsequent run. The Ubuntu target adaptation series and optional DEBUGCTL patch are unchanged. Requalify the new matched module pair before promoting a PVM candidate. Sandboxd also reports a VMM exit during startup or restore promptly, including blocked agent I/O, instead of waiting for the 15-second agent deadline.
