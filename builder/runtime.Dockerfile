# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

ARG AKERNEL_RUNTIME_BASE_IMAGE=ubuntu:24.04
ARG UV_VERSION=0.11.32
ARG PYTHON_310_VERSION=3.10.18
ARG PYTHON_311_VERSION=3.11.13
ARG PYTHON_312_VERSION=3.12.11
ARG PYTHON_313_VERSION=3.13.5
ARG PYTHON_314_VERSION=3.14.6
ARG OPEN_YR_VERSION=0.9.9

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS rrt-download

ARG OPEN_YR_VERSION
ARG RRT_RUNTIME_URL=https://github.com/openYuanrong-mirror/yuanrong/releases/download/${OPEN_YR_VERSION}/rrt-runtime-amd64
ARG RRT_RUNTIME_SHA256=d2e5a43cd1d384e0c58174821fc787e7946fb330b23021769a823625a2401274

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fSL --retry 5 --retry-delay 2 --retry-all-errors \
        -o /rrt-runtime "${RRT_RUNTIME_URL}"; \
    echo "${RRT_RUNTIME_SHA256}  /rrt-runtime" | sha256sum -c -; \
    chmod 0755 /rrt-runtime

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS rrt-runtime-rootfs

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

RUN mkdir -p /var/task/code /__yuanrong && \
    ln -sfn /home /__yuanrong/home && \
    ln -sfn /usr /__yuanrong/usr && \
    ln -sfn /opt /__yuanrong/opt && \
    ln -sfn /root /__yuanrong/root

COPY --from=rrt-download /rrt-runtime /usr/local/bin/rrt-runtime

FROM ${AKERNEL_RUNTIME_BASE_IMAGE} AS erofs-builder-base

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends erofs-utils && \
    rm -rf /var/lib/apt/lists/*

FROM erofs-builder-base AS rrt-erofs-builder

COPY --from=rrt-runtime-rootfs / /rootfs
RUN mkfs.erofs -E noinline_data /yr-runtime-rootfs.img /rootfs && \
    fsck.erofs /yr-runtime-rootfs.img

FROM scratch AS runtime-rrt
COPY --from=rrt-erofs-builder /yr-runtime-rootfs.img /yr-runtime-rootfs.img
LABEL org.akernel.runtime.profile="rrt"

FROM rrt-runtime-rootfs AS python-runtime-rootfs

ARG UV_VERSION
ARG PYTHON_310_VERSION
ARG PYTHON_311_VERSION
ARG PYTHON_312_VERSION
ARG PYTHON_313_VERSION
ARG PYTHON_314_VERSION
ARG OPEN_YR_VERSION
ARG FASTAPI_VERSION=0.138.0
ARG PYDANTIC_VERSION=2.13.4
ARG UVICORN_VERSION=0.49.0
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV UV_CACHE_DIR=/tmp/uv-cache \
    UV_HTTP_TIMEOUT=120 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    LD_LIBRARY_PATH=/usr/local/lib \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
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
        --index-url "${PIP_INDEX_URL}" \
        --timeout 120 \
        --retries 10 \
        "uv==${UV_VERSION}"

RUN uv python install \
        "${PYTHON_310_VERSION}" \
        "${PYTHON_311_VERSION}" \
        "${PYTHON_312_VERSION}" \
        "${PYTHON_313_VERSION}" \
        "${PYTHON_314_VERSION}"; \
    rm -rf "${UV_CACHE_DIR}"

RUN set -eux; \
    for spec in \
        "3.10:${PYTHON_310_VERSION}" \
        "3.11:${PYTHON_311_VERSION}" \
        "3.12:${PYTHON_312_VERSION}" \
        "3.13:${PYTHON_313_VERSION}" \
        "3.14:${PYTHON_314_VERSION}"; do \
        py="${spec%%:*}"; \
        version="${spec#*:}"; \
        uv venv "/opt/venv-py${py}" --python "${version}" --seed; \
        ln -sfn \
            "uv-python/cpython-${version}-linux-x86_64-gnu/bin/python${py}" \
            "/opt/python${py}"; \
    done; \
    rm -rf "${UV_CACHE_DIR}"

RUN set -eux; \
    for py in 3.10 3.11 3.12 3.13 3.14; do \
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
    done; \
    rm -rf "${UV_CACHE_DIR}"

COPY ./builder/scripts/entryfile.sh /home/entryfile.sh
RUN set -eux; \
    chmod 0755 /home/entryfile.sh; \
    for py in 3.10 3.11 3.12 3.13 3.14; do \
        "/opt/venv-py${py}/bin/python" -m compileall -q \
            "/opt/venv-py${py}/lib/python${py}/site-packages"; \
    done

FROM erofs-builder-base AS python-erofs-builder

COPY --from=python-runtime-rootfs / /rootfs
RUN mkfs.erofs -E noinline_data /yr-runtime-rootfs.img /rootfs && \
    fsck.erofs /yr-runtime-rootfs.img

FROM scratch AS runtime-python
COPY --from=python-erofs-builder /yr-runtime-rootfs.img /yr-runtime-rootfs.img
LABEL org.akernel.runtime.profile="python"
