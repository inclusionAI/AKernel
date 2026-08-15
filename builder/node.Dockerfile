# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

ARG AKERNEL_NODE_BASE_IMAGE=ubuntu:24.04
ARG AKERNEL_RUNTIME_IMAGE=akernel-runtime:local
ARG AKERNEL_RUNTIME_PROFILE=rrt
ARG AKERNEL_ENABLE_KATA=true
ARG SANDBOXD_BUILD_IMAGE=golang:1.25.5-bookworm
ARG DISTILL_FS_BUILD_IMAGE=rust:1.85.0-bookworm
ARG OPEN_YR_VERSION=0.9.7
ARG OPEN_YR_CORE_WHEEL_URL=
ARG OPEN_YR_CORE_WHEEL_SHA256=
ARG OPEN_YR_RELEASE_BASE_URL=https://github.com/openYuanrong-mirror/yuanrong/releases/download
ARG OPEN_YR_CORE_AMD64_SHA256=0a890db1785e349bfd625844a05059bdd494e32a429cea771cf969f09e3aba2c
ARG OPEN_YR_CORE_ARM64_SHA256=64e14233fcbbb3418311d2f242e164e7be6e7bee0315c7619b18d9c5ddd01a76
ARG GVISOR_RELEASE=release-20260706.0
ARG GVISOR_RELEASE_BASE_URL=https://storage.googleapis.com/gvisor/releases
ARG LIBNVIDIA_CONTAINER_VERSION=1.19.1-1
ARG KATA_BUILD_IMAGE=ubuntu:24.04
ARG KATA_RELEASE=4.0.0
ARG KATA_AMD64_SHA256=2c3b9dfeba355582b40aee462b12916c9740654d0230f696adf719d67b063a8c
ARG KATA_RELEASE_BASE_URL=https://github.com/kata-containers/kata-containers/releases/download
ARG OTELCOL_CONTRIB_VERSION=0.120.0
ARG OTELCOL_CONTRIB_URL=https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_CONTRIB_VERSION}/otelcol-contrib_${OTELCOL_CONTRIB_VERSION}_linux_amd64.tar.gz
ARG AKERNEL_VERSION=unknown
ARG AKERNEL_REVISION=unknown

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
ARG GVISOR_RELEASE_BASE_URL
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

RUN set -eux; \
    case "${TARGETARCH:-}" in \
        amd64) gvisor_arch="x86_64" ;; \
        "") \
            [ "$(uname -m)" = "x86_64" ] || { echo "unsupported gVisor target architecture: $(uname -m)" >&2; exit 1; }; \
            gvisor_arch="x86_64" ;; \
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

# Install the complete, language-runtime-free openYuanRong control plane from
# its checksum-pinned core wheel. A URL and checksum pair may override the
# release asset when validating an unreleased daily build.
RUN set -eux; \
    case "${TARGETARCH:-}" in \
      amd64) wheel_arch=x86_64; release_sha="${OPEN_YR_CORE_AMD64_SHA256}" ;; \
      arm64) wheel_arch=aarch64; release_sha="${OPEN_YR_CORE_ARM64_SHA256}" ;; \
      "") \
        case "$(uname -m)" in \
          x86_64) wheel_arch=x86_64; release_sha="${OPEN_YR_CORE_AMD64_SHA256}" ;; \
          aarch64) wheel_arch=aarch64; release_sha="${OPEN_YR_CORE_ARM64_SHA256}" ;; \
          *) echo "unsupported openYuanRong target architecture: $(uname -m)" >&2; exit 1 ;; \
        esac ;; \
      *) echo "unsupported openYuanRong target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    wheel_name="openyuanrong_core-${OPEN_YR_VERSION}-py3-none-manylinux_2_31_${wheel_arch}.whl"; \
    wheel_url="${OPEN_YR_RELEASE_BASE_URL}/${OPEN_YR_VERSION}/${wheel_name}"; \
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

COPY --from=sandboxd-builder /src/sandboxd/output/sandboxd /usr/local/bin/sandboxd
COPY --from=sandboxd-builder /src/sandboxd/output/sbox /usr/local/bin/sbox
COPY --from=sandboxd-builder /src/sandboxd/output/sandbox-logger /usr/local/bin/sandbox-logger
COPY --from=distill-fs-builder /src/distill-fs/target/release/distill_fs /usr/local/bin/distill_fs
COPY --from=kata-runtime /kata/opt/kata /opt/kata
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
      org.akernel.kata.enabled="${AKERNEL_ENABLE_KATA}"

ENV YR_LOG_PATH=${YR_INSTALLATION_DIR}/logs
STOPSIGNAL SIGRTMIN+3
