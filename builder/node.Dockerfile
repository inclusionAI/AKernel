# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

ARG AKERNEL_NODE_BASE_IMAGE=ubuntu:24.04
ARG AKERNEL_RUNTIME_IMAGE=akernel-runtime:local
ARG SANDBOXD_BUILD_IMAGE=golang:1.25.5-bookworm
ARG DISTILL_FS_BUILD_IMAGE=rust:1.85.0-bookworm
ARG OPEN_YR_VERSION=0.9.1
ARG OPEN_YR_RELEASE_BASE_URL=https://github.com/openYuanrong-mirror/yuanrong/releases/download
ARG GVISOR_RELEASE=release-20260706.0
ARG GVISOR_RELEASE_BASE_URL=https://storage.googleapis.com/gvisor/releases
ARG OTELCOL_CONTRIB_VERSION=0.120.0
ARG OTELCOL_CONTRIB_URL=
ARG AKERNEL_VERSION=unknown
ARG AKERNEL_REVISION=unknown

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
ARG AKERNEL_VERSION
ARG AKERNEL_REVISION
ARG OPEN_YR_VERSION
ARG OPEN_YR_RELEASE_BASE_URL
ARG GVISOR_RELEASE
ARG GVISOR_RELEASE_BASE_URL
ARG OTELCOL_CONTRIB_VERSION
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
        iproute2 \
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

RUN if command -v update-alternatives >/dev/null 2>&1; then \
        update-alternatives --set iptables /usr/sbin/iptables-legacy || true; \
        update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy || true; \
    fi

RUN set -eux; \
    case "${TARGETARCH:-}" in \
        amd64) gvisor_arch="x86_64" ;; \
        arm64) gvisor_arch="aarch64" ;; \
        "") \
            case "$(uname -m)" in \
                x86_64) gvisor_arch="x86_64" ;; \
                aarch64|arm64) gvisor_arch="aarch64" ;; \
                *) echo "unsupported gVisor target architecture: $(uname -m)" >&2; exit 1 ;; \
            esac ;; \
        *) echo "unsupported gVisor target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    gvisor_version="${GVISOR_RELEASE#release-}"; \
    if [ "${gvisor_version}" = "${GVISOR_RELEASE}" ]; then \
        echo "GVISOR_RELEASE must be an official tag such as release-20260706.0" >&2; \
        exit 1; \
    fi; \
    gvisor_url="${GVISOR_RELEASE_BASE_URL}/release/${gvisor_version}/${gvisor_arch}"; \
    mkdir -p /tmp/gvisor-release; \
    cd /tmp/gvisor-release; \
    curl -fSLO --retry 10 --retry-delay 2 --retry-all-errors "${gvisor_url}/runsc"; \
    curl -fSLO --retry 10 --retry-delay 2 --retry-all-errors "${gvisor_url}/runsc.sha512"; \
    sha512sum -c runsc.sha512; \
    install -m 0755 runsc /usr/local/bin/runsc; \
    rm -rf /tmp/gvisor-release

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

# Download and unpack the openyuanrong runtime tarball from the GitHub
# release mirror. The .sha256 sidecar verifies the download before unpacking.
RUN mkdir -p "${YR_INSTALLATION_DIR}" /tmp/yr-release && \
    cd /tmp/yr-release && \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) artifact_arch="amd64" ;; \
      arm64) artifact_arch="arm64" ;; \
      *) echo "unsupported openYuanrong target architecture: ${TARGETARCH:-$(dpkg --print-architecture)}" >&2; exit 1 ;; \
    esac && \
    yr_tarball="openyuanrong-${OPEN_YR_VERSION}-linux-${artifact_arch}.tar.gz" && \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors -O \
      "${OPEN_YR_RELEASE_BASE_URL}/${OPEN_YR_VERSION}/${yr_tarball}" && \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors -O \
      "${OPEN_YR_RELEASE_BASE_URL}/${OPEN_YR_VERSION}/${yr_tarball}.sha256" && \
    sha256sum -c "${yr_tarball}.sha256" && \
    tar -xzf "${yr_tarball}" -C "${YR_INSTALLATION_DIR}" --strip-components=1 && \
    cd / && rm -rf /tmp/yr-release && \
    ln -sf "${YR_INSTALLATION_DIR}/functionsystem/bin/yr" /usr/bin/yr

COPY --from=runtime-image /yr-runtime-rootfs.img ${YR_INSTALLATION_DIR}/yr-runtime-rootfs.img

COPY --from=sandboxd-builder /src/sandboxd/output/sandboxd /usr/local/bin/sandboxd
COPY --from=sandboxd-builder /src/sandboxd/output/sbox /usr/local/bin/sbox
COPY --from=distill-fs-builder /src/distill-fs/target/release/distill_fs /usr/local/bin/distill_fs

COPY ./builder/scripts/akernel-entrypoint.sh /usr/local/bin/akernel-entrypoint
COPY ./builder/scripts/ensure-component-cert.sh /usr/local/bin/ensure-component-cert
RUN chmod 0755 \
        /usr/local/bin/runsc \
        /usr/local/bin/sandboxd \
        /usr/local/bin/sbox \
        /usr/local/bin/distill_fs \
        /usr/local/bin/akernel-entrypoint \
        /usr/local/bin/ensure-component-cert

COPY ./builder/config/yr_services.yaml ${YR_INSTALLATION_DIR}/deploy/process/services.yaml

RUN mkdir -p ${YR_INSTALLATION_DIR}/metrics ${YR_INSTALLATION_DIR}/trace
COPY ./builder/config/otel-collector-config.yaml ${YR_INSTALLATION_DIR}/otel_config.yaml
COPY ./builder/config/metrics_config.json ${YR_INSTALLATION_DIR}/metrics/metrics_config.json
COPY ./builder/config/trace_config.json ${YR_INSTALLATION_DIR}/trace/trace_config.json
COPY ./builder/config/logrotate.d/gvisor /etc/logrotate.d/gvisor
COPY ./builder/scripts/yr_node_bootstrap.sh ${YR_INSTALLATION_DIR}/yr_node_bootstrap.sh
COPY ./builder/scripts/master_entrypoint.sh ${YR_INSTALLATION_DIR}/entrypoint.sh
COPY ./builder/scripts/*.sh /root/
COPY ./builder/systemd_services/*.service /etc/systemd/system/

RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) artifact_arch="amd64" ;; \
      arm64) artifact_arch="arm64" ;; \
      *) echo "unsupported OpenTelemetry target architecture: ${TARGETARCH:-$(dpkg --print-architecture)}" >&2; exit 1 ;; \
    esac; \
    otelcol_url="${OTELCOL_CONTRIB_URL:-https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_CONTRIB_VERSION}/otelcol-contrib_${OTELCOL_CONTRIB_VERSION}_linux_${artifact_arch}.tar.gz}"; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
        "${otelcol_url}" \
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
      org.opencontainers.image.revision="${AKERNEL_REVISION}"

ENV YR_LOG_PATH=${YR_INSTALLATION_DIR}/logs
STOPSIGNAL SIGRTMIN+3
