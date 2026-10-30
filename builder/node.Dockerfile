# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

ARG AKERNEL_NODE_BASE_IMAGE=ubuntu:24.04
ARG AKERNEL_RUNTIME_IMAGE=akernel-runtime:local
ARG AKERNEL_ENABLE_KATA=true
ARG AKERNEL_ENABLE_RUNC=false
ARG AKERNEL_ENABLE_FIRECRACKER=true
ARG SANDBOXD_BUILD_IMAGE=golang:1.25.5-bookworm
ARG GVISOR_DOWNLOAD_IMAGE=ubuntu:24.04
ARG GVISOR_RELEASE
ARG GVISOR_URL
ARG GVISOR_SHA512
ARG RUNC_VERSION
ARG RUNC_SHA256
ARG RUNC_RELEASE_BASE_URL
ARG RUNC_BUILD_IMAGE=ubuntu:24.04
ARG LIBNVIDIA_CONTAINER_VERSION=1.19.1-1
ARG KATA_BUILD_IMAGE=ubuntu:24.04
ARG KATA_RELEASE=4.0.0
ARG KATA_AMD64_SHA256=2c3b9dfeba355582b40aee462b12916c9740654d0230f696adf719d67b063a8c
ARG KATA_RELEASE_BASE_URL=https://github.com/kata-containers/kata-containers/releases/download
ARG FIRECRACKER_BUILD_IMAGE=ubuntu:24.04
ARG FIRECRACKER_RELEASE
ARG FIRECRACKER_AMD64_SHA256
ARG FIRECRACKER_AMD64_URL
ARG FIRECRACKER_KERNEL_PROFILE=kvm
ARG VIRTIOFSD_BUILD_IMAGE=rust:1.90.0-bookworm
# virtiofsd v1.14.0, including the release Cargo.lock.
ARG VIRTIOFSD_REVISION=c2540f8db14caba81c1e37fba23fc7bf2cd7f0dd
# Upstream publishes the contrib 0.120.1 binary in the v0.120.0 archive.
ARG OTELCOL_CONTRIB_VERSION=0.120.0
ARG OTELCOL_CONTRIB_AMD64_SHA256=81bf885bc9a86705feb3c113c5a356571390e3601eb651ffcf2b3428f6571adb
ARG OTELCOL_CONTRIB_ARM64_SHA256=00b11a5b468455e6ab49d6857e36f5825cb887bf5d8646282f8db33e3eeb5c76
ARG OTELCOL_CONTRIB_URL=
ARG OTELCOL_CONTRIB_SHA256=
ARG AKERNEL_VERSION=unknown
ARG AKERNEL_REVISION=unknown
ARG ADX_VERSION=v0.1.0rc2
ARG ADX_RELEASE_URL=https://agent-dx.obs.cn-southwest-2.myhuaweicloud.com/adx/daily/20261028142955-18acb0fe1215/linux/amd64/adx-release.tar.gz
ARG ADX_RELEASE_SHA256=d5db7193e80ab0088d8a1ff304fc38bba02ba76761b29f5fbd89881059e1de2d

FROM ${AKERNEL_NODE_BASE_IMAGE} AS adx-release
ARG ADX_RELEASE_URL
ARG ADX_RELEASE_SHA256
ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh
RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    set -eux; \
    sh /tmp/apt-get-update.sh; \
    apt-get install -y --no-install-recommends ca-certificates curl python3; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /tmp/adx-release; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "${ADX_RELEASE_URL}" -o /tmp/adx-release.tar.gz; \
    echo "${ADX_RELEASE_SHA256}  /tmp/adx-release.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/adx-release.tar.gz -C /tmp/adx-release; \
    /tmp/adx-release/install.sh --prefix /opt/adx --bin-dir /usr/local/bin; \
    rm -rf /tmp/adx-release /tmp/adx-release.tar.gz

