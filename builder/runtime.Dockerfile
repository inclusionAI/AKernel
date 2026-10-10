# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

ARG AKERNEL_RUNTIME_BASE_IMAGE=ubuntu:24.04
ARG ADX_EXECD_URL=https://github.com/openJiuwen-ai/agent-dx/releases/download/v0.1.0rc1/adx-execd-v0.1.0rc1-linux-amd64.tar.gz
ARG ADX_EXECD_SHA256=ba4e4a62982656fe32b66bcdb8dc1e1c54ef28cc57d302702ef0229cdd0007db

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS adx-execd
ARG ADX_EXECD_URL
ARG ADX_EXECD_SHA256
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /opt/adx-execd; \
    curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
      "${ADX_EXECD_URL}" -o /tmp/adx-execd.tar.gz; \
    echo "${ADX_EXECD_SHA256}  /tmp/adx-execd.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/adx-execd.tar.gz -C /opt/adx-execd; \
    test -x /opt/adx-execd/adx-execd; \
    test -f /opt/adx-execd/manifest.json; \
    rm -f /tmp/adx-execd.tar.gz

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS execd-runtime-rootfs

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && \
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

RUN apt-get update && \
    apt-get install -y --no-install-recommends erofs-utils && \
    rm -rf /var/lib/apt/lists/*

FROM erofs-builder-base AS execd-erofs-builder

COPY --from=execd-runtime-rootfs / /rootfs
RUN mkfs.erofs -E noinline_data /akernel-runtime-rootfs.img /rootfs && \
    fsck.erofs /akernel-runtime-rootfs.img

FROM scratch AS runtime-execd
COPY --from=execd-erofs-builder /akernel-runtime-rootfs.img /akernel-runtime-rootfs.img
LABEL org.akernel.runtime.profile="execd"
