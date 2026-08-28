#!/usr/bin/env bash

# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
chart="${repo_root}/deploy/akernel"
tmp_dir=$(mktemp -d)
trap 'rm -rf "${tmp_dir}"' EXIT

default_render="${tmp_dir}/default.yaml"
s3_render="${tmp_dir}/s3.yaml"
helm template akernel-snapshot "${chart}" --set monitor.enabled=false >"${default_render}"
if grep -q 'name: AKERNEL_ENABLE_SNAPSHOT' "${default_render}"; then
  echo "Helm still emits the removed snapshot enable switch" >&2
  exit 1
fi
grep -A1 'name: AKERNEL_SNAPSHOT_STORAGE_BACKEND' "${default_render}" | grep -q 'value: "datasystem"'

helm template akernel-snapshot "${chart}" \
  --set monitor.enabled=false \
  --set core.snapshot.storage.backend=s3 \
  --set core.snapshot.storage.s3.provider=generic \
  --set core.snapshot.storage.s3.endpoint=s3.private.example \
  --set core.snapshot.storage.s3.region=us-east-1 \
  --set core.snapshot.storage.s3.bucket=akernel-test \
  --set core.snapshot.storage.s3.existingSecret=akernel-snapshot-s3 \
  --set core.snapshot.storage.s3.pathStyle=true >"${s3_render}"
grep -A1 'name: AKERNEL_SNAPSHOT_S3_PROVIDER' "${s3_render}" | grep -q 'value: "generic"'
grep -A1 'name: AKERNEL_SNAPSHOT_S3_ENDPOINT' "${s3_render}" | grep -q 'value: "s3.private.example"'
grep -A5 'name: AKERNEL_SNAPSHOT_S3_ACCESS_KEY' "${s3_render}" | grep -q 'name: "akernel-snapshot-s3"'

if helm template akernel-snapshot "${chart}" \
  --set monitor.enabled=false \
  --set core.snapshot.storage.backend=obs >/dev/null 2>&1; then
  echo "Helm accepted the removed snapshot OBS backend" >&2
  exit 1
fi

if helm template akernel-snapshot "${chart}" \
  --set monitor.enabled=false \
  --set core.snapshot.storage.backend=s3 \
  --set core.snapshot.storage.s3.provider=unknown \
  --set core.snapshot.storage.s3.endpoint=s3.example \
  --set core.snapshot.storage.s3.bucket=akernel-test \
  --set core.snapshot.storage.s3.existingSecret=akernel-snapshot-s3 >/dev/null 2>&1; then
  echo "Helm accepted an unknown S3 provider" >&2
  exit 1
fi

if helm template akernel-snapshot "${chart}" \
  --set monitor.enabled=false \
  --set core.snapshot.storage.backend=s3 \
  --set core.snapshot.storage.s3.provider=oss \
  --set core.snapshot.storage.s3.endpoint=oss-cname.example \
  --set core.snapshot.storage.s3.region=cn-hangzhou \
  --set core.snapshot.storage.s3.bucket=akernel-test \
  --set core.snapshot.storage.s3.existingSecret=akernel-snapshot-s3 \
  --set core.snapshot.storage.s3.pathStyle=true >/dev/null 2>&1; then
  echo "Helm accepted path-style addressing for OSS" >&2
  exit 1
fi

echo "Kubernetes snapshot wiring contract passed"
