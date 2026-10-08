# PVM-backed Firecracker nodes

PVM is an opt-in backend of the Firecracker adapter. Use `Sandbox(runtime="firecracker-pvm")` on a dedicated PVM node profile. The default `firecracker` class requires hardware KVM; sandboxd probes the actual `/dev/kvm` ABI before advertising either class. YuanRong's existing capability snapshot and `sandbox.runtime` placement labels carry the distinct classes without a static label override.

This profile requires the sandboxd change that adds `firecracker-pvm`, and a validated PVM guest bundle from `akernel-dev/firecracker`. Merge and pin the tested sandboxd revision before shipping an AKernel image. Default runtime release pins remain unchanged until a candidate passes sandboxd and AKernel validation and its exact bytes are promoted.

## Host and guest prerequisites

Use the existing distribution host/L1 kernel with a matched out-of-tree `kvm.ko` and `kvm-pvm.ko`. The host-module source is `virt-pvm/linux`'s `pvm-6.12-host-oot` branch, with the tested target-version compatibility changes pinned in [`pvm/oot-host.env`](./pvm/oot-host.env). No PVM patch, rebuild or replacement of the host kernel binary is required. The qualified target is the original Ubuntu `7.0.0-30-generic` kernel on Intel x86-64; other kernels/configurations and AMD hosts need separate build and runtime qualification.

The OOT host branch does not contain the PVM guest implementation. Firecracker's `resources/akernel/pvm-kernel-versions.env` independently pins the PVM guest source at `58902213f660d7f8d75eb9f08e6c3ff7e4a3721d`. Use each source for its own role; do not substitute the host-only branch into the guest builder.

Install the running distribution kernel's headers, generated configuration and `Module.symvers`, and use its compiler version. KVM must be unloadable modules (`CONFIG_KVM=m`); a built-in KVM core cannot be replaced this way. The OOT build enables its PVM backend privately, so the distribution host config does not need `CONFIG_KVM_PVM`. The tested compiler is GCC 15.2.0. BTF was skipped because the target `vmlinux` was unavailable; no forced loading or symbol-version bypass was used.

```sh
source deploy/pvm/oot-host.env
: "${PVM_HOST_SOURCE:?select a new host-module source checkout path}"
test ! -e "$PVM_HOST_SOURCE"
git init "$PVM_HOST_SOURCE"
git -C "$PVM_HOST_SOURCE" fetch --depth=1 \
    "$PVM_HOST_SOURCE_REPOSITORY" "$PVM_HOST_SOURCE_COMMIT"
git -C "$PVM_HOST_SOURCE" checkout --detach FETCH_HEAD
test "$(git -C "$PVM_HOST_SOURCE" rev-parse HEAD)" = "$PVM_HOST_SOURCE_COMMIT"
make -C "/lib/modules/$(uname -r)/build" \
    M="$PVM_HOST_SOURCE/arch/x86/kvm" PVM_OOT_MODE=1 -j2 modules
```

Keep the complete distribution networking/storage module package available. AKernel standalone needs IPv4/IPv6 legacy filter tables, conntrack, connmark/CONNMARK, bridge filtering, ipset and TUN. These remain stock distribution modules; do not replace them with a trimmed PVM kernel/module tree. An earlier custom-host experiment failed at `modprobe iptable_filter` after omitting these dependencies. The stock-kernel OOT qualification retains them without rebuilding the host.

PVM does not require VMX/SVM, including inside an L1 without nested hardware virtualization. The L1 still needs FSGSBASE, RDTSCP and CMPXCHG16B; CPUID faulting is required when interception is enabled. The tested host uses `nokaslr pti=off` with FRED not exposed, and KASAN is unsupported. A current boot that fails these constraints may require a boot-parameter change and reboot of the same kernel; OOT removes the host binary customization requirement, not these prerequisites. Module signing must satisfy the existing host policy.

Prepare a dedicated node or isolated L1, drain workloads and stop sandboxd and all VMMs before switching modules. PVM and the stock VMX/SVM vendor modules are mutually exclusive. After confirming no KVM users remain, load the matched pair:

