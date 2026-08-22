#!/usr/bin/env bash

set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 DEST_FILE" >&2
  exit 2
fi

destination="$1"
override_url="${OPEN_YR_RRT_WHEEL_URL:-}"
override_sha256="${OPEN_YR_RRT_WHEEL_SHA256:-}"

if [[ -n "${override_url}" && -z "${override_sha256}" ]] || \
   [[ -z "${override_url}" && -n "${override_sha256}" ]]; then
  echo "OPEN_YR_RRT_WHEEL_URL and OPEN_YR_RRT_WHEEL_SHA256 must be set together" >&2
  exit 1
fi

destination_dir="$(dirname "${destination}")"
mkdir -p "${destination_dir}"
[[ ! -d "${destination}" ]] || {
  echo "RRT destination must be a file path: ${destination}" >&2
  exit 1
}

temporary_dir="$(mktemp -d "${destination_dir}/.openyuanrong-rrt.XXXXXX")"
trap 'rm -rf "${temporary_dir}"' EXIT
candidate="${temporary_dir}/rrt-runtime"

if [[ -n "${override_url}" ]]; then
  wheel="${temporary_dir}/openyuanrong-rrt.whl"
  curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
    "${override_url}" -o "${wheel}"
  echo "${override_sha256}  ${wheel}" | sha256sum -c -
  unzip -p "${wheel}" openyuanrong_rrt/rrt-runtime >"${candidate}"
else
  runtime_url="${RRT_RUNTIME_URL:?RRT_RUNTIME_URL is required}"
  runtime_sha256="${RRT_RUNTIME_SHA256:?RRT_RUNTIME_SHA256 is required}"
  curl -fSL --retry 5 --retry-delay 2 --retry-all-errors \
    "${runtime_url}" -o "${candidate}"
  echo "${runtime_sha256}  ${candidate}" | sha256sum -c -
fi

[[ -s "${candidate}" ]] || {
  echo "downloaded RRT runtime is empty" >&2
  exit 1
}
mv "${candidate}" "${destination}"
