#!/usr/bin/env bash
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
# Explicit integration test: build, exercise the SDK, and stop a fresh profile.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"
command -v uv >/dev/null
case "$(docker info --format '{{.OSType}}/{{.Architecture}}')" in
    linux/amd64|linux/x86_64) native_platform=linux/amd64; client_platform=linux/arm64 ;;
    linux/arm64|linux/aarch64) native_platform=linux/arm64; client_platform=linux/amd64 ;;
    *) echo 'Source integration test requires a Linux amd64 or arm64 Docker daemon' >&2; exit 1 ;;
esac
if [[ -n "$(docker ps -a --filter name='^/akernel-node$' --filter name='^/akernel-traefik$' --format '{{.ID}}')" ]]; then
    echo 'Standalone names are occupied; integration test did not start' >&2
    exit 1
fi
mkdir -p "${ROOT}/.akernel/standalone-tests"
profile="$(mktemp -d "${ROOT}/.akernel/standalone-tests/source.XXXXXX")"
export AKERNEL_STANDALONE_DATA_DIR="${profile}/data"
export AKERNEL_CHUNK_DB_SIZE="${AKERNEL_CHUNK_DB_SIZE:-2GiB}"
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -f "${AKERNEL_STANDALONE_DATA_DIR}/source-state.json" ]]; then
        if ! make standalone-stop; then
            echo "Integration cleanup failed; inspect ${profile}" >&2
            status=1
        fi
    fi
    exit "${status}"
}
trap cleanup EXIT
# Source startup must override a conflicting client platform for both images.
make standalone DOCKER_DEFAULT_PLATFORM="${client_platform}"
for container in akernel-node akernel-traefik; do
    image_id="$(docker inspect --format '{{.Image}}' "${container}")"
    image_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${image_id}")"
    if [[ "${image_platform}" != "${native_platform}" ]]; then
        echo "${container} image platform ${image_platform} differs from daemon ${native_platform}" >&2
        exit 1
    fi
done
make standalone-status
# The SDK gets credentials through environment only, never command arguments.
set +x
# shellcheck source=/dev/null
source "${AKERNEL_STANDALONE_DATA_DIR}/sdk-env.sh"
uv run --isolated --no-project --with './sdk/python' python - <<'PY'
import logging
import sys
logging.disable(sys.maxsize)
from akernel_sdk import Sandbox
try:
    with Sandbox(cpu=1000, memory=1024) as sandbox:
        result = sandbox.commands.run('printf standalone-source-ok')
        assert result.exit_code == 0 and result.stdout == 'standalone-source-ok'
        sandbox.files.write('/tmp/source-test.txt', 'file-round-trip')
        assert sandbox.files.read('/tmp/source-test.txt') == 'file-round-trip'
except Exception:
    print('Source standalone SDK lifecycle failed', file=sys.stderr)
    raise SystemExit(1) from None
print('Source standalone SDK lifecycle passed')
PY