```sh
for vendor in kvm_intel kvm_amd; do
    if test -d "/sys/module/$vendor"; then modprobe -r "$vendor"; fi
done
if test -d /sys/module/kvm; then modprobe -r kvm; fi
modprobe irqbypass
insmod "$PVM_HOST_SOURCE/arch/x86/kvm/kvm.ko"
insmod "$PVM_HOST_SOURCE/arch/x86/kvm/kvm-pvm.ko"
```

Restart sandboxd to reprobe the backend before accepting workloads and keep the pair fixed for its lifetime. The OOT core must not be combined with distribution VMX/SVM modules. It is still a two-module replacement, not a single independent `pvm.ko` attached to the stock KVM core. This AKernel profile does not automatically change host kernels, boot settings, modules, networks or existing clusters.

Build the guest bundle with Firecracker's `AKERNEL_KERNEL_PROFILE=pvm` option or candidate workflow `kernel_profile=pvm` choice. The builder verifies PVM guest options and common AKernel filesystem, network and virtio requirements. Its guest config, source provenance, licenses and checksums are packaged together. Build the initrd from the consuming sandboxd revision so host and guest agent protocols match.

The guest must enable `CONFIG_KVM_GUEST=y`, `CONFIG_PVM_GUEST=y`, `CONFIG_X86_PIE=y`, and `CONFIG_X86_INTEL_MEMORY_PROTECTION_KEYS=y`, plus the common AKernel guest fragment. Use Firecracker's resolved `resources/akernel/kernel/pvm-guest.config`, not an unmodified upstream minimal PVM config: the latter can omit AKernel's filesystem/virtio requirements and disable MPK. On a PKU-capable host, enabling guest MPK aligns guest and host `XCR0.PKRU`; otherwise a nested deployment can incur two intercepted `XSETBV` instructions on each guest/host transition. The tested machine reports an xstate mask of `0x2ff` in both kernels. Check the actual xstate masks on the target CPU; `0x2ff` is not a portable CPU requirement. Do not disable host PKU with `nopku` to work around a mismatch: this pinned PVM revision can fault in its `rdpkru` path. Enabling the guest xstate bit does not qualify enforcement of guest `pkey_mprotect` permissions, which remains incomplete in this PVM revision.

## Optional OOT-module DEBUGCTL optimization

The frequently described "one-line optimization" is the removal of this unconditional save from the common host KVM `vcpu_enter_guest()` path in `arch/x86/kvm/x86.c`:

```c
vcpu->arch.host_debugctl = get_debugctlmsr();
```

The value is consumed by VMX/SVM, while PVM already saves its own host DEBUGCTL state in `pvm_vcpu_load()`. An L1 can incur an outer-hypervisor MSR exit for each unnecessary `IA32_DEBUGCTL` (`0x1d9`) read, including first-write faults during exact dirty-page tracking. The [backend-scoped reference patch](pvm/kvm-debugctl-backend-scope.patch) moves the save into `vmx_vcpu_run()` and `svm_vcpu_run()`, preserving those source paths. It applies to the pinned OOT source and changes the module source only. OOT mode builds the core and PVM backend, excluding VMX/SVM; the guest-bundle builder does not apply the patch.

To build this optional variant, use a separate clean checkout at the pinned OOT commit and build only the matched modules against the unchanged distribution host:

```sh
PVM_HOST_SOURCE=/path/to/separate-pinned-pvm-linux
PVM_DEBUGCTL_PATCH="$PWD/deploy/pvm/kvm-debugctl-backend-scope.patch"
git -C "$PVM_HOST_SOURCE" apply --check "$PVM_DEBUGCTL_PATCH"
git -C "$PVM_HOST_SOURCE" apply "$PVM_DEBUGCTL_PATCH"
make -C "/lib/modules/$(uname -r)/build" \
    M="$PVM_HOST_SOURCE/arch/x86/kvm" PVM_OOT_MODE=1 -j2 modules
```

Load both modules during the dedicated-node procedure above and requalify the optimized pair. Preserve hardware-KVM restoration; do not discard its saved value or mix the OOT core with stock vendor modules. To return to hardware KVM, stop PVM users, unload both OOT modules and load the complete original stock pair. The stock kernel and its vendor modules remain unchanged. SVM hardware execution was not covered by the Intel-only experiment.

