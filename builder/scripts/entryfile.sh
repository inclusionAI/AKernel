#!/bin/sh

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

set -eu

RUNTIME_ENV=${YR_LANGUAGE:-python3.12}

case "$(uname -m)" in
    x86_64)
        MULTIARCH=x86_64-linux-gnu
        RUNTIME_LOADER=ld-linux-x86-64.so.2
        ;;
    aarch64|arm64)
        MULTIARCH=aarch64-linux-gnu
        RUNTIME_LOADER=ld-linux-aarch64.so.1
        ;;
    *)
        echo "unsupported runtime architecture: $(uname -m)" >&2
        exit 127
        ;;
esac

case "$RUNTIME_ENV" in
    python3.10)
        PY_VERSION=3.10
        ;;
    python3.11)
        PY_VERSION=3.11
        ;;
    python3.12)
        PY_VERSION=3.12
        ;;
    python3.13)
        PY_VERSION=3.13
        ;;
    *)
        echo "unsupported YR_LANGUAGE=${RUNTIME_ENV}; supported runtimes: python3.10, python3.11, python3.12, python3.13" >&2
        exit 127
        ;;
esac

RUNTIME_PREFIX=${YR_RUNTIME_PREFIX:-}
if [ -z "${RUNTIME_PREFIX}" ]; then
    if [ -x "/opt/python${PY_VERSION}" ]; then
        RUNTIME_PREFIX=""
    elif [ -x "/__yuanrong/opt/python${PY_VERSION}" ]; then
        RUNTIME_PREFIX="/__yuanrong"
    fi
fi

cd "${YR_RT_WORKING_DIR:-/}"

SITE_PACKAGES="${RUNTIME_PREFIX}/opt/venv-py${PY_VERSION}/lib/python${PY_VERSION}/site-packages"
RUNTIME_BIN="${RUNTIME_PREFIX}/opt/python${PY_VERSION}"

if [ ! -x "${RUNTIME_BIN}" ]; then
    RUNTIME_BIN="${RUNTIME_PREFIX}/opt/venv-py${PY_VERSION}/bin/python"
fi

RUNTIME_LIBRARY_PATH="${RUNTIME_PREFIX}/usr/lib/${MULTIARCH}:${RUNTIME_PREFIX}/usr/local/lib:${RUNTIME_PREFIX}/usr/lib64:${RUNTIME_PREFIX}/lib/${MULTIARCH}:${RUNTIME_PREFIX}/lib64:${RUNTIME_PREFIX}/lib"

if [ ! -x "${RUNTIME_BIN}" ]; then
    echo "missing runtime interpreter: ${RUNTIME_BIN}" >&2
    exit 127
fi

if [ ! -f "${SITE_PACKAGES}/yr/main/yr_runtime_main.py" ]; then
    echo "missing openYuanrong runtime entrypoint: ${SITE_PACKAGES}/yr/main/yr_runtime_main.py" >&2
    exit 127
fi

export PYTHONPATH="${SITE_PACKAGES}${PYTHONPATH:+:${PYTHONPATH}}"

if [ -n "${RUNTIME_PREFIX}" ]; then
    RUNTIME_LD=""
    for candidate in \
        "${RUNTIME_PREFIX}/lib/${MULTIARCH}/${RUNTIME_LOADER}" \
        "${RUNTIME_PREFIX}/lib64/${RUNTIME_LOADER}" \
        "${RUNTIME_PREFIX}/lib/${RUNTIME_LOADER}"; do
        if [ -x "${candidate}" ]; then
            RUNTIME_LD="${candidate}"
            break
        fi
    done
    if [ ! -x "${RUNTIME_LD}" ]; then
        echo "missing runtime dynamic linker under ${RUNTIME_PREFIX}" >&2
        exit 127
    fi

    exec "${RUNTIME_LD}" \
        --library-path "${RUNTIME_LIBRARY_PATH}" \
        "${RUNTIME_BIN}" \
        "${SITE_PACKAGES}/yr/main/yr_runtime_main.py" \
        "$@"
fi

export LD_LIBRARY_PATH="${RUNTIME_LIBRARY_PATH}:/usr/lib/${MULTIARCH}:/usr/local/lib:/usr/lib64:/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

exec "${RUNTIME_BIN}" \
    "${SITE_PACKAGES}/yr/main/yr_runtime_main.py" \
    "$@"
