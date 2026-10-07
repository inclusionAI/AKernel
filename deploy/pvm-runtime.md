# PVM-backed Firecracker nodes

PVM is an opt-in backend of the Firecracker adapter. Use `Sandbox(runtime="firecracker-pvm")` on a dedicated PVM node profile. The default `firecracker` class requires hardware KVM; sandboxd probes the actual `/dev/kvm` ABI before advertising either class. YuanRong's existing capability snapshot and `sandbox.runtime` placement labels carry the distinct classes without a static label override.

This profile requires the sandboxd change that adds `firecracker-pvm`, and a validated PVM guest bundle from `akernel-dev/firecracker`. Merge and pin the tested sandboxd revision before shipping an AKernel image. Default runtime release pins remain unchanged until a candidate passes sandboxd and AKernel validation and its exact bytes are promoted.

## Host and guest prerequisites

The host must boot a PVM Linux kernel and load matching `kvm` and `kvm-pvm` modules. A distribution kernel cannot acquire PVM support by loading a module built for another kernel. The tested source is `virt-pvm/linux` commit `58902213f660d7f8d75eb9f08e6c3ff7e4a3721d`, Linux 6.12.33. [`pvm/host.config`](./pvm/host.config) preserves the resolved isolated-host configuration, including virtio boot devices and AKernel network/storage prerequisites. Qualify each target machine's drivers, CPU features, kernel support policy and boot process before using this reproduction input on a dedicated node.

```sh
mkdir /path/to/new-host-build
cp deploy/pvm/host.config /path/to/new-host-build/.config
make -C /path/to/pinned-pvm-linux O=/path/to/new-host-build olddefconfig
make -C /path/to/pinned-pvm-linux O=/path/to/new-host-build -j8 bzImage modules
```

Use the checksum-verified source archive pinned by the matching Firecracker PVM profile. The tested host boots with `nokaslr pti=off`. PVM requires supported FSGSBASE, RDTSCP, CMPXCHG16B and, when interception is enabled, CPUID faulting; this revision does not support host KPTI or FRED. PVM and hardware vendor modules are mutually exclusive. Prepare a dedicated node and recovery boot entry, drain workloads and stop all VMMs before installing kernels or changing vendor modules. This AKernel profile does not change host kernels, boot settings, modules, networks or existing clusters.

Build the guest bundle with Firecracker's `AKERNEL_KERNEL_PROFILE=pvm` option or candidate workflow `kernel_profile=pvm` choice. The builder verifies PVM guest options and common AKernel filesystem, network and virtio requirements. Its guest config, source provenance, licenses and checksums are packaged together. Build the initrd from the consuming sandboxd revision so host and guest agent protocols match.

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