FROM ${GVISOR_DOWNLOAD_IMAGE} AS gvisor-runtime
ARG GVISOR_RELEASE
ARG GVISOR_URL
ARG GVISOR_SHA512
ARG TARGETARCH
COPY src/sandboxd/third_party/install-gvisor.sh /usr/local/libexec/install-gvisor.sh
COPY ./builder/scripts/verify-elf-arch.sh /usr/local/libexec/verify-elf-arch.sh
ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    set -eux; \
    case "${TARGETARCH:-}" in \
      amd64|arm64) ;; \
      *) echo "unsupported gVisor target architecture: ${TARGETARCH}" >&2; \
         exit 1 ;; \
    esac; \
    test -n "${GVISOR_RELEASE}"; \
    test -n "${GVISOR_URL}"; \
    test -n "${GVISOR_SHA512}"; \
    asset=/tmp/gvisor.tar.bz2; \
    sh /tmp/apt-get-update.sh; \
    apt-get install -y --no-install-recommends bzip2 ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fSL --http1.1 --retry 10 --retry-delay 2 --retry-all-errors \
      "${GVISOR_URL}" -o "${asset}"; \
    bash /usr/local/libexec/install-gvisor.sh "${asset}" \
      "${GVISOR_SHA512}" /gvisor; \
    for binary in runsc containerd-shim-runsc-v1 \
      gvisor-bin/checkpointgofer gvisor-bin/gvisor-sentry-prewarmer \
      gvisor-bin/gvisor_sentry gvisor-bin/runsc-fd-parking \
      gvisor-bin/runsc-metric-server; do \
      sh /usr/local/libexec/verify-elf-arch.sh "/gvisor/${binary}" "${TARGETARCH}"; \
    done; \
    /gvisor/runsc --version

