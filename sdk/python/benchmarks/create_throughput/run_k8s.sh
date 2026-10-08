#!/usr/bin/env bash
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
namespace=akernel
deployment=akernel-adx-ingress-api
service=akernel-adx-ingress-api
secret=akernel-adx-tls
secret_key=admin-key
target_node=
runtime=runsc
batch_size=32
concurrency=1,2,4,8,16
cpu=100
memory=128
cpu_limit=0
memory_limit=0
timeout_seconds=120
cleanup_concurrency=16
cooldown_seconds=5
load_cpu=4
kubeconfig=${KUBECONFIG:-}
output_dir="${repo_root}/out/performance/create-throughput-$(date -u +%Y%m%dT%H%M%SZ)"
keep=0

usage() {
  cat <<'EOF'
Usage: run_k8s.sh --kubeconfig PATH [options]

Measures Create request-to-running throughput in an in-cluster Job. Direct
route verification and deletion happen after the measured interval.

Options:
  --namespace NAME              Kubernetes namespace (default: akernel)
  --deployment NAME             Ingress/API Server deployment
  --service NAME                Ingress/API Server service
  --secret NAME                 Secret containing the API key
  --secret-key KEY              API key field (default: admin-key)
  --target-node NODE            Hard-pin all Sandboxes; omit for automatic placement
  --runtime NAME                Sandbox runtime (default: runsc)
  --batch-size N                Creates per concurrency phase (default: 32)
  --concurrency CSV             Closed-loop concurrency matrix
  --cpu MILLICORES              CPU reservation per Sandbox (default: 100)
  --memory MIB                  Memory reservation per Sandbox (default: 128)
  --cpu-limit MILLICORES        Runtime CPU limit; 0 uses API default
  --memory-limit MIB            Runtime memory limit; 0 uses API default
  --timeout-seconds N           Per-create timeout (default: 120)
  --cleanup-concurrency N       Parallel route checks and deletes (default: 16)
  --cooldown-seconds N          Delay between phases (default: 5)
  --load-cpu CORES              Load Job CPU request and limit (default: 4)
  --output-dir PATH             Evidence directory
  --keep                        Keep the Job and ConfigMap
EOF
}

while (($#)); do
  case "$1" in
    --kubeconfig) kubeconfig=$2; shift 2 ;;
    --namespace) namespace=$2; shift 2 ;;
    --deployment) deployment=$2; shift 2 ;;
    --service) service=$2; shift 2 ;;
    --secret) secret=$2; shift 2 ;;
    --secret-key) secret_key=$2; shift 2 ;;
    --target-node) target_node=$2; shift 2 ;;
    --runtime) runtime=$2; shift 2 ;;
    --batch-size) batch_size=$2; shift 2 ;;
    --concurrency) concurrency=$2; shift 2 ;;
    --cpu) cpu=$2; shift 2 ;;
    --memory) memory=$2; shift 2 ;;
    --cpu-limit) cpu_limit=$2; shift 2 ;;
    --memory-limit) memory_limit=$2; shift 2 ;;
    --timeout-seconds) timeout_seconds=$2; shift 2 ;;
    --cleanup-concurrency) cleanup_concurrency=$2; shift 2 ;;
    --cooldown-seconds) cooldown_seconds=$2; shift 2 ;;
    --load-cpu) load_cpu=$2; shift 2 ;;
    --output-dir) output_dir=$2; shift 2 ;;
    --keep) keep=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$kubeconfig" || ! -r "$kubeconfig" ]]; then
  echo "--kubeconfig must name a readable file" >&2
  exit 2
fi

kubectl_cmd=(kubectl --kubeconfig "$kubeconfig" -n "$namespace")
mkdir -p "$output_dir"
run_id="adx-create-$(date -u +%H%M%S)-$$"
configmap="${run_id}-client"
job="${run_id}-load"

