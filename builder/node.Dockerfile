# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

ARG AKERNEL_NODE_BASE_IMAGE=ubuntu:24.04
ARG AKERNEL_RUNTIME_IMAGE=akernel-runtime:local
ARG AKERNEL_RUNTIME_PROFILE=rrt
ARG AKERNEL_ENABLE_KATA=true
ARG AKERNEL_ENABLE_RUNC=false
ARG AKERNEL_ENABLE_FIRECRACKER=true
ARG SANDBOXD_BUILD_IMAGE=golang:1.25.5-bookworm
ARG DISTILL_FS_BUILD_IMAGE=rust:1.85.0-bookworm
ARG OPEN_YR_VERSION=0.10.1rc4
ARG OPEN_YR_CORE_WHEEL_URL=
ARG OPEN_YR_CORE_WHEEL_SHA256=
ARG OPEN_YR_RELEASE_BASE_URL=https://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/release
ARG OPEN_YR_CORE_AMD64_SHA256=65c1f27e7e700a253a2e907dea0273e85f1c76610e48c93544caa6bcc07ac3af
ARG OPEN_YR_CORE_ARM64_SHA256=29d25c3388c2913346035ee8df9b8159e702de7a8e6f783e3e218ab773896333
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
ARG OTELCOL_CONTRIB_VERSION=0.120.0
ARG OTELCOL_CONTRIB_URL=https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_CONTRIB_VERSION}/otelcol-contrib_${OTELCOL_CONTRIB_VERSION}_linux_amd64.tar.gz
ARG AKERNEL_VERSION=unknown
ARG AKERNEL_REVISION=unknown

FROM ${GVISOR_DOWNLOAD_IMAGE} AS gvisor-runtime
ARG GVISOR_RELEASE
ARG GVISOR_AMD64_URL
ARG GVISOR_AMD64_SHA512
ARG TARGETARCH
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
    asset=/tmp/runsc; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "${GVISOR_AMD64_URL}" -o "${asset}"; \
    echo "${GVISOR_AMD64_SHA512}  ${asset}" | sha512sum -c -; \
    install -D -m 0755 "${asset}" /gvisor/runsc

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

FROM ${DISTILL_FS_BUILD_IMAGE} AS distill-fs-builder
ENV DEBIAN_FRONTEND=noninteractive \
    CARGO_NET_GIT_FETCH_WITH_CLI=true
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        cmake \
        g++ \
        gcc \
        git \
        make \
        perl \
        pkg-config && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src/distill-fs
COPY ./src/distill-fs/ ./
RUN cargo build --locked --release --bin distill_fs

FROM ${AKERNEL_NODE_BASE_IMAGE}
ARG AKERNEL_ENABLE_KATA
ARG AKERNEL_ENABLE_RUNC
ARG AKERNEL_ENABLE_FIRECRACKER
ARG AKERNEL_RUNTIME_PROFILE
ARG AKERNEL_VERSION
ARG AKERNEL_REVISION
ARG OPEN_YR_VERSION
ARG OPEN_YR_CORE_WHEEL_URL
ARG OPEN_YR_CORE_WHEEL_SHA256
ARG OPEN_YR_RELEASE_BASE_URL
ARG OPEN_YR_CORE_AMD64_SHA256
ARG OPEN_YR_CORE_ARM64_SHA256
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
        libgcc-s1 \
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


ENV YR_INSTALLATION_DIR=/home/yuanrong

