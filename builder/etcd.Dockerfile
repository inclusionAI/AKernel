# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

ARG ETCD_VERSION=3.6.8

FROM gcr.io/etcd-development/etcd:v${ETCD_VERSION} AS upstream

FROM alpine:3.22

ARG ETCD_VERSION

LABEL org.opencontainers.image.title="AKernel etcd" \
      org.opencontainers.image.description="etcd with a POSIX shell for the AKernel Helm chart" \
      org.opencontainers.image.source="https://github.com/etcd-io/etcd" \
      org.opencontainers.image.version="${ETCD_VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apk add --no-cache ca-certificates \
    && addgroup -S -g 1001 etcd \
    && adduser -S -D -H -u 1001 -G etcd etcd \
    && install -d -o etcd -g etcd /etcd /usr/share/licenses/etcd

COPY --from=upstream \
    /usr/local/bin/etcd \
    /usr/local/bin/etcdctl \
    /usr/local/bin/etcdutl \
    /usr/local/bin/
COPY LICENSE /usr/share/licenses/etcd/LICENSE
COPY builder/etcd.NOTICE /usr/share/licenses/etcd/NOTICE

RUN chmod 0755 /usr/local/bin/etcd /usr/local/bin/etcdctl /usr/local/bin/etcdutl

USER 1001:1001
WORKDIR /etcd

EXPOSE 2379 2378

ENTRYPOINT ["/usr/local/bin/etcd"]