cleanup() {
  if [[ "$keep" == 0 ]]; then
    "${kubectl_cmd[@]}" delete job "$job" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    "${kubectl_cmd[@]}" delete configmap "$configmap" --ignore-not-found >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

image=$("${kubectl_cmd[@]}" get deployment "$deployment" -o jsonpath='{.spec.template.spec.containers[0].image}')
server_pod=$("${kubectl_cmd[@]}" get pod -l "app=${deployment}" -o jsonpath='{.items[0].metadata.name}')
server_node=$("${kubectl_cmd[@]}" get pod "$server_pod" -o jsonpath='{.spec.nodeName}')
redis_pod=$("${kubectl_cmd[@]}" get pod -l app=akernel-adx-redis -o jsonpath='{.items[0].metadata.name}')
origin="https://${service}.${namespace}.svc.cluster.local:8443"
excluded_nodes="$server_node"
if [[ -n "$target_node" ]]; then
  excluded_nodes="${server_node},${target_node}"
  "${kubectl_cmd[@]}" get node "$target_node" -o yaml >"$output_dir/target-node.yaml"
fi
"${kubectl_cmd[@]}" get deployment "$deployment" -o yaml >"$output_dir/deployment.yaml"
"${kubectl_cmd[@]}" get pod "$server_pod" -o yaml >"$output_dir/server-pod.yaml"

python3 - "$output_dir/manifest.json" "$repo_root" "$namespace" "$image" \
  "$target_node" "$server_node" "$server_pod" "$batch_size" "$concurrency" \
  "$cpu" "$memory" "$cpu_limit" "$memory_limit" "$runtime" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

(output, repo, namespace, image, target_node, server_node, server_pod,
 batch_size, concurrency, cpu, memory, cpu_limit, memory_limit, runtime) = sys.argv[1:]
Path(output).write_text(json.dumps({
    "schema_version": 1,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "repository_revision": subprocess.check_output(
        ["git", "-C", repo, "rev-parse", "HEAD"], text=True
    ).strip(),
    "namespace": namespace,
    "image": image,
    "placement_mode": "pinned" if target_node else "automatic",
    "target_node": target_node or None,
    "server_node": server_node,
    "server_pod": server_pod,
    "batch_size": int(batch_size),
    "concurrency": concurrency,
    "cpu_request_millicores": int(cpu),
    "memory_request_mib": int(memory),
    "cpu_limit_millicores": int(cpu_limit),
    "memory_limit_mib": int(memory_limit),
    "runtime": runtime,
}, indent=2) + "\n")
PY

"${kubectl_cmd[@]}" create configmap "$configmap" \
  --from-file=raw_create.py="$repo_root/sdk/python/benchmarks/create_throughput/raw_create.py" \
  --from-file=raw_direct.py="$repo_root/sdk/python/benchmarks/direct_command/raw_direct.py" \
  --dry-run=client -o yaml | "${kubectl_cmd[@]}" apply -f - >/dev/null

affinity_values=$(python3 - "$excluded_nodes" <<'PY'
import json, sys
print(json.dumps(sys.argv[1].split(',')))
PY
)

cat <<EOF | "${kubectl_cmd[@]}" apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  labels: {app: adx-create-throughput-benchmark}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 3600
  template:
    metadata:
      labels: {app: adx-create-throughput-benchmark}
    spec:
      restartPolicy: Never
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: kubernetes.io/hostname
                    operator: NotIn
                    values: ${affinity_values}
      containers:
        - name: load
          image: ${image}
          imagePullPolicy: IfNotPresent
          command: [/usr/bin/python3]
          args:
            - /bench/raw_create.py
            - --origin
            - ${origin}
            - --target-node
            - "${target_node}"
            - --runtime
            - ${runtime}
            - --batch-size
            - "${batch_size}"
            - --concurrency
            - "${concurrency}"
            - --cpu
            - "${cpu}"
            - --memory
            - "${memory}"
            - --cpu-limit
            - "${cpu_limit}"
            - --memory-limit
            - "${memory_limit}"
            - --timeout-seconds
            - "${timeout_seconds}"
            - --cleanup-concurrency
            - "${cleanup_concurrency}"
            - --cooldown-seconds
            - "${cooldown_seconds}"
          env:
            - name: PYTHONPATH
              value: /bench
            - name: BENCH_TOKEN
              valueFrom:
                secretKeyRef: {name: ${secret}, key: ${secret_key}}
          resources:
            requests: {cpu: "${load_cpu}", memory: 256Mi}
            limits: {cpu: "${load_cpu}", memory: 1Gi}
          volumeMounts:
            - {name: client, mountPath: /bench, readOnly: true}
      volumes:
        - name: client
          configMap: {name: ${configmap}}
EOF

pod=
for _ in $(seq 1 60); do
  pod=$("${kubectl_cmd[@]}" get pod -l "job-name=${job}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [[ -n "$pod" ]] && break
  sleep 1
done
[[ -n "$pod" ]] || { echo "benchmark Job did not create a Pod" >&2; exit 1; }

deadline=$((SECONDS + 3600))
: >"$output_dir/resource-samples.tsv"
while ((SECONDS < deadline)); do
  phase=$("${kubectl_cmd[@]}" get pod "$pod" -o jsonpath='{.status.phase}')
  {
    printf '%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$phase"
    for sampled_pod in "$pod" "$server_pod"; do
      printf 'pod=%s\n' "$sampled_pod"
      "${kubectl_cmd[@]}" top pod "$sampled_pod" --containers 2>&1 || true
    done
  } >>"$output_dir/resource-samples.tsv"
  case "$phase" in Succeeded|Failed) break ;; esac
  sleep 2
done

"${kubectl_cmd[@]}" logs "$pod" >"$output_dir/job.log" 2>&1 || true
"${kubectl_cmd[@]}" get pod "$pod" -o yaml >"$output_dir/load-pod.yaml"

python3 - "$output_dir/job.log" "$output_dir/created-ids.txt" <<'PY'
import json
import sys
from pathlib import Path

log_path, output_path = map(Path, sys.argv[1:3])
prefix = "ADX_CREATE_THROUGHPUT_RESULT="
lines = [line for line in log_path.read_text(errors="replace").splitlines() if line.startswith(prefix)]
if not lines:
    raise SystemExit("benchmark log has no result marker")
summary = json.loads(lines[-1][len(prefix):])
ids = [
    created["sandbox_id"]
    for phase in summary.get("phases", [])
    for created in phase.get("created", [])
]
output_path.write_text("".join(f"{sandbox_id}\n" for sandbox_id in ids))
PY

redis_fields=()
while IFS= read -r sandbox_id; do
  [[ -n "$sandbox_id" ]] || continue
  redis_fields+=("environment:${sandbox_id}")
done <"$output_dir/created-ids.txt"
if ((${#redis_fields[@]})); then
  "${kubectl_cmd[@]}" exec "$redis_pod" -- sh -lc \
    'REDISCLI_AUTH="$REDIS_PASSWORD" exec /opt/adx/current/bin/redis-cli --no-auth-warning --json HMGET '"'"'adx:{akernel}:control:v1'"'"' "$@"' \
    sh "${redis_fields[@]}" >"$output_dir/redis-after.json"
else
  printf '[]\n' >"$output_dir/redis-after.json"
fi

python3 - "$output_dir/job.log" "$output_dir/summary.json" \
  "$output_dir/redis-after.json" "$output_dir/created-ids.txt" "$target_node" <<'PY'
import json
import sys
from pathlib import Path

log_path, summary_path, redis_path, ids_path = map(Path, sys.argv[1:5])
target_node = sys.argv[5]
prefix = "ADX_CREATE_THROUGHPUT_RESULT="
lines = [line for line in log_path.read_text(errors="replace").splitlines() if line.startswith(prefix)]
if not lines:
    raise SystemExit("benchmark log has no result marker")
summary = json.loads(lines[-1][len(prefix):])
values = json.loads(redis_path.read_text())
ids = ids_path.read_text().splitlines()
if not isinstance(values, list) or len(values) != len(ids):
    raise SystemExit("Redis HMGET result does not match the created Environment list")
records = dict(zip(("environment:" + sandbox_id for sandbox_id in ids), values))
errors = []
placements = {}
for phase in summary.get("phases", []):
    for created in phase.get("created", []):
        sandbox_id = created["sandbox_id"]
        value = records.get("environment:" + sandbox_id)
        if value is None:
            errors.append(f"{sandbox_id}: no persisted record")
            continue
        record = json.loads(value)
        assignment = record.get("assignment") or {}
        result = record.get("result") or {}
        node_id = str(assignment.get("node_id") or "")
        held = bool(result.get("resources_held"))
        placements[sandbox_id] = {"node_id": node_id, "resources_held": held}
        if target_node and node_id != target_node:
            errors.append(f"{sandbox_id}: assigned to {node_id}, expected {target_node}")
        if held:
            errors.append(f"{sandbox_id}: still holds resources")
summary["storage_audit"] = {
    "status": "passed" if not errors else "failed",
    "records": placements,
    "errors": errors,
}
if errors:
    summary["status"] = "failed"
summary_path.write_text(json.dumps(summary, indent=2) + "\n")
PY

python3 - "$output_dir/summary.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
print(json.dumps({
    "status": value["status"],
    "phases": [{
        "concurrency": phase["concurrency"],
        "creates_per_second": phase["creates_per_second"],
        "p99_ms": phase["create_latency_ms"]["p99"],
        "failed": phase["failed"],
        "route_errors": len(phase["route_errors"]),
        "cleanup_errors": len(phase["cleanup_errors"]),
        "node_counts": phase["node_counts"],
    } for phase in value.get("phases", [])],
    "fatal_error": value.get("fatal_error"),
    "residue": value.get("residue", []),
    "storage_audit": value.get("storage_audit"),
}, indent=2))
raise SystemExit(0 if value["status"] == "passed" else 1)
PY
