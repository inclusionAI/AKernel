# PVM-backed Firecracker nodes

PVM is an opt-in backend of the Firecracker adapter. Use `Sandbox(runtime="firecracker-pvm")` on a dedicated PVM node profile. The default `firecracker` class requires hardware KVM; sandboxd probes the actual `/dev/kvm` ABI before advertising either class. YuanRong's existing capability snapshot and `sandbox.runtime` placement labels carry the distinct classes without a static label override.

This profile requires the sandboxd change that adds `firecracker-pvm`, and a validated PVM guest bundle from `akernel-dev/firecracker`. Merge and pin the tested sandboxd revision before shipping an AKernel image. Default runtime release pins remain unchanged until a candidate passes sandboxd and AKernel validation and its exact bytes are promoted.

## Host and guest prerequisites

The host must boot a PVM Linux kernel and load matching `kvm` and `kvm-pvm` modules. Install the complete matching module tree: standalone requires IPv4/IPv6 legacy filter tables, conntrack/connmark, bridged filtering and ipset in addition to the KVM modules. A host config trimmed with `localmodconfig` can omit these modules even when direct runtime tests work with nftables. A distribution kernel cannot acquire PVM support by loading a module built for another kernel. The tested source is `virt-pvm/linux` commit `58902213f660d7f8d75eb9f08e6c3ff7e4a3721d`, Linux 6.12.33. [`pvm/host.config`](./pvm/host.config) preserves the resolved isolated-host configuration, including virtio boot devices and AKernel network/storage prerequisites. Qualify each target machine's drivers, CPU features, kernel support policy and boot process before using this reproduction input on a dedicated node.

```sh
mkdir /path/to/new-host-build
cp deploy/pvm/host.config /path/to/new-host-build/.config
make -C /path/to/pinned-pvm-linux O=/path/to/new-host-build olddefconfig
make -C /path/to/pinned-pvm-linux O=/path/to/new-host-build -j8 bzImage modules
```

The host and guest need different configurations. The host input includes `CONFIG_KVM=m`, `CONFIG_KVM_PVM=m`, and `CONFIG_X86_INTEL_MEMORY_PROTECTION_KEYS=y`; retain the target machine's boot/storage drivers and the complete AKernel networking requirements. In particular, keep `CONFIG_IP_NF_FILTER`, `CONFIG_IP6_NF_IPTABLES`, `CONFIG_IP6_NF_FILTER`, `CONFIG_NETFILTER_XT_TARGET_CONNMARK`, and `CONFIG_NETFILTER_XT_MATCH_CONNMARK` enabled, together with conntrack, bridge filtering, ipset and TUN. The checked-in host config includes these options. Install the complete matching module tree, rather than copying only `kvm.ko` and `kvm-pvm.ko`. Omitting the legacy filter modules reproduced a standalone startup failure at `modprobe iptable_filter` even though direct PVM tests passed.

Use the checksum-verified source archive pinned by the matching Firecracker PVM profile. The tested host boots with `nokaslr pti=off`. PVM requires supported FSGSBASE, RDTSCP, CMPXCHG16B and, when interception is enabled, CPUID faulting; this revision does not support host KPTI or FRED. PVM and hardware vendor modules are mutually exclusive. Prepare a dedicated node and recovery boot entry, drain workloads, stop sandboxd and all VMMs before installing kernels or changing vendor modules, and restart sandboxd to reprobe the backend before accepting workloads. Backend modules must remain fixed for a daemon's lifetime. This AKernel profile does not change host kernels, boot settings, modules, networks or existing clusters.

Build the guest bundle with Firecracker's `AKERNEL_KERNEL_PROFILE=pvm` option or candidate workflow `kernel_profile=pvm` choice. The builder verifies PVM guest options and common AKernel filesystem, network and virtio requirements. Its guest config, source provenance, licenses and checksums are packaged together. Build the initrd from the consuming sandboxd revision so host and guest agent protocols match.