# Install the complete, language-runtime-free openYuanRong control plane from
# its checksum-pinned core wheel. A URL and checksum pair may override the
# release asset when validating an unreleased daily build.
RUN set -eux; \
    case "${TARGETARCH:-}" in \
      amd64) wheel_arch=x86_64; wheel_platform=amd64; release_sha="${OPEN_YR_CORE_AMD64_SHA256}" ;; \
      arm64) wheel_arch=aarch64; wheel_platform=arm64; release_sha="${OPEN_YR_CORE_ARM64_SHA256}" ;; \
      "") \
        case "$(uname -m)" in \
          x86_64) wheel_arch=x86_64; wheel_platform=amd64; release_sha="${OPEN_YR_CORE_AMD64_SHA256}" ;; \
          aarch64) wheel_arch=aarch64; wheel_platform=arm64; release_sha="${OPEN_YR_CORE_ARM64_SHA256}" ;; \
          *) echo "unsupported openYuanRong target architecture: $(uname -m)" >&2; exit 1 ;; \
        esac ;; \
      *) echo "unsupported openYuanRong target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    wheel_name="openyuanrong_core-${OPEN_YR_VERSION}-py3-none-manylinux_2_31_${wheel_arch}.whl"; \
    wheel_url="${OPEN_YR_RELEASE_BASE_URL}/${OPEN_YR_VERSION}/linux/${wheel_platform}/${wheel_name}"; \
    wheel_sha="${release_sha}"; \
    if [ -n "${OPEN_YR_CORE_WHEEL_URL}" ]; then \
      test -n "${OPEN_YR_CORE_WHEEL_SHA256}"; \
      wheel_name="$(python3 -c 'import os, sys, urllib.parse; print(os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(sys.argv[1]).path)))' "${OPEN_YR_CORE_WHEEL_URL}")"; \
      case "${wheel_name}" in *.whl) ;; *) echo "OPEN_YR_CORE_WHEEL_URL must reference a .whl file" >&2; exit 1 ;; esac; \
      wheel_url="${OPEN_YR_CORE_WHEEL_URL}"; \
      wheel_sha="${OPEN_YR_CORE_WHEEL_SHA256}"; \
    else \
      test -z "${OPEN_YR_CORE_WHEEL_SHA256}"; \
    fi; \
    wheel="/tmp/${wheel_name}"; \
    target=/tmp/openyuanrong-core; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "${wheel_url}" -o "${wheel}"; \
    echo "${wheel_sha}  ${wheel}" | sha256sum -c -; \
    python3 -m pip install \
      --break-system-packages \
      --no-cache-dir \
      --no-deps \
      --target "${target}" \
      "${wheel}"; \
    test -x "${target}/yr/functionsystem/bin/yr"; \
    mkdir -p "${YR_INSTALLATION_DIR}"; \
    cp -a "${target}/yr/." "${YR_INSTALLATION_DIR}/"; \
    rm -rf "${target}" "${wheel}"; \
    ln -sfn "${YR_INSTALLATION_DIR}/functionsystem/bin/yr" /usr/bin/yr

COPY --from=runtime-image /yr-runtime-rootfs.img ${YR_INSTALLATION_DIR}/yr-runtime-rootfs.img

COPY --from=gvisor-runtime /gvisor/runsc /usr/local/bin/runsc
COPY --from=sandboxd-builder /src/sandboxd/output/sandboxd /usr/local/bin/sandboxd
COPY --from=sandboxd-builder /src/sandboxd/output/sbox /usr/local/bin/sbox
COPY --from=sandboxd-builder /src/sandboxd/output/sandbox-logger /usr/local/bin/sandbox-logger
COPY --from=distill-fs-builder /src/distill-fs/target/release/distill_fs /usr/local/bin/distill_fs
COPY --from=kata-runtime /kata/opt/kata /opt/kata
COPY --from=runc-runtime /runc/usr/local/bin/ /usr/local/bin/
COPY --from=firecracker-runtime /firecracker/ /
RUN if [ "${AKERNEL_ENABLE_KATA}" = "true" ]; then \
      ln -sf /opt/kata/runtime-rs/bin/containerd-shim-kata-v2 /usr/local/bin/containerd-shim-kata-v2; \
    fi

COPY ./builder/scripts/akernel-entrypoint.sh /usr/local/bin/akernel-entrypoint
COPY ./builder/scripts/ensure-component-cert.sh /usr/local/bin/ensure-component-cert
COPY ./builder/scripts/sandboxd_network_prepare.sh /usr/local/bin/sandboxd-network-prepare
RUN chmod 0755 \
        /usr/local/bin/runsc \
        /usr/local/bin/sandboxd \
        /usr/local/bin/sbox \
        /usr/local/bin/sandbox-logger \
        /usr/local/bin/distill_fs \
        /usr/local/bin/akernel-entrypoint \
        /usr/local/bin/ensure-component-cert \
        /usr/local/bin/sandboxd-network-prepare
