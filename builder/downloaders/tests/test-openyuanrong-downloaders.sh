#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CORE_DOWNLOADER="${ROOT}/builder/downloaders/download-openyuanrong-core.sh"
RRT_DOWNLOADER="${ROOT}/builder/downloaders/download-openyuanrong-rrt.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

sha256() {
  sha256sum "$1" | awk '{print $1}'
}

core_name="openyuanrong_core-0.7.0+build237-py3-none-manylinux_2_31_x86_64.whl"
core_source="${TMP}/${core_name}"
printf 'buildkite-237 core wheel\n' >"${core_source}"
core_url="file://${core_source}"
core_url="${core_url/+/%2B}"
core_output="${TMP}/core-output"

OPEN_YR_CORE_WHEEL_URL="${core_url}" \
OPEN_YR_CORE_WHEEL_SHA256="$(sha256 "${core_source}")" \
  "${CORE_DOWNLOADER}" "${core_output}"
cmp "${core_source}" "${core_output}/${core_name}"

bad_core_output="${TMP}/bad-core-output"
mkdir -p "${bad_core_output}"
if OPEN_YR_CORE_WHEEL_URL="${core_url}" \
  OPEN_YR_CORE_WHEEL_SHA256=ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff \
    "${CORE_DOWNLOADER}" "${bad_core_output}" >/dev/null 2>&1; then
  echo "core downloader accepted an invalid checksum" >&2
  exit 1
fi
if find "${bad_core_output}" -mindepth 1 -print -quit | grep -q .; then
  echo "core downloader published a failed download" >&2
  exit 1
fi

rrt_root="${TMP}/rrt-wheel"
rrt_wheel="${TMP}/openyuanrong_rrt-0.7.0+build237-py3-none-manylinux_2_31_x86_64.whl"
mkdir -p "${rrt_root}/openyuanrong_rrt"
printf 'buildkite-237 rrt runtime\n' >"${rrt_root}/openyuanrong_rrt/rrt-runtime"
(
  cd "${rrt_root}"
  python3 -m zipfile -c "${rrt_wheel}" openyuanrong_rrt
)
rrt_output="${TMP}/rrt-runtime"

OPEN_YR_RRT_WHEEL_URL="file://${rrt_wheel}" \
OPEN_YR_RRT_WHEEL_SHA256="$(sha256 "${rrt_wheel}")" \
  "${RRT_DOWNLOADER}" "${rrt_output}"
cmp "${rrt_root}/openyuanrong_rrt/rrt-runtime" "${rrt_output}"

if OPEN_YR_RRT_WHEEL_URL="file://${rrt_wheel}" \
  OPEN_YR_RRT_WHEEL_SHA256='' \
    "${RRT_DOWNLOADER}" "${TMP}/unpaired-rrt" >/dev/null 2>&1; then
  echo "RRT downloader accepted a URL without a checksum" >&2
  exit 1
fi

echo "openYuanRong downloader behavior checks passed"