The guest must enable `CONFIG_KVM_GUEST=y`, `CONFIG_PVM_GUEST=y`, `CONFIG_X86_PIE=y`, and `CONFIG_X86_INTEL_MEMORY_PROTECTION_KEYS=y`, plus the common AKernel guest fragment. Use Firecracker's resolved `resources/akernel/kernel/pvm-guest.config`, not an unmodified upstream minimal PVM config: the latter can omit AKernel's filesystem/virtio requirements and disable MPK. On a PKU-capable host, enabling guest MPK aligns guest and host `XCR0.PKRU`; otherwise a nested deployment can incur two intercepted `XSETBV` instructions on each guest/host transition. The tested machine reports an xstate mask of `0x2ff` in both kernels. Check the actual xstate masks on the target CPU; `0x2ff` is not a portable CPU requirement. Do not disable host PKU with `nopku` to work around a mismatch: this pinned PVM revision can fault in its `rdpkru` path. Enabling the guest xstate bit does not qualify enforcement of guest `pkey_mprotect` permissions, which remains incomplete in this PVM revision.

## Optional nested-host DEBUGCTL optimization

The frequently described "one-line optimization" is the removal of this unconditional save from the common host KVM `vcpu_enter_guest()` path in `arch/x86/kvm/x86.c`:

```c
vcpu->arch.host_debugctl = get_debugctlmsr();
```

The value is consumed by VMX/SVM, while PVM already saves its own host DEBUGCTL state in `pvm_vcpu_load()`. In a nested deployment, the unnecessary common-path read of `IA32_DEBUGCTL` (`0x1d9`) can cause an outer-hypervisor MSR exit for each PVM guest entry, including first-write faults during exact dirty-page tracking. The [backend-scoped reference patch](pvm/kvm-debugctl-backend-scope.patch) moves the save into both `vmx_vcpu_run()` and `svm_vcpu_run()` instead of discarding the state needed by hardware KVM. It applies to the pinned PVM source above and is a separate, optional host-kernel input; the default host config and Firecracker guest-bundle builder do not apply it.

To build this variant, start with a separate, clean checkout at the pinned commit, apply the patch, and build the host kernel and modules together:

```sh
PVM_HOST_SOURCE=/path/to/separate-pinned-pvm-linux
PVM_HOST_OUTPUT=/path/to/new-optimized-host-build
PVM_DEBUGCTL_PATCH="$PWD/deploy/pvm/kvm-debugctl-backend-scope.patch"
git -C "$PVM_HOST_SOURCE" apply --check "$PVM_DEBUGCTL_PATCH"
git -C "$PVM_HOST_SOURCE" apply "$PVM_DEBUGCTL_PATCH"
mkdir "$PVM_HOST_OUTPUT"
cp deploy/pvm/host.config "$PVM_HOST_OUTPUT/.config"
make -C "$PVM_HOST_SOURCE" O="$PVM_HOST_OUTPUT" olddefconfig
make -C "$PVM_HOST_SOURCE" O="$PVM_HOST_OUTPUT" -j8 bzImage modules
```

Install the complete module tree from that build during the dedicated-node maintenance procedure above. If the host boots modules from an initramfs, rebuild that initramfs as well. Do not merely delete the save, replace it with zero, or mix a modified `kvm.ko` with stock VMX/SVM modules: hardware-KVM DEBUGCTL restoration must remain intact, including when the host uses LBR/BTS. Requalify PVM checkpoint/restore and any hardware-KVM backend the node will use before rollout; SVM hardware execution was not covered by the earlier Intel-only experiment.

An earlier controlled nested microbenchmark on a Cascade Lake host measured 96 MiB first dirty writes at about 61.5 ms with guest MPK alone and 35.9 ms with MPK plus this backend-scoped patch, compared with about 50 ms for nested hardware KVM. Those are workload-specific experimental results, not an AKernel service benchmark or a guarantee for direct bare-metal hosts, where the outer MSR interception is absent. The October AKernel/SDK qualification used the unmodified pinned host source with the corrected network config; it must not be presented as qualification of this optional patched build. Record the applied patch and exact kernel/module identities separately when validating an optimized host.

## Candidate image and node selection

To validate an unreleased bundle, provide all three artifact fields and its kernel profile. Partial overrides fail. Image construction verifies archive/internal checksums, manifest identity, selected profile and PVM guest options. The artifact profile does not change the host backend.

```sh
: "${PVM_BUNDLE_TAG:?set the candidate's planned release tag}"
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

Rollback retains a hardware-KVM boot entry and the previous bundle/config. Drain workloads, preserve caller-owned artifacts, boot the prepared hardware-KVM profile and select the default runtime. PVM snapshots need a compatible PVM stack and cannot restore on rollback KVM. Host replacement and cluster deployment remain deliberate operator actions.
