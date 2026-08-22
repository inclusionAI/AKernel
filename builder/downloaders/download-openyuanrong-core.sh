#!/usr/bin/env bash

set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 DEST_DIR" >&2
  exit 2
fi

destination_dir="$1"
override_url="${OPEN_YR_CORE_WHEEL_URL:-}"
override_sha256="${OPEN_YR_CORE_WHEEL_SHA256:-}"

if [[ -n "${override_url}" && -z "${override_sha256}" ]] || \
   [[ -z "${override_url}" && -n "${override_sha256}" ]]; then
  echo "OPEN_YR_CORE_WHEEL_URL and OPEN_YR_CORE_WHEEL_SHA256 must be set together" >&2
  exit 1
fi

if [[ -n "${override_url}" ]]; then
  wheel_url="${override_url}"
  wheel_sha256="${override_sha256}"
  wheel_name="$(python3 -c '
import os
import sys
import urllib.parse

print(os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(sys.argv[1]).path)))
' "${wheel_url}")"
else
  case "${TARGETARCH:-}" in
    amd64)
      wheel_arch=x86_64
      wheel_sha256="${OPEN_YR_CORE_AMD64_SHA256:?OPEN_YR_CORE_AMD64_SHA256 is required}"
      ;;
    arm64)
      wheel_arch=aarch64
      wheel_sha256="${OPEN_YR_CORE_ARM64_SHA256:?OPEN_YR_CORE_ARM64_SHA256 is required}"
      ;;
    "")
      case "$(uname -m)" in
        x86_64)
          wheel_arch=x86_64
          wheel_sha256="${OPEN_YR_CORE_AMD64_SHA256:?OPEN_YR_CORE_AMD64_SHA256 is required}"
          ;;
        aarch64|arm64)
          wheel_arch=aarch64
          wheel_sha256="${OPEN_YR_CORE_ARM64_SHA256:?OPEN_YR_CORE_ARM64_SHA256 is required}"
          ;;
        *)
          echo "unsupported openYuanRong target architecture: $(uname -m)" >&2
          exit 1
          ;;
      esac
      ;;
    *)
      echo "unsupported openYuanRong target architecture: ${TARGETARCH}" >&2
      exit 1
      ;;
  esac

  open_yr_version="${OPEN_YR_VERSION:?OPEN_YR_VERSION is required}"
  release_base_url="${OPEN_YR_RELEASE_BASE_URL:?OPEN_YR_RELEASE_BASE_URL is required}"
  wheel_name="openyuanrong_core-${open_yr_version}-py3-none-manylinux_2_31_${wheel_arch}.whl"
  wheel_url="${release_base_url}/${open_yr_version}/${wheel_name}"
fi

case "${wheel_name}" in
  ?*.whl) ;;
  *)
    echo "openYuanRong core URL must reference a .whl file: ${wheel_url}" >&2
    exit 1
    ;;
esac

mkdir -p "${destination_dir}"
destination="${destination_dir}/${wheel_name}"
[[ ! -e "${destination}" ]] || {
  echo "openYuanRong core destination already exists: ${destination}" >&2
  exit 1
}

temporary_dir="$(mktemp -d)"
trap 'rm -rf "${temporary_dir}"' EXIT
temporary_wheel="${temporary_dir}/${wheel_name}"

curl -fSL --retry 10 --retry-delay 2 --retry-all-errors \
  "${wheel_url}" -o "${temporary_wheel}"
echo "${wheel_sha256}  ${temporary_wheel}" | sha256sum -c -
mv "${temporary_wheel}" "${destination}"
