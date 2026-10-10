# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

# 此开发镜像只消费已经冻结并经过 SHA256 检查的构建产物。
ARG BASE_IMAGE=ubuntu@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
FROM ${BASE_IMAGE}
ENV container=oci DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates e2fsprogs fuse3 iproute2 ipset iptables libseccomp2 \
    mount openssl procps python3 python3-yaml tini && rm -rf /var/lib/apt/lists/*
COPY payload/ /
COPY SHA256SUMS /opt/akernel/SHA256SUMS
RUN cd / && sha256sum --check /opt/akernel/SHA256SUMS
COPY source/builder/scripts/adx-service.sh /usr/local/bin/adx-service
COPY source/builder/scripts/sandboxd_network_prepare.sh /usr/local/bin/sandboxd-network-prepare
COPY source/deploy/standalone/scheduler-dev-entrypoint.py /usr/local/bin/scheduler-dev-entrypoint.py
COPY source/deploy/standalone/config/ /etc/akernel/dev-templates/
RUN chmod 0755 /usr/local/bin/adx-service /usr/local/bin/sandboxd-network-prepare && \
    update-alternatives --set iptables /usr/sbin/iptables-legacy && \
    update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/usr/local/bin/scheduler-dev-entrypoint.py"]
