#!/bin/sh
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

# Refresh all indexes again when a repository is temporarily inconsistent.
set -eu

attempt=1
while :; do
    if apt-get -o APT::Update::Error-Mode=any \
        -o Acquire::http::No-Cache=true \
        -o Acquire::https::No-Cache=true update; then
        exit 0
    else
        status=$?
    fi
    if [ "${attempt}" -ge 5 ]; then
        echo "APT index refresh failed after ${attempt} attempts" >&2
        exit "${status}"
    fi
    delay=$((attempt * 5))
    echo "APT index refresh failed; retrying in ${delay}s (${attempt}/5)" >&2
    sleep "${delay}"
    attempt=$((attempt + 1))
done
