#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/deploy/scripts/common.sh"

vendor="aliyun"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vendor)
      vendor="$2"
      shift 2
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done
vendor="$(normalize_vendor "${vendor}")"

require_cmd bash python3 docker terraform helm kubectl
vendor_dir "${vendor}" >/dev/null

info "required tools are available"

for source_file in \
  "${AKERNEL_REPO_ROOT}/src/sandboxd/go.mod" \
  "${AKERNEL_REPO_ROOT}/src/sandboxd/version/VERSION" \
  "${AKERNEL_REPO_ROOT}/src/sandboxd/third_party/runtime-versions.env"; do
  if [[ ! -f "${source_file}" ]]; then
    die "missing submodule source ${source_file}; run git submodule update --init src/sandboxd"
  fi
done

for source_file in \
  "${AKERNEL_REPO_ROOT}/builder/distill-fs-versions.env" \
  "${AKERNEL_REPO_ROOT}/builder/scripts/install-distill-fs.sh"; do
  if [[ ! -f "${source_file}" ]]; then
    die "missing distill-fs release build input ${source_file}; restore it from the AKernel checkout"
  fi
done

info "runtime sources and release build inputs are available"
