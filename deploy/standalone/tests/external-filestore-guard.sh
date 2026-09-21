#!/bin/bash

# Copyright (c) 2026 Ant Group Corporation.
#
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../../.." && pwd)"
guard="${repo_root}/builder/scripts/verify-external-filestore.sh"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT

fake_bin="${test_root}/bin"
target="${test_root}/filestore"
state_dir="${test_root}/state"
mkdir -p "${fake_bin}" "${target}" "${state_dir}"

cat > "${fake_bin}/mountpoint" <<'EOF'
#!/bin/bash
[[ "${FAKE_MOUNTPOINT:-true}" == true ]]
EOF

cat > "${fake_bin}/findmnt" <<'EOF'
#!/bin/bash
case "$*" in
    *"-o SOURCE"*) printf '%s\n' "${FAKE_SOURCE:-/dev/test-disk}" ;;
    *"-o FSTYPE"*) printf '%s\n' "${FAKE_FSTYPE:-ext4}" ;;
    *"-o UUID"*) printf '%s\n' "${FAKE_UUID:-test-uuid}" ;;
    *) exit 2 ;;
esac
EOF
chmod 0755 "${fake_bin}/mountpoint" "${fake_bin}/findmnt"

# Additional NAME=value arguments let individual cases override the fake mount.
# shellcheck disable=SC2120
run_guard() {
    env \
        PATH="${fake_bin}:${PATH}" \
        AKERNEL_EXTERNAL_FILESTORE_ENABLED=true \
        AKERNEL_EXTERNAL_FILESTORE_TARGET="${target}" \
        AKERNEL_EXTERNAL_FILESTORE_SOURCE=/dev/test-disk \
        AKERNEL_EXTERNAL_FILESTORE_FSTYPE=ext4 \
        AKERNEL_EXTERNAL_FILESTORE_UUID=test-uuid \
        "$@" \
        "${guard}"
}

write_state() {
    printf 'true\n' > "${state_dir}/enabled"
    printf '%s\n' "${target}" > "${state_dir}/target"
    printf '/dev/test-disk\n' > "${state_dir}/source"
    printf 'ext4\n' > "${state_dir}/fstype"
    printf 'test-uuid\n' > "${state_dir}/uuid"
}

expect_failure() {
    if "$@" > /dev/null 2>&1; then
        echo "expected command to fail: $*" >&2
        exit 1
    fi
}

# shellcheck disable=SC2119
run_guard
write_state
env PATH="${fake_bin}:${PATH}" \
    AKERNEL_EXTERNAL_FILESTORE_STATE_DIR="${state_dir}" \
    "${guard}"
expect_failure env FAKE_UUID=wrong-uuid PATH="${fake_bin}:${PATH}" \
    AKERNEL_EXTERNAL_FILESTORE_STATE_DIR="${state_dir}" \
    "${guard}"
expect_failure run_guard FAKE_MOUNTPOINT=false
expect_failure run_guard FAKE_UUID=wrong-uuid
expect_failure run_guard FAKE_FSTYPE=xfs
expect_failure run_guard FAKE_SOURCE=/dev/loop0 AKERNEL_EXTERNAL_FILESTORE_UUID=
expect_failure run_guard FAKE_SOURCE=/dev/other AKERNEL_EXTERNAL_FILESTORE_UUID=

touch "${target}/ext4.img"
expect_failure run_guard
rm -f -- "${target}/ext4.img"

env PATH="${fake_bin}:${PATH}" AKERNEL_EXTERNAL_FILESTORE_ENABLED=false "${guard}"
expect_failure env PATH="${fake_bin}:${PATH}" AKERNEL_EXTERNAL_FILESTORE_ENABLED=invalid "${guard}"
expect_failure env PATH="${fake_bin}:${PATH}" \
    AKERNEL_EXTERNAL_FILESTORE_REQUIRED=true \
    AKERNEL_EXTERNAL_FILESTORE_STATE_DIR="${test_root}/missing-state" \
    "${guard}"

printf 'false\n' > "${state_dir}/enabled"
expect_failure env PATH="${fake_bin}:${PATH}" \
    AKERNEL_EXTERNAL_FILESTORE_REQUIRED=true \
    AKERNEL_EXTERNAL_FILESTORE_STATE_DIR="${state_dir}" \
    "${guard}"
write_state

rm -f -- "${state_dir}/uuid"
expect_failure env PATH="${fake_bin}:${PATH}" \
    AKERNEL_EXTERNAL_FILESTORE_STATE_DIR="${state_dir}" \
    "${guard}"

write_state
rm -f -- "${state_dir}/enabled"
expect_failure env PATH="${fake_bin}:${PATH}" \
    AKERNEL_EXTERNAL_FILESTORE_STATE_DIR="${state_dir}" \
    "${guard}"

echo "external filestore guard tests passed"
