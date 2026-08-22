#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

core_url='https://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/daily/20260822051203/linux/amd64/openyuanrong_core-0.7.0%2B87cba622b491-py3-none-manylinux_2_31_x86_64.whl'
core_sha='9eb44e1ea59153ab9a65a81fc32450c09376e835732290046d028cec2db3b200'
rrt_url='https://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/daily/20260822042459/linux/amd64/openyuanrong_rrt-0.7.0%2B87cba622b491-py3-none-manylinux_2_31_x86_64.whl'
rrt_sha='3aff1b4a676ca28992a2478adab900bc7bd1e76928cc12016ae50fea412a68c4'

mkdir -p "${TMP}/bin"
cat >"${TMP}/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${DOCKER_LOG}"
EOF
chmod +x "${TMP}/bin/docker"

DOCKER_LOG="${TMP}/docker.log" \
PATH="${TMP}/bin:${PATH}" \
  make -C "${ROOT}" SHELL=/bin/bash build \
    ENV=obs-contract-test \
    IMAGE_REPOSITORY=registry.example.invalid/akernel \
    IMAGE_TAG=build-237

runtime_invocation="$(sed -n '1p' "${TMP}/docker.log")"
node_invocation="$(sed -n '2p' "${TMP}/docker.log")"

for expected in \
  "OPEN_YR_RRT_WHEEL_URL=${rrt_url}" \
  "OPEN_YR_RRT_WHEEL_SHA256=${rrt_sha}"; do
  [[ "${runtime_invocation}" == *"${expected}"* ]] || {
    echo "runtime build is missing ${expected}" >&2
    exit 1
  }
done

for expected in \
  "OPEN_YR_CORE_WHEEL_URL=${core_url}" \
  "OPEN_YR_CORE_WHEEL_SHA256=${core_sha}"; do
  [[ "${node_invocation}" == *"${expected}"* ]] || {
    echo "node build is missing ${expected}" >&2
    exit 1
  }
done

invalid_log="${TMP}/invalid-docker.log"
if DOCKER_LOG="${invalid_log}" PATH="${TMP}/bin:${PATH}" \
  "${ROOT}/deploy/scripts/build-image.sh" \
    --repository registry.example.invalid/akernel \
    --tag invalid \
    --open-yr-rrt-wheel-url "${rrt_url}" \
    >"${TMP}/invalid.out" 2>&1; then
  echo "build accepted an RRT URL without a checksum" >&2
  exit 1
fi
grep -Fq \
  'OPEN_YR_RRT_WHEEL_URL and OPEN_YR_RRT_WHEEL_SHA256 must be set together' \
  "${TMP}/invalid.out"
[[ ! -e "${invalid_log}" ]] || {
  echo "invalid RRT override reached Docker" >&2
  exit 1
}

echo "Buildkite 237 OBS build contract checks passed"
