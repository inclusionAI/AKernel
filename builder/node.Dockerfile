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
ARG GVISOR_AMD64_URL
ARG GVISOR_AMD64_SHA512
ARG RUNC_VERSION=1.5.1
ARG RUNC_AMD64_SHA256=177df879d50c913eb205e898d5c1c05a18f574053c0ce5524c471208eaf06f6f
ARG RUNC_RELEASE_BASE_URL=https://github.com/opencontainers/runc/releases/download
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
ARG VIRTIOFSD_BUILD_IMAGE=rust:1.90.0-bookworm
# virtiofsd v1.14.0, including the release Cargo.lock.
ARG VIRTIOFSD_REVISION=c2540f8db14caba81c1e37fba23fc7bf2cd7f0dd
ARG OTELCOL_CONTRIB_VERSION=0.120.0
ARG OTELCOL_CONTRIB_URL=https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_CONTRIB_VERSION}/otelcol-contrib_${OTELCOL_CONTRIB_VERSION}_linux_amd64.tar.gz
ARG AKERNEL_VERSION=unknown
ARG AKERNEL_REVISION=unknown
ARG ADX_RELEASE_URL=https://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/adx/daily/20260923062527-e295f895e279/linux/amd64/adx-release.tar.gz
ARG ADX_RELEASE_SHA256=602a738c4367923e8b31a7b0125d5b2069389d7fef93796c86a8d7b758a40dab

FROM ${AKERNEL_NODE_BASE_IMAGE} AS adx-release
ARG ADX_RELEASE_URL
ARG ADX_RELEASE_SHA256
RUN set -eux; \
    apt-get update; \
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
ARG GVISOR_AMD64_URL
ARG GVISOR_AMD64_SHA512
ARG TARGETARCH
COPY src/sandboxd/third_party/install-gvisor.sh /usr/local/libexec/install-gvisor.sh
RUN set -eux; \
    case "${TARGETARCH:-}" in \
      amd64) ;; \
      "") test "$(uname -m)" = "x86_64" ;; \
      *) echo "unsupported gVisor target architecture: ${TARGETARCH}" >&2; \
         exit 1 ;; \
    esac; \
    test -n "${GVISOR_RELEASE}"; \
    test -n "${GVISOR_AMD64_URL}"; \
    test -n "${GVISOR_AMD64_SHA512}"; \
    asset=/tmp/gvisor.tar.bz2; \
    apt-get update; \
    apt-get install -y --no-install-recommends bzip2 ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "${GVISOR_AMD64_URL}" -o "${asset}"; \
    bash /usr/local/libexec/install-gvisor.sh "${asset}" \
      "${GVISOR_AMD64_SHA512}" /gvisor

FROM ${KATA_BUILD_IMAGE} AS kata-runtime-true
ARG KATA_RELEASE
ARG KATA_AMD64_SHA256
ARG KATA_RELEASE_BASE_URL
ARG TARGETARCH
RUN set -eux; \
    test "${TARGETARCH:-amd64}" = "amd64"; \
    apt-get update; \
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
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        gcc \
        git \
        libc6-dev \
        make && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src/sandboxd
COPY ./src/sandboxd/ ./
RUN make release

FROM ${VIRTIOFSD_BUILD_IMAGE} AS virtiofsd-builder
ARG VIRTIOFSD_REVISION
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates git libcap-ng-dev libseccomp-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src/virtiofsd
RUN git init && \
    git fetch --depth=1 https://gitlab.com/virtio-fs/virtiofsd.git "${VIRTIOFSD_REVISION}" && \
    git checkout --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = "${VIRTIOFSD_REVISION}" && \
    cargo build --release --locked

FROM ${FIRECRACKER_BUILD_IMAGE} AS firecracker-runtime-true
ARG FIRECRACKER_RELEASE
ARG FIRECRACKER_AMD64_SHA256
ARG FIRECRACKER_AMD64_URL
ARG TARGETARCH
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates cpio curl gzip jq && \
    rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    test "${TARGETARCH:-amd64}" = "amd64"; \
    test -n "${FIRECRACKER_RELEASE}"; \
    test -n "${FIRECRACKER_AMD64_SHA256}"; \
    test -n "${FIRECRACKER_AMD64_URL}"; \
    archive="/tmp/firecracker-${FIRECRACKER_RELEASE}-x86_64.tgz"; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "${FIRECRACKER_AMD64_URL}" \
      -o "${archive}"; \
    echo "${FIRECRACKER_AMD64_SHA256}  ${archive}" | sha256sum -c -; \
    mkdir -p /tmp/firecracker-release \
      /firecracker/usr/local/bin \
      /firecracker/opt/firecracker/licenses \
      /initrd; \
    tar -xzf "${archive}" -C /tmp/firecracker-release; \
    bundle="/tmp/firecracker-release/release-${FIRECRACKER_RELEASE}-x86_64"; \
    jq -e --arg release "${FIRECRACKER_RELEASE}" \
      '.component == "akernel-firecracker-runtime" and \
       .repository == "akernel-dev/firecracker" and \
       .release_tag == $release and \
       .architecture == "x86_64"' \
      "${bundle}/manifest.json" >/dev/null; \
    (cd "${bundle}"; sha256sum -c SHA256SUMS); \
    install -m 0755 "${bundle}/firecracker" \
      /firecracker/usr/local/bin/firecracker; \
    install -m 0644 \
      "${bundle}/vmlinux" \
      "${bundle}/kernel.config" \
      "${bundle}/manifest.json" \
      /firecracker/opt/firecracker/; \
    cp -a "${bundle}/licenses/." /firecracker/opt/firecracker/licenses/

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
ARG RUNC_AMD64_SHA256
ARG RUNC_RELEASE_BASE_URL
ARG TARGETARCH
RUN set -eux; \
    test "${TARGETARCH:-amd64}" = "amd64"; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    asset=/tmp/runc.amd64; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "${RUNC_RELEASE_BASE_URL}/v${RUNC_VERSION}/runc.amd64" \
      -o "${asset}"; \
    echo "${RUNC_AMD64_SHA256}  ${asset}" | sha256sum -c -; \
    install -D -m 0755 "${asset}" /runc/usr/local/bin/runc; \
    rm -f "${asset}"
