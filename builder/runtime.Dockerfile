# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

ARG AKERNEL_RUNTIME_BASE_IMAGE=ubuntu:24.04
ARG UV_VERSION=0.7.13
ARG PYTHON_310_VERSION=3.10.18
ARG PYTHON_311_VERSION=3.11.13
ARG PYTHON_312_VERSION=3.12.11
ARG PYTHON_313_VERSION=3.13.5

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS python-runtime-base

ARG UV_VERSION
ARG PYTHON_310_VERSION
ARG PYTHON_311_VERSION
ARG PYTHON_312_VERSION
ARG PYTHON_313_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_HTTP_TIMEOUT=120 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LD_LIBRARY_PATH=/usr/local/lib \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libbz2-1.0 \
        libffi8 \
        liblzma5 \
        libreadline8 \
        libsqlite3-0 \
        libssl3 \
        python3 \
        python3-pip \
        xz-utils \
        zlib1g && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --timeout 120 \
        --retries 10 \
        "uv==${UV_VERSION}"

RUN uv python install \
        "${PYTHON_310_VERSION}" \
        "${PYTHON_311_VERSION}" \
        "${PYTHON_312_VERSION}" \
        "${PYTHON_313_VERSION}"; \
    rm -rf "${UV_CACHE_DIR}"

RUN set -eux; \
    for spec in \
        "3.10:${PYTHON_310_VERSION}" \
        "3.11:${PYTHON_311_VERSION}" \
        "3.12:${PYTHON_312_VERSION}" \
        "3.13:${PYTHON_313_VERSION}"; do \
        py="${spec%%:*}"; \
        version="${spec#*:}"; \
        uv venv "/opt/venv-py${py}" --python "${version}" --seed; \
        ln -sfn \
            "uv-python/cpython-${version}-linux-x86_64-gnu/bin/python${py}" \
            "/opt/python${py}"; \
    done; \
    rm -rf "${UV_CACHE_DIR}"

FROM python-runtime-base AS runtime-rootfs

ARG OPEN_YR_VERSION=0.9.3
ARG FASTAPI_VERSION=0.138.0
ARG PYDANTIC_VERSION=2.13.4
ARG UVICORN_VERSION=0.49.0
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PYTHON_310_VERSION
ARG PYTHON_311_VERSION
ARG PYTHON_312_VERSION
ARG PYTHON_313_VERSION

RUN mkdir -p /var/task/code /__yuanrong && \
    ln -sfn /home /__yuanrong/home && \
    ln -sfn /usr /__yuanrong/usr && \
    ln -sfn /opt /__yuanrong/opt && \
    ln -sfn /root /__yuanrong/root

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends tini; \
    rm -rf /var/lib/apt/lists/*; \
    cp /usr/bin/tini /usr/bin/tini-static; \
    /usr/bin/tini-static --version

RUN set -eux; \
    py="3.10"; version="${PYTHON_310_VERSION}"; \
    attempt=1; \
    while true; do \
        uv pip install \
            --no-cache-dir \
            --index-url "${PIP_INDEX_URL}" \
            --python "/opt/venv-py${py}/bin/python" \
            "fastapi==${FASTAPI_VERSION}" \
            "pydantic==${PYDANTIC_VERSION}" \
            "uvicorn==${UVICORN_VERSION}" \
            "openyuanrong_sdk==${OPEN_YR_VERSION}" && break; \
        if [ "${attempt}" -ge 3 ]; then exit 1; fi; \
        sleep $((attempt * 5)); \
        attempt=$((attempt + 1)); \
    done; \
    ln -sfn \
        "uv-python/cpython-${version}-linux-x86_64-gnu/bin/python${py}" \
        "/opt/python${py}"; \
    rm -rf "${UV_CACHE_DIR:-/tmp/uv-cache}"

RUN set -eux; \
    py="3.11"; version="${PYTHON_311_VERSION}"; \
    attempt=1; \
    while true; do \
        uv pip install \
            --no-cache-dir \
            --index-url "${PIP_INDEX_URL}" \
            --python "/opt/venv-py${py}/bin/python" \
            "fastapi==${FASTAPI_VERSION}" \
            "pydantic==${PYDANTIC_VERSION}" \
            "uvicorn==${UVICORN_VERSION}" \
            "openyuanrong_sdk==${OPEN_YR_VERSION}" && break; \
        if [ "${attempt}" -ge 3 ]; then exit 1; fi; \
        sleep $((attempt * 5)); \
        attempt=$((attempt + 1)); \
    done; \
    ln -sfn \
        "uv-python/cpython-${version}-linux-x86_64-gnu/bin/python${py}" \
        "/opt/python${py}"; \
    rm -rf "${UV_CACHE_DIR:-/tmp/uv-cache}"

RUN set -eux; \
    py="3.12"; version="${PYTHON_312_VERSION}"; \
    attempt=1; \
    while true; do \
        uv pip install \
            --no-cache-dir \
            --index-url "${PIP_INDEX_URL}" \
            --python "/opt/venv-py${py}/bin/python" \
            "fastapi==${FASTAPI_VERSION}" \
            "pydantic==${PYDANTIC_VERSION}" \
            "uvicorn==${UVICORN_VERSION}" \
            "openyuanrong_sdk==${OPEN_YR_VERSION}" && break; \
        if [ "${attempt}" -ge 3 ]; then exit 1; fi; \
        sleep $((attempt * 5)); \
        attempt=$((attempt + 1)); \
    done; \
    ln -sfn \
        "uv-python/cpython-${version}-linux-x86_64-gnu/bin/python${py}" \
        "/opt/python${py}"; \
    rm -rf "${UV_CACHE_DIR:-/tmp/uv-cache}"

RUN set -eux; \
    py="3.13"; version="${PYTHON_313_VERSION}"; \
    attempt=1; \
    while true; do \
        uv pip install \
            --no-cache-dir \
            --index-url "${PIP_INDEX_URL}" \
            --python "/opt/venv-py${py}/bin/python" \
            "fastapi==${FASTAPI_VERSION}" \
            "pydantic==${PYDANTIC_VERSION}" \
            "uvicorn==${UVICORN_VERSION}" \
            "openyuanrong_sdk==${OPEN_YR_VERSION}" && break; \
        if [ "${attempt}" -ge 3 ]; then exit 1; fi; \
        sleep $((attempt * 5)); \
        attempt=$((attempt + 1)); \
    done; \
    ln -sfn \
        "uv-python/cpython-${version}-linux-x86_64-gnu/bin/python${py}" \
        "/opt/python${py}"; \
    rm -rf "${UV_CACHE_DIR:-/tmp/uv-cache}"

COPY ./builder/scripts/entryfile.sh /home/entryfile.sh
RUN set -eux; \
    chmod 0755 /home/entryfile.sh; \
    for py in 3.10 3.11 3.12 3.13; do \
        "/opt/venv-py${py}/bin/python" -m compileall -q \
            "/opt/venv-py${py}/lib/python${py}/site-packages"; \
    done

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS erofs-builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends erofs-utils && \
    rm -rf /var/lib/apt/lists/*

COPY --from=runtime-rootfs / /rootfs
RUN mkfs.erofs -E noinline_data /yr-runtime-rootfs.img /rootfs && \
    fsck.erofs /yr-runtime-rootfs.img

FROM scratch
COPY --from=erofs-builder /yr-runtime-rootfs.img /yr-runtime-rootfs.img