An earlier controlled nested microbenchmark on a Cascade Lake host measured 96 MiB first dirty writes at about 61.5 ms with guest MPK alone and 35.9 ms with MPK plus this backend-scoped patch, compared with about 50 ms for nested hardware KVM. These earlier in-tree results do not qualify OOT performance, AKernel service performance or direct bare-metal behavior. Current stock-kernel OOT qualification uses the unoptimized pair. Record the patch and exact module identities separately when validating an optimized pair.

## Candidate image and node selection

To validate an unreleased bundle, provide all three artifact fields and its kernel profile. Partial overrides fail. Image construction verifies archive/internal checksums, manifest identity, selected profile and PVM guest options. The artifact profile does not change the host backend.

```sh
: "${PVM_BUNDLE_TAG:?set the planned candidate release tag}"
: "${PVM_BUNDLE_URL:?set the exact candidate archive URL}"
: "${PVM_BUNDLE_SHA256:?set the verified archive SHA-256}"
FIRECRACKER_KERNEL_PROFILE=pvm \
FIRECRACKER_RELEASE="$PVM_BUNDLE_TAG" \
FIRECRACKER_AMD64_URL="$PVM_BUNDLE_URL" \
FIRECRACKER_AMD64_SHA256="$PVM_BUNDLE_SHA256" \
    deploy/scripts/build-image.sh --repository pvm-validation --tag candidate
```

Keep default release pins unchanged during development. After validation and promotion, pin the published URL and verified digest in sandboxd and advance AKernel's gitlink. Do not rebuild the candidate during promotion.

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

The tested PVM backend has no TSC frequency scaling. PVM snapshots need a verified source frequency and the target must report the same frequency; the gate rejects a mismatch or unverifiable frequency. This does not qualify heterogeneous CPU migration. The baseline is same-host restore with identical VMM, kernel, initrd and virtiofsd; qualify cross-host CPU compatibility separately. Writable host exports remain caller-owned and require local checkpoint/restore.

Before expansion, run sandboxd Full, XFS Incremental, local virtio-fs and shared host-mount suites; check both advertisements, both cross-backend rejection directions, legacy/missing-frequency rejection and same-PVM recovery. Then run SDK integration with `AKERNEL_TEST_RUNTIME=firecracker-pvm` and pressure with `--runtime firecracker-pvm`. OCI/Nydus requires real image-provider tests; directory roots do not cover that path.

For hardware-KVM rollback on a host that exposes VMX/SVM, drain workloads, stop PVM users, preserve caller-owned artifacts, unload `kvm_pvm` and the OOT `kvm`, then `modprobe kvm_intel` or `modprobe kvm_amd` to restore the complete stock pair. Restart sandboxd and select the default runtime after its ABI probe succeeds. An L1 without nested hardware virtualization has no hardware-KVM fallback; use an eligible hardware node or the existing runsc systrap profile. PVM snapshots require a compatible PVM stack and cannot restore on rollback KVM. Cluster deployment remains a deliberate operator action.

## Qualification without nested hardware virtualization

Boot an isolated L1 with the original distribution kernel and no VMX/SVM exposure, for example QEMU `-cpu host,-vmx,-svm`. Keep the other PVM CPU prerequisites exposed and use the supported host boot settings. Check the L1 CPUID bits, verify the stock vendor module cannot load, then load the OOT pair and probe the PVM ABI. Perform all module operations inside the L1; the physical test server's kernel, KVM modules and active cluster stay untouched.

Opening `/dev/kvm` alone is insufficient. Run boot/exec/delete, Full and Incremental/SoftDirty checkpoint/restore, actual OCI/Nydus image-provider tests, the complete private control plane/SDK suite, and bounded pressure with strict deletion checks. Record the stock-kernel hash, module hashes, CPU exposure, guest bundle/agent digests and tested revisions. Check kernel dmesg and restore/unload cleanup. CI runtime runners must create their own disposable L1 rather than replacing the runner's live KVM core.

The stock-kernel qualification also confirms the matched pair does not require nested VT-x/AMD-V. It does not cover every distribution ABI, CPU architecture, Secure Boot setup, AMD execution, heterogeneous migration or the complete DeepSWE oracle workload. The historical first 15-second guest-agent timeout remains unlocated. Large checkpoint/reload can briefly exceed the tunnel heartbeat budget before automatic reconnection; passing restore tests do not guarantee uninterrupted connections.
