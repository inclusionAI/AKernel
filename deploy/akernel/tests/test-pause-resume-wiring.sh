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
s3_default_addressing_render="${tmp_dir}/s3-default-addressing.yaml"
s3_distributed_only_render="${tmp_dir}/s3-distributed-only.yaml"
helm template akernel-snapshot "${chart}" --set monitor.enabled=false >"${default_render}"
if grep -q 'name: AKERNEL_ENABLE_SNAPSHOT' "${default_render}"; then
  echo "Helm still emits the removed snapshot enable switch" >&2
  exit 1
fi
grep -A1 'name: AKERNEL_SNAPSHOT_STORAGE_BACKEND' "${default_render}" | grep -q 'value: "datasystem"'
if grep -Eq 'name: AKERNEL_SNAPSHOT_STORAGE_MODE|name: AKERNEL_SNAPSHOT_S3_' "${default_render}"; then
  echo "default DataSystem Helm render contains S3-only environment" >&2
  exit 1
fi

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
grep -A1 'name: AKERNEL_SNAPSHOT_STORAGE_MODE' "${s3_render}" \
  | grep -q 'value: "distributed_cache"'
grep -A1 'name: AKERNEL_SNAPSHOT_S3_ENDPOINT' "${s3_render}" | grep -q 'value: "s3.private.example"'
grep -A1 'name: AKERNEL_SNAPSHOT_S3_REGION' "${s3_render}" | grep -q 'value: "us-east-1"'
grep -A1 'name: AKERNEL_SNAPSHOT_S3_BUCKET' "${s3_render}" | grep -q 'value: "akernel-test"'
grep -A1 'name: AKERNEL_SNAPSHOT_S3_USE_HTTPS' "${s3_render}" | grep -q 'value: "true"'
grep -A1 'name: AKERNEL_SNAPSHOT_S3_PATH_STYLE' "${s3_render}" | grep -q 'value: "true"'
for credential in ACCESS_KEY SECRET_KEY SECURITY_TOKEN; do
  grep -A5 "name: AKERNEL_SNAPSHOT_S3_${credential}" "${s3_render}" \
    | grep -q 'name: "akernel-snapshot-s3"'
done

helm template akernel-snapshot "${chart}" \
  --set monitor.enabled=false \
  --set core.snapshot.storage.backend=s3 \
  --set core.snapshot.storage.s3.provider=generic \
  --set core.snapshot.storage.s3.endpoint=s3.private.example \
  --set core.snapshot.storage.s3.region=us-east-1 \
  --set core.snapshot.storage.s3.bucket=akernel-test \
  --set core.snapshot.storage.s3.existingSecret=akernel-snapshot-s3 \
  >"${s3_default_addressing_render}"
grep -A1 'name: AKERNEL_SNAPSHOT_S3_PATH_STYLE' "${s3_default_addressing_render}" \
  | grep -q 'value: "false"'

helm template akernel-snapshot "${chart}" \
  --set monitor.enabled=false \
  --set core.snapshot.storage.backend=s3 \
  --set core.snapshot.storage.mode=distributed_only \
  --set core.snapshot.storage.s3.provider=generic \
  --set core.snapshot.storage.s3.endpoint=s3.private.example \
  --set core.snapshot.storage.s3.region=us-east-1 \
  --set core.snapshot.storage.s3.bucket=akernel-test \
  --set core.snapshot.storage.s3.existingSecret=akernel-snapshot-s3 \
  >"${s3_distributed_only_render}"
grep -A1 'name: AKERNEL_SNAPSHOT_STORAGE_MODE' "${s3_distributed_only_render}" \
  | grep -q 'value: "distributed_only"'

if helm template akernel-snapshot "${chart}" \
  --set monitor.enabled=false \
  --set core.snapshot.storage.backend=obs >/dev/null 2>&1; then
  echo "Helm accepted the removed snapshot OBS backend" >&2
  exit 1
fi

for boolean_field in useHttps pathStyle; do
  if helm template akernel-snapshot "${chart}" \
    --set monitor.enabled=false \
    --set core.snapshot.storage.backend=s3 \
    --set core.snapshot.storage.s3.provider=generic \
    --set core.snapshot.storage.s3.endpoint=s3.example \
    --set core.snapshot.storage.s3.region=us-east-1 \
    --set core.snapshot.storage.s3.bucket=akernel-test \
    --set core.snapshot.storage.s3.existingSecret=akernel-snapshot-s3 \
    --set-string "core.snapshot.storage.s3.${boolean_field}=not-a-boolean" >/dev/null 2>&1; then
    echo "Helm accepted non-boolean S3 ${boolean_field}" >&2
    exit 1
  fi
done

for storage_mode in local_only invalid; do
  if helm template akernel-snapshot "${chart}" \
    --set monitor.enabled=false \
    --set core.snapshot.storage.backend=s3 \
    --set core.snapshot.storage.mode="${storage_mode}" \
    --set core.snapshot.storage.s3.provider=generic \
    --set core.snapshot.storage.s3.endpoint=s3.example \
    --set core.snapshot.storage.s3.region=us-east-1 \
    --set core.snapshot.storage.s3.bucket=akernel-test \
    --set core.snapshot.storage.s3.existingSecret=akernel-snapshot-s3 >/dev/null 2>&1; then
    echo "Helm accepted S3 storage mode ${storage_mode}" >&2
    exit 1
  fi
done

for secret_key_field in accessKeyKey secretKeyKey securityTokenKey; do
  if helm template akernel-snapshot "${chart}" \
    --set monitor.enabled=false \
    --set core.snapshot.storage.backend=s3 \
    --set core.snapshot.storage.s3.provider=generic \
    --set core.snapshot.storage.s3.endpoint=s3.example \
    --set core.snapshot.storage.s3.region=us-east-1 \
    --set core.snapshot.storage.s3.bucket=akernel-test \
    --set core.snapshot.storage.s3.existingSecret=akernel-snapshot-s3 \
    --set-string "core.snapshot.storage.s3.${secret_key_field}=" >/dev/null 2>&1; then
    echo "Helm accepted empty S3 ${secret_key_field}" >&2
    exit 1
  fi
done

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