RUN if [ "${AKERNEL_ENABLE_KATA}" = "true" ]; then chmod 0755 /usr/local/bin/containerd-shim-kata-v2; fi
RUN if [ "${AKERNEL_ENABLE_RUNC}" = "true" ]; then \
      chmod 0755 /usr/local/bin/runc /usr/local/bin/runc-shim; \
    else \
      test ! -e /usr/local/bin/runc; \
      test ! -e /usr/local/bin/runc-shim; \
    fi

COPY ./builder/config/yr_services.yaml /tmp/yr_services_rrt.yaml
COPY ./builder/config/yr_services_python.yaml /tmp/yr_services_python.yaml
RUN set -eux; \
    case "${AKERNEL_RUNTIME_PROFILE}" in \
      rrt) services=/tmp/yr_services_rrt.yaml ;; \
      python) services=/tmp/yr_services_python.yaml ;; \
      *) echo "unsupported AKERNEL_RUNTIME_PROFILE: ${AKERNEL_RUNTIME_PROFILE}" >&2; exit 1 ;; \
    esac; \
    install -D -m 0644 "${services}" ${YR_INSTALLATION_DIR}/deploy/process/services.yaml; \
    rm -f /tmp/yr_services_rrt.yaml /tmp/yr_services_python.yaml

RUN mkdir -p ${YR_INSTALLATION_DIR}/metrics ${YR_INSTALLATION_DIR}/trace
COPY ./builder/config/otel-collector-config.yaml ${YR_INSTALLATION_DIR}/otel_config.yaml
COPY ./builder/config/metrics_config.json ${YR_INSTALLATION_DIR}/metrics/metrics_config.json
COPY ./builder/config/trace_config.json ${YR_INSTALLATION_DIR}/trace/trace_config.json
COPY ./builder/config/logrotate.d/gvisor /etc/logrotate.d/gvisor
COPY ./builder/scripts/yr_node_bootstrap.sh ${YR_INSTALLATION_DIR}/yr_node_bootstrap.sh
COPY ./builder/scripts/master_entrypoint.sh ${YR_INSTALLATION_DIR}/entrypoint.sh
COPY ./builder/scripts/*.sh /root/
COPY ./builder/systemd_services/*.service /etc/systemd/system/

RUN curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
        "${OTELCOL_CONTRIB_URL}" \
    | tar -xz -C /usr/local/bin otelcol-contrib && \
    chmod 0755 /usr/local/bin/otelcol-contrib

RUN mkdir -p ${YR_INSTALLATION_DIR}/logs ${YR_INSTALLATION_DIR}/metrics ${YR_INSTALLATION_DIR}/trace && \
    chmod 0755 ${YR_INSTALLATION_DIR}/yr_node_bootstrap.sh ${YR_INSTALLATION_DIR}/entrypoint.sh && \
    chmod 0644 /etc/logrotate.d/gvisor && \
    systemctl mask getty-static.service || true && \
    systemctl enable logrotate.timer && \
    systemctl enable otel_collector.service && \
    systemctl enable sandboxd.service && \
    systemctl enable yuanrong.service

LABEL org.opencontainers.image.version="${AKERNEL_VERSION}" \
      org.opencontainers.image.revision="${AKERNEL_REVISION}" \
      org.akernel.runtime.profile="${AKERNEL_RUNTIME_PROFILE}" \
      org.akernel.gvisor.release="${GVISOR_RELEASE}" \
      org.akernel.runc.version="${RUNC_VERSION}" \
      org.akernel.runc.enabled="${AKERNEL_ENABLE_RUNC}" \
      org.akernel.kata.enabled="${AKERNEL_ENABLE_KATA}" \
      org.akernel.firecracker.release="${FIRECRACKER_RELEASE}" \
      org.akernel.firecracker.enabled="${AKERNEL_ENABLE_FIRECRACKER}"

ENV YR_LOG_PATH=${YR_INSTALLATION_DIR}/logs
STOPSIGNAL SIGRTMIN+3
