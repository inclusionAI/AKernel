# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
ARG PYTHON_IMAGE=docker.io/library/python:3.12.15-slim-bookworm@sha256:57d96f71de145a98e7ceca18ee4ade77bba4103a33a72dfaea026b23625c6bf0
ARG POSTGRES_IMAGE=ghcr.io/cloudnative-pg/postgresql:17.11@sha256:27391a45ec43c3a93f61765e9161c0aaa9f8a46a9ff7a687313fe7c3c6b86549
ARG JUICEFS_IMAGE=docker.io/juicedata/mount:ce-v1.4.1@sha256:e7fff4153156a112ffdc69517c70c2be1665800add19d7cfdfa7c0362a9bd1d9
# A qualified all-in-one image containing /opt/adx/current/bin/redis-cli.
ARG REDIS_TOOLS_IMAGE
FROM ${PYTHON_IMAGE} AS python-deps
COPY requirements.lock /tmp/requirements.lock
RUN python3 -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
FROM ${POSTGRES_IMAGE} AS pg
FROM ${REDIS_TOOLS_IMAGE} AS redis
FROM ${JUICEFS_IMAGE}
COPY --from=pg /usr/lib/postgresql/17/bin/pg_dump /usr/lib/postgresql/17/bin/pg_dump
COPY --from=pg /usr/lib/postgresql/17/bin/pg_restore /usr/lib/postgresql/17/bin/pg_restore
COPY --from=pg /usr/lib/postgresql/17/bin/psql /usr/lib/postgresql/17/bin/psql
COPY --from=pg /usr/lib/x86_64-linux-gnu/libpq.so.5.18 /usr/local/lib/akernel-backup/libpq.so.5
COPY --from=pg /usr/share/doc/postgresql-client-17/copyright /usr/share/doc/akernel-backup/postgresql-client-17-copyright
COPY --from=pg /usr/share/doc/libpq5/copyright /usr/share/doc/akernel-backup/libpq5-copyright
COPY --from=redis /opt/adx/current/bin/redis-cli /usr/local/bin/redis-cli
COPY --from=redis /opt/adx/current/third_party/redis/LICENSE /usr/share/doc/akernel-backup/redis-LICENSE
COPY --from=redis /opt/adx/current/third_party/redis/source.json /usr/share/doc/akernel-backup/redis-source.json
COPY --from=python-deps /usr/local/ /opt/backup/python/
ENV PATH="/opt/backup/python/bin:/usr/lib/postgresql/17/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    LD_LIBRARY_PATH="/opt/backup/python/lib:/usr/local/lib/akernel-backup" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# The application is a versioned ConfigMap so the image contains tools only.
RUN python3 -c 'import ssl, oss2; from alibabacloud_credentials.client import Client' \
    && pg_dump --version && pg_restore --version && redis-cli --version && juicefs --version
ENTRYPOINT ["/opt/backup/python/bin/python3.12"]
CMD ["--version"]
