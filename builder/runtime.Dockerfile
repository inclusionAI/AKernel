# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

ARG AKERNEL_RUNTIME_BASE_IMAGE=ubuntu:24.04
ARG ADX_VERSION=v0.1.0rc2
ARG ADX_EXECD_URL=https://agent-dx.obs.cn-southwest-2.myhuaweicloud.com/adx/daily/20261031094839-3cd9e575dea0/linux/amd64/adx-execd.tar.gz
ARG ADX_EXECD_SHA256=92db5531d26b4fd7bea3f1f3df63eb3b07c79c55d850d9c6d133f31b7400709d

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS adx-execd
ARG ADX_EXECD_URL
ARG ADX_EXECD_SHA256
ARG TARGETARCH
COPY ./builder/scripts/verify-elf-arch.sh /usr/local/libexec/verify-elf-arch.sh
ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh
RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    set -eux; \
    sh /tmp/apt-get-update.sh; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /opt/adx-execd; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "${ADX_EXECD_URL}" -o /tmp/adx-execd.tar.gz; \
    echo "${ADX_EXECD_SHA256}  /tmp/adx-execd.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/adx-execd.tar.gz -C /opt/adx-execd; \
    test -x /opt/adx-execd/adx-execd; \
    sh /usr/local/libexec/verify-elf-arch.sh /opt/adx-execd/adx-execd "${TARGETARCH}"; \
    test -f /opt/adx-execd/manifest.json; \
    rm -f /tmp/adx-execd.tar.gz

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS execd-runtime-rootfs

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    sh /tmp/apt-get-update.sh && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        libgcc-s1 \
        tini && \
    rm -rf /var/lib/apt/lists/* && \
    test -x /usr/bin/tini-static && \
    /usr/bin/tini-static --version

RUN mkdir -p /var/task /__adx && \
    ln -sfn /home /__adx/home && \
    ln -sfn /usr /__adx/usr && \
    ln -sfn /opt /__adx/opt && \
    ln -sfn /root /__adx/root

COPY --from=adx-execd /opt/adx-execd/adx-execd /usr/local/bin/adx-execd

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS erofs-builder-base

ENV DEBIAN_FRONTEND=noninteractive

ARG AKERNEL_APT_UBUNTU_MIRROR
ARG AKERNEL_APT_UBUNTU_PORTS_MIRROR
RUN --mount=type=bind,source=builder/scripts/configure-apt-mirror.sh,target=/tmp/configure-apt-mirror.sh \
    sh /tmp/configure-apt-mirror.sh

RUN --mount=type=bind,source=builder/scripts/apt-get-update.sh,target=/tmp/apt-get-update.sh \
    sh /tmp/apt-get-update.sh && \
    apt-get install -y --no-install-recommends erofs-utils && \
    rm -rf /var/lib/apt/lists/*

FROM erofs-builder-base AS execd-erofs-builder

COPY --from=execd-runtime-rootfs / /rootfs
RUN mkfs.erofs -E noinline_data /akernel-runtime-rootfs.img /rootfs && \
    fsck.erofs /akernel-runtime-rootfs.img

FROM scratch AS runtime-execd
COPY --from=execd-erofs-builder /akernel-runtime-rootfs.img /akernel-runtime-rootfs.img
LABEL org.akernel.runtime.profile="execd"