COPY --from=sandboxd-builder /src/sandboxd/output/runc-shim /runc/usr/local/bin/runc-shim

FROM ${RUNC_BUILD_IMAGE} AS runc-runtime-false
RUN mkdir -p /runc/usr/local/bin

FROM runc-runtime-${AKERNEL_ENABLE_RUNC} AS runc-runtime

FROM ubuntu:24.04 AS distill-fs-runtime
ARG TARGETARCH
ARG DISTILL_FS_RELEASE
ARG DISTILL_FS_AMD64_URL
ARG DISTILL_FS_AMD64_SHA256
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl jq binutils && \
    rm -rf /var/lib/apt/lists/*
COPY ./builder/scripts/install-distill-fs.sh /install-distill-fs.sh
RUN sh /install-distill-fs.sh "$DISTILL_FS_RELEASE" \
    "$DISTILL_FS_AMD64_URL" "$DISTILL_FS_AMD64_SHA256" /distill-fs

FROM ${AKERNEL_NODE_BASE_IMAGE}
# Let PID 1 systemd avoid remounting shared host filesystems during shutdown.
ENV container=oci

ARG AKERNEL_ENABLE_KATA
ARG AKERNEL_ENABLE_RUNC
ARG AKERNEL_ENABLE_FIRECRACKER
ARG AKERNEL_VERSION
ARG AKERNEL_REVISION
ARG GVISOR_RELEASE
ARG RUNC_VERSION
ARG FIRECRACKER_RELEASE
ARG LIBNVIDIA_CONTAINER_VERSION
ARG OTELCOL_CONTRIB_URL
ARG TARGETARCH
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
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
        systemd \
        systemd-sysv \
        tzdata \
        xfsprogs && \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fsSL --retry 10 --retry-delay 2 --retry-all-errors \
      https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg; \
    curl -fsSL --retry 10 --retry-delay 2 --retry-all-errors \
      https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed \
        's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      "libnvidia-container1=${LIBNVIDIA_CONTAINER_VERSION}" \
      "libnvidia-container-tools=${LIBNVIDIA_CONTAINER_VERSION}"; \
    rm -rf /var/lib/apt/lists/*

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


COPY --from=runtime-image /akernel-runtime-rootfs.img /opt/akernel/runtime/akernel-runtime-rootfs.img

# ADX provides the control and data plane processes. AKernel keeps building
# sandboxd and its runtime rootfs from pinned sources and installs only EXECD
# from the verified ADX release into that rootfs.
COPY --from=adx-release /opt/adx/current/bin/ /opt/adx/current/bin/
COPY --from=adx-release /opt/adx/current/sdk/ /opt/adx/current/sdk/
COPY --from=adx-release /opt/adx/current/runtime/adx-execd /opt/adx/current/runtime/adx-execd
RUN set -eux; \
    test -x /opt/adx/current/bin/adxctl; \
    test -x /opt/adx/current/bin/adx-coordinator; \
    test -x /opt/adx/current/bin/adxlet; \
    test -x /opt/adx/current/bin/adx-apiserver; \
    test -x /opt/adx/current/runtime/adx-execd; \
    test ! -e /opt/adx/current/runtime/adx-runtime-rootfs.img; \
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
COPY ./builder/scripts/adx-service.sh /usr/local/bin/adx-service
COPY ./builder/scripts/sandboxd_network_prepare.sh /usr/local/bin/sandboxd-network-prepare
COPY ./builder/config/adx-standalone.yaml /etc/akernel/adx-standalone.yaml
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

COPY ./builder/config/otel-collector-config.yaml /etc/akernel/otel_config.yaml
COPY ./builder/config/logrotate.d/gvisor /etc/logrotate.d/gvisor
COPY ./builder/scripts/*.sh /root/
COPY ./builder/systemd_services/*.service /etc/systemd/system/

RUN curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
        "${OTELCOL_CONTRIB_URL}" \
    | tar -xz -C /usr/local/bin otelcol-contrib && \
    chmod 0755 /usr/local/bin/otelcol-contrib

RUN chmod 0644 /etc/logrotate.d/gvisor && \
    systemctl mask getty-static.service && \
    systemctl enable logrotate.timer otel_collector.service sandboxd.service adx.service

LABEL org.opencontainers.image.version="${AKERNEL_VERSION}" \
      org.opencontainers.image.revision="${AKERNEL_REVISION}" \
      org.akernel.runtime.profile="rrt" \
      org.akernel.gvisor.release="${GVISOR_RELEASE}" \
      org.akernel.runc.version="${RUNC_VERSION}" \
      org.akernel.runc.enabled="${AKERNEL_ENABLE_RUNC}" \
      org.akernel.kata.enabled="${AKERNEL_ENABLE_KATA}" \
      org.akernel.firecracker.release="${FIRECRACKER_RELEASE}" \
      org.akernel.firecracker.enabled="${AKERNEL_ENABLE_FIRECRACKER}"

STOPSIGNAL SIGRTMIN+3