FROM ${KATA_BUILD_IMAGE} AS kata-runtime-true
ARG KATA_RELEASE
ARG KATA_AMD64_SHA256
ARG KATA_RELEASE_BASE_URL
ARG TARGETARCH
ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    set -eux; \
    test "${TARGETARCH:-amd64}" = "amd64"; \
    sh /tmp/apt-get-update.sh; \
    apt-get install -y --no-install-recommends ca-certificates curl zstd; \
    rm -rf /var/lib/apt/lists/*; \
    archive="/tmp/kata-static-${KATA_RELEASE}-amd64.tar.zst"; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "${KATA_RELEASE_BASE_URL}/${KATA_RELEASE}/kata-static-${KATA_RELEASE}-amd64.tar.zst" \
      -o "${archive}"; \
    echo "${KATA_AMD64_SHA256}  ${archive}" | sha256sum -c -; \
    mkdir -p /kata; \
    tar --zstd -xf "${archive}" -C /kata \
      ./opt/kata/runtime-rs/bin/containerd-shim-kata-v2 \
      ./opt/kata/share/defaults/kata-containers/runtime-rs/configuration-dragonball.toml \
      ./opt/kata/share/kata-containers/vmlinux-dragonball-experimental.container \
      ./opt/kata/share/kata-containers/vmlinux-6.18.35-200-dragonball-experimental \
      ./opt/kata/share/kata-containers/kata-containers.img \
      ./opt/kata/share/kata-containers/kata-ubuntu-noble.image; \
    ln -sfn configuration-dragonball.toml \
      /kata/opt/kata/share/defaults/kata-containers/runtime-rs/configuration.toml; \
    mkdir -p /kata/opt/kata/share/licenses/kata-containers; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "https://raw.githubusercontent.com/kata-containers/kata-containers/${KATA_RELEASE}/LICENSE" \
      -o /kata/opt/kata/share/licenses/kata-containers/LICENSE; \
    rm -f "${archive}"

FROM ${KATA_BUILD_IMAGE} AS kata-runtime-false
RUN mkdir -p /kata/opt/kata

FROM kata-runtime-${AKERNEL_ENABLE_KATA} AS kata-runtime

FROM ${AKERNEL_RUNTIME_IMAGE} AS runtime-image

FROM ${SANDBOXD_BUILD_IMAGE} AS sandboxd-builder
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive
ARG AKERNEL_APT_DEBIAN_MIRROR
ARG AKERNEL_APT_DEBIAN_SECURITY_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    sh /tmp/apt-get-update.sh && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        gcc \
        git \
        libc6-dev \
        make && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src/sandboxd
COPY ./src/sandboxd/ ./
COPY ./builder/scripts/verify-elf-arch.sh /usr/local/libexec/verify-elf-arch.sh
ARG GOPROXY=https://proxy.golang.org,direct
RUN set -eux; \
    case "${TARGETARCH}" in amd64|arm64) ;; *) exit 1 ;; esac; \
    make release RELEASE_GOARCH="${TARGETARCH}"; \
    for binary in sandboxd sbox runc-shim sandbox-logger firecracker-agent; do \
      sh /usr/local/libexec/verify-elf-arch.sh "output/${binary}" "${TARGETARCH}"; \
    done

FROM ${VIRTIOFSD_BUILD_IMAGE} AS virtiofsd-builder
ARG VIRTIOFSD_REVISION
ARG AKERNEL_APT_DEBIAN_MIRROR
ARG AKERNEL_APT_DEBIAN_SECURITY_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    sh /tmp/apt-get-update.sh && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      -o Dpkg::Options::="--force-confold" --no-install-recommends \
      ca-certificates git libcap-ng-dev libseccomp-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src/virtiofsd
RUN git init && \
    git fetch --depth=1 https://gitlab.com/virtio-fs/virtiofsd.git "${VIRTIOFSD_REVISION}" && \
    git checkout --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = "${VIRTIOFSD_REVISION}" && \
    cargo build --release --locked

FROM ${FIRECRACKER_BUILD_IMAGE} AS firecracker-runtime-true
COPY src/sandboxd/third_party/firecracker-candidate/ /tmp/firecracker-candidate/
ARG FIRECRACKER_RELEASE
ARG FIRECRACKER_AMD64_SHA256
ARG FIRECRACKER_AMD64_URL
ARG FIRECRACKER_KERNEL_PROFILE
ARG TARGETARCH
ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    sh /tmp/apt-get-update.sh && \
    apt-get install -y --no-install-recommends ca-certificates cpio curl gzip jq && \
    rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    test "${TARGETARCH:-amd64}" = "amd64"; \
    test -n "${FIRECRACKER_RELEASE}"; \
    test -n "${FIRECRACKER_AMD64_SHA256}"; \
    test -n "${FIRECRACKER_AMD64_URL}"; \
    archive="/tmp/firecracker-${FIRECRACKER_RELEASE}-x86_64.tgz"; \
    if [ "${FIRECRACKER_KERNEL_PROFILE}" = pvm ]; then \
      cp /tmp/firecracker-candidate/firecracker.tgz "${archive}"; \
    else \
      curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
        "${FIRECRACKER_AMD64_URL}" -o "${archive}"; \
    fi; \
    echo "${FIRECRACKER_AMD64_SHA256}  ${archive}" | sha256sum -c -; \
    mkdir -p /tmp/firecracker-release \
      /firecracker/usr/local/bin \
      /firecracker/opt/firecracker/licenses \
      /initrd; \
    tar -xzf "${archive}" -C /tmp/firecracker-release; \
    bundle="/tmp/firecracker-release/release-${FIRECRACKER_RELEASE}-x86_64"; \
    jq -e --arg release "${FIRECRACKER_RELEASE}" \
      --arg profile "${FIRECRACKER_KERNEL_PROFILE}" \
      '.component == "akernel-firecracker-runtime" and \
       .repository == "akernel-dev/firecracker" and \
       .release_tag == $release and \
       .architecture == "x86_64" and \
       (.kernel_profile // "kvm") == $profile and \
       ($profile == "kvm" or $profile == "pvm")' \
      "${bundle}/manifest.json" >/dev/null; \
    (cd "${bundle}"; sha256sum -c SHA256SUMS); \
    if [ "${FIRECRACKER_KERNEL_PROFILE}" = pvm ]; then \
      for config in PVM_GUEST X86_PIE X86_INTEL_MEMORY_PROTECTION_KEYS; do \
        grep -qx "CONFIG_${config}=y" "${bundle}/kernel.config"; \
      done; \
    fi; \
    install -m 0755 "${bundle}/firecracker" \
      /firecracker/usr/local/bin/firecracker; \
    install -m 0644 \
      "${bundle}/vmlinux" \
      "${bundle}/kernel.config" \
      "${bundle}/manifest.json" \
      /firecracker/opt/firecracker/; \
    cp -a "${bundle}/licenses/." /firecracker/opt/firecracker/licenses/; \
    rm -rf /tmp/firecracker-candidate

COPY --from=virtiofsd-builder /src/virtiofsd/target/release/virtiofsd /firecracker/usr/local/bin/virtiofsd
COPY --from=virtiofsd-builder /src/virtiofsd/LICENSE-APACHE /firecracker/opt/firecracker/licenses/virtiofsd-LICENSE-APACHE
COPY --from=virtiofsd-builder /src/virtiofsd/LICENSE-BSD-3-Clause /firecracker/opt/firecracker/licenses/virtiofsd-LICENSE-BSD-3-Clause
COPY --from=sandboxd-builder /src/sandboxd/output/firecracker-agent /initrd/init
RUN set -eux; \
    chmod 0755 /initrd/init; \
    chmod 0700 /initrd; \
    touch -d @0 /initrd /initrd/init; \
    cd /initrd; \
    find . -print0 \
      | LC_ALL=C sort -z \
      | cpio --null --create --format=newc --owner=0:0 --reproducible \
      | gzip -n -9 > /firecracker/opt/firecracker/initrd.img; \
    chmod 0644 /firecracker/opt/firecracker/initrd.img

FROM ${FIRECRACKER_BUILD_IMAGE} AS firecracker-runtime-false
RUN mkdir -p /firecracker/usr/local/bin /firecracker/opt/firecracker

FROM firecracker-runtime-${AKERNEL_ENABLE_FIRECRACKER} AS firecracker-runtime

FROM ${RUNC_BUILD_IMAGE} AS runc-runtime-true
ARG RUNC_VERSION
ARG RUNC_SHA256
ARG RUNC_RELEASE_BASE_URL
ARG TARGETARCH
COPY ./builder/scripts/verify-elf-arch.sh /usr/local/libexec/verify-elf-arch.sh
ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    set -eux; \
    case "${TARGETARCH}" in \
      amd64|arm64) ;; \
      *) echo "unsupported runc target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    test -n "${RUNC_VERSION}"; \
    test -n "${RUNC_SHA256}"; \
    test -n "${RUNC_RELEASE_BASE_URL}"; \
    sh /tmp/apt-get-update.sh; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    asset="/tmp/runc.${TARGETARCH}"; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "${RUNC_RELEASE_BASE_URL}/v${RUNC_VERSION}/runc.${TARGETARCH}" \
      -o "${asset}"; \
    echo "${RUNC_SHA256}  ${asset}" | sha256sum -c -; \
    sh /usr/local/libexec/verify-elf-arch.sh "${asset}" "${TARGETARCH}"; \
    install -D -m 0755 "${asset}" /runc/usr/local/bin/runc; \
    rm -f "${asset}"
COPY --from=sandboxd-builder /src/sandboxd/output/runc-shim /runc/usr/local/bin/runc-shim

FROM ${RUNC_BUILD_IMAGE} AS runc-runtime-false
RUN mkdir -p /runc/usr/local/bin

FROM runc-runtime-${AKERNEL_ENABLE_RUNC} AS runc-runtime

FROM ubuntu:24.04 AS distill-fs-runtime
ARG TARGETARCH
ARG DISTILL_FS_RELEASE
ARG DISTILL_FS_URL
ARG DISTILL_FS_SHA256
ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    sh /tmp/apt-get-update.sh && \
    apt-get install -y --no-install-recommends ca-certificates curl jq binutils && \
    rm -rf /var/lib/apt/lists/*
COPY ./builder/scripts/install-distill-fs.sh /install-distill-fs.sh
COPY ./builder/scripts/verify-elf-arch.sh /verify-elf-arch.sh
RUN sh /install-distill-fs.sh "$DISTILL_FS_RELEASE" \
    "$DISTILL_FS_URL" "$DISTILL_FS_SHA256" /distill-fs

FROM ${AKERNEL_NODE_BASE_IMAGE}
# Let PID 1 systemd avoid remounting shared host filesystems during shutdown.
ENV container=oci

ARG AKERNEL_ENABLE_KATA
ARG AKERNEL_ENABLE_RUNC
ARG AKERNEL_ENABLE_FIRECRACKER
ARG GVISOR_RELEASE
ARG GVISOR_SHA512
ARG DISTILL_FS_SHA256
ARG RUNC_VERSION
ARG RUNC_SHA256
ARG FIRECRACKER_RELEASE
ARG LIBNVIDIA_CONTAINER_VERSION
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive

ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    sh /tmp/apt-get-update.sh && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        e2fsprogs \
        fuse3 \
        gnupg \
        iproute2 \
        ipset \
        iptables \
        jq \
        kmod \
        libcap-ng0 \
        libgcc-s1 \
        libseccomp2 \
        logrotate \
        mount \
        openssl \
        procps \
        python3 \
        python3-pip \
        python3-yaml \
        systemd \
        systemd-sysv \
        tzdata \
        xfsprogs && \
    rm -rf /var/lib/apt/lists/*

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    set -eux; \
    if [ "${TARGETARCH}" = "arm64" ]; then \
      echo "NVIDIA payload is excluded from the linux/arm64 image"; \
    else \
    curl -fsSL --retry 10 --retry-delay 2 --retry-all-errors \
      https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg; \
    curl -fsSL --retry 10 --retry-delay 2 --retry-all-errors \
      https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed \
        's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list; \
    sh /tmp/apt-get-update.sh; \
    apt-get install -y --no-install-recommends \
      "libnvidia-container1=${LIBNVIDIA_CONTAINER_VERSION}" \
      "libnvidia-container-tools=${LIBNVIDIA_CONTAINER_VERSION}"; \
    rm -rf /var/lib/apt/lists/*; \
    fi

RUN if command -v update-alternatives >/dev/null 2>&1; then \
        update-alternatives --set iptables /usr/sbin/iptables-legacy || true; \
        update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy || true; \
    fi

RUN if command -v systemctl >/dev/null 2>&1; then \
        systemctl mask \
            dev-hugepages.mount \
            dev-mqueue.mount \
            getty@.service \
            systemd-logind.service \
            systemd-remount-fs.service \
            systemd-tmpfiles-setup-dev.service || true; \
    fi

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone


COPY ./builder/scripts/verify-elf-arch.sh /usr/local/libexec/verify-elf-arch.sh
COPY --from=runtime-image /akernel-runtime-rootfs.img /opt/akernel/runtime/akernel-runtime-rootfs.img
RUN --mount=from=runtime-image,target=/mnt/akernel-runtime \
    mkdir -p /opt/akernel/runtime/metadata && \
    if test -d /mnt/akernel-runtime/runtime-metadata; then \
      cp -a /mnt/akernel-runtime/runtime-metadata/. /opt/akernel/runtime/metadata/; \
    fi
COPY ./builder/scripts/verify-e2b-release.py /usr/local/libexec/verify-e2b-release.py

# ADX provides the control and data plane processes. AKernel keeps building
# sandboxd from pinned sources. The default rootfs installs only EXECD; the
# opt-in E2B rootfs reuses the envd-enabled ADX Execd component.
COPY --from=adx-release /opt/adx/current/bin/ /opt/adx/current/bin/
COPY --from=adx-release /opt/adx/current/third_party/ /opt/adx/current/third_party/
COPY --from=adx-release /opt/adx/current/LICENSE /opt/adx/current/LICENSE
COPY --from=adx-release /opt/adx/current/manifest.json /opt/adx/current/manifest.json
COPY --from=adx-release /opt/adx/current/etc/ /opt/adx/current/etc/
COPY --from=adx-release /opt/adx/current/sdk/ /opt/adx/current/sdk/
COPY --from=adx-release /opt/adx/current/runtime/adx-execd /opt/adx/current/runtime/adx-execd
RUN set -eux; \
    test -x /opt/adx/current/bin/adxctl; \
    test -x /opt/adx/current/bin/adx-coordinator; \
    test -x /opt/adx/current/bin/adxlet; \
    test -x /opt/adx/current/bin/adx-apiserver; \
    test -x /opt/adx/current/runtime/adx-execd; \
    test ! -e /opt/adx/current/runtime/adx-runtime-rootfs.img; \
    if test -f /opt/akernel/runtime/metadata/adx-component-manifest.json; then \
      python3 /usr/local/libexec/verify-e2b-release.py \
        /opt/akernel/runtime/metadata/adx-component-manifest.json \
        /opt/adx/current/manifest.json; \
    fi; \
    for binary in adxctl adx-coordinator adxlet adx-apiserver; do \
      sh /usr/local/libexec/verify-elf-arch.sh "/opt/adx/current/bin/${binary}" "${TARGETARCH}"; \
    done; \
    ln -sfn /opt/adx/current/bin/adxctl /usr/local/bin/adxctl

COPY --from=gvisor-runtime /gvisor/ /usr/local/bin/
COPY --from=sandboxd-builder /src/sandboxd/output/sandboxd /usr/local/bin/sandboxd
COPY --from=sandboxd-builder /src/sandboxd/output/sbox /usr/local/bin/sbox
COPY --from=sandboxd-builder /src/sandboxd/output/sandbox-logger /usr/local/bin/sandbox-logger
COPY --from=distill-fs-runtime /distill-fs/bin/distill_fs /usr/local/bin/distill_fs
COPY --from=distill-fs-runtime /distill-fs/share/distill-fs/ /usr/local/share/distill-fs/
COPY --from=kata-runtime /kata/opt/kata /opt/kata
COPY --from=runc-runtime /runc/usr/local/bin/ /usr/local/bin/
COPY --from=firecracker-runtime /firecracker/ /
RUN if [ "${AKERNEL_ENABLE_KATA}" = "true" ]; then \
      ln -sf /opt/kata/runtime-rs/bin/containerd-shim-kata-v2 /usr/local/bin/containerd-shim-kata-v2; \
    fi

COPY ./builder/scripts/akernel-entrypoint.sh /usr/local/bin/akernel-entrypoint
COPY ./builder/scripts/prepare-adx-config.py /usr/local/libexec/prepare-adx-config.py
COPY ./builder/scripts/adx-service.sh /usr/local/bin/adx-service
COPY ./builder/scripts/otel-collector-service.sh /usr/local/bin/otel-collector-service
COPY ./builder/scripts/sandboxd_network_prepare.sh /usr/local/bin/sandboxd-network-prepare
ARG FIRECRACKER_KERNEL_PROFILE
COPY ./builder/config/adx-standalone.yaml /etc/akernel/adx-standalone.yaml
RUN case "${FIRECRACKER_KERNEL_PROFILE}" in \
      pvm) python3 /usr/local/libexec/prepare-adx-config.py \
        /etc/akernel/adx-standalone.yaml /etc/akernel/adx-standalone.yaml --pvm ;; \
      kvm) ;; \
      *) echo "invalid Firecracker kernel profile" >&2; exit 1 ;; \
    esac
RUN chmod 0755 \
        /usr/local/bin/runsc \
        /usr/local/bin/sandboxd \
        /usr/local/bin/sbox \
        /usr/local/bin/sandbox-logger \
        /usr/local/bin/distill_fs \
        /usr/local/bin/akernel-entrypoint \
        /usr/local/bin/adx-service \
        /usr/local/bin/sandboxd-network-prepare
RUN if [ "${AKERNEL_ENABLE_KATA}" = "true" ]; then chmod 0755 /usr/local/bin/containerd-shim-kata-v2; fi
RUN if [ "${AKERNEL_ENABLE_RUNC}" = "true" ]; then \
      chmod 0755 /usr/local/bin/runc /usr/local/bin/runc-shim; \
    else \
      test ! -e /usr/local/bin/runc; \
      test ! -e /usr/local/bin/runc-shim; \
    fi

COPY ./builder/config/otel-collector-standalone.yaml /etc/akernel/otel_standalone.yaml
COPY ./deploy/akernel/charts/core/files/otel-collector/config.yaml /etc/akernel/otel_config.yaml
RUN chmod 0755 /usr/local/bin/otel-collector-service
COPY ./builder/config/logrotate.d/gvisor /etc/logrotate.d/gvisor
COPY ./builder/scripts/*.sh /root/
COPY ./builder/systemd_services/*.service /etc/systemd/system/

ARG OTELCOL_CONTRIB_VERSION
ARG OTELCOL_CONTRIB_AMD64_SHA256
ARG OTELCOL_CONTRIB_ARM64_SHA256
ARG OTELCOL_CONTRIB_URL
ARG OTELCOL_CONTRIB_SHA256

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) archive_sha="${OTELCOL_CONTRIB_AMD64_SHA256}" ;; \
      arm64) archive_sha="${OTELCOL_CONTRIB_ARM64_SHA256}" ;; \
      *) echo "unsupported OTel target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive_url="https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_CONTRIB_VERSION}/otelcol-contrib_${OTELCOL_CONTRIB_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    if [ -n "${OTELCOL_CONTRIB_URL}" ]; then \
      test -n "${OTELCOL_CONTRIB_SHA256}" || { \
        echo "OTELCOL_CONTRIB_URL and OTELCOL_CONTRIB_SHA256 must be set together" >&2; exit 1; \
      }; \
      archive_url="${OTELCOL_CONTRIB_URL}"; \
      archive_sha="${OTELCOL_CONTRIB_SHA256}"; \
    else \
      test -z "${OTELCOL_CONTRIB_SHA256}" || { \
        echo "OTELCOL_CONTRIB_URL and OTELCOL_CONTRIB_SHA256 must be set together" >&2; exit 1; \
      }; \
    fi; \
    printf '%s\n' "${archive_sha}" | grep -Eq '^[0-9a-fA-F]{64}$' || { \
      echo "invalid OTel SHA-256 pin" >&2; exit 1; \
    }; \
    temp_dir="$(mktemp -d /tmp/akernel-otelcol.XXXXXX)"; \
    trap 'rm -rf "${temp_dir}"' EXIT; \
    archive="${temp_dir}/otelcol-contrib.tar.gz"; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
        "${archive_url}" -o "${archive}"; \
    printf '%s  %s\n' "${archive_sha}" "${archive}" | sha256sum -c -; \
    tar -xzf "${archive}" -C "${temp_dir}" otelcol-contrib; \
    sh /usr/local/libexec/verify-elf-arch.sh "${temp_dir}/otelcol-contrib" "${TARGETARCH}"; \
    install -m 0755 "${temp_dir}/otelcol-contrib" /usr/local/bin/otelcol-contrib

RUN chmod 0644 /etc/logrotate.d/gvisor && \
    systemctl mask getty-static.service && \
    systemctl enable logrotate.timer otel_collector.service sandboxd.service adx.service

# Metadata-only changes must not invalidate fixed runtime download layers.
ARG AKERNEL_VERSION
ARG AKERNEL_REVISION
LABEL org.opencontainers.image.version="${AKERNEL_VERSION}" \
      org.opencontainers.image.revision="${AKERNEL_REVISION}" \
      org.akernel.runtime.profile="rrt" \
      org.akernel.gvisor.release="${GVISOR_RELEASE}" \
      org.akernel.gvisor.archive.sha512="${GVISOR_SHA512}" \
      org.akernel.distill-fs.archive.sha256="${DISTILL_FS_SHA256}" \
      org.akernel.runc.version="${RUNC_VERSION}" \
      org.akernel.runc.binary.sha256="${RUNC_SHA256}" \
      org.akernel.runc.enabled="${AKERNEL_ENABLE_RUNC}" \
      org.akernel.kata.enabled="${AKERNEL_ENABLE_KATA}" \
      org.akernel.firecracker.release="${FIRECRACKER_RELEASE}" \
      org.akernel.firecracker.enabled="${AKERNEL_ENABLE_FIRECRACKER}"

STOPSIGNAL SIGRTMIN+3
