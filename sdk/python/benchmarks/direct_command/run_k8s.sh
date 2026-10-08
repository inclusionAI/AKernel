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
operation=command
generator_processes=1
sandbox_counts=1,2,4,8,16,24
concurrency_factors=1,2,4,8
max_concurrency=128
warmup_seconds=2
duration_seconds=8
cooldown_seconds=2
timeout_seconds=10
cpu=1000
memory=2048
load_cpu=4
kubeconfig=${KUBECONFIG:-}
output_dir="${repo_root}/out/performance/direct-command-$(date -u +%Y%m%dT%H%M%SZ)"
keep=0

usage() {
  cat <<'EOF'
Usage: run_k8s.sh --kubeconfig PATH --target-node NODE [options]

Creates node-pinned Sandboxes, then measures raw HTTP Direct Command calls.
Sandbox creation and deletion are excluded from reported QPS. The benchmark
always attempts to delete every Sandbox it created and audits Redis for new
records that still hold resources.

Options:
  --namespace NAME              Kubernetes namespace (default: akernel)
  --deployment NAME             Ingress/API Server deployment
  --service NAME                Ingress/API Server service
  --secret NAME                 Secret containing the API key
  --secret-key KEY              API key field (default: admin-key)
  --target-node NODE            Node ID/hostname for all Sandboxes (required)
  --runtime NAME                Sandbox runtime (default: runsc)
  --operation NAME              command or health (default: command)
  --generator-processes N       Independent load processes (default: 1)
  --sandbox-counts CSV          Matrix sizes (default: 1,2,4,8,16,24)
  --concurrency-factors CSV     Total concurrency is N times each factor
  --max-concurrency N           Cap total client concurrency (default: 128)
  --warmup-seconds N            Warmup per phase (default: 2)
  --duration-seconds N          Measurement per phase (default: 8)
  --cooldown-seconds N          Recovery delay after each phase (default: 2)
  --timeout-seconds N           Per-command HTTP timeout (default: 10)
  --cpu MILLICORES              Reserved CPU per Sandbox (default: 1000)
  --memory MIB                  Reserved memory per Sandbox (default: 2048)
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
    --operation) operation=$2; shift 2 ;;
    --generator-processes) generator_processes=$2; shift 2 ;;
    --sandbox-counts) sandbox_counts=$2; shift 2 ;;
    --concurrency-factors) concurrency_factors=$2; shift 2 ;;
    --max-concurrency) max_concurrency=$2; shift 2 ;;
    --warmup-seconds) warmup_seconds=$2; shift 2 ;;
    --duration-seconds) duration_seconds=$2; shift 2 ;;
    --cooldown-seconds) cooldown_seconds=$2; shift 2 ;;
    --timeout-seconds) timeout_seconds=$2; shift 2 ;;
    --cpu) cpu=$2; shift 2 ;;
    --memory) memory=$2; shift 2 ;;
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
if [[ -z "$target_node" ]]; then
  echo "--target-node is required" >&2
  exit 2
fi
if [[ "$operation" != command && "$operation" != health ]]; then
  echo "--operation must be command or health" >&2
  exit 2
fi
if [[ "$generator_processes" -lt 1 ]]; then
  echo "--generator-processes must be positive" >&2
  exit 2
fi
if [[ "$load_cpu" -lt 1 ]]; then
  echo "--load-cpu must be positive" >&2
  exit 2
fi

kubectl_cmd=(kubectl --kubeconfig "$kubeconfig" -n "$namespace")
mkdir -p "$output_dir"
run_id="adx-direct-$(date -u +%H%M%S)-$$"
configmap="${run_id}-client"
job="${run_id}-load"

cleanup() {
  if [[ "$keep" == 0 ]]; then
    "${kubectl_cmd[@]}" delete job "$job" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    "${kubectl_cmd[@]}" delete configmap "$configmap" --ignore-not-found >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

image=$("${kubectl_cmd[@]}" get deployment "$deployment" \
  -o jsonpath='{.spec.template.spec.containers[0].image}')
server_pod=$("${kubectl_cmd[@]}" get pod -l "app=${deployment}" \
  -o jsonpath='{.items[0].metadata.name}')
server_node=$("${kubectl_cmd[@]}" get pod "$server_pod" \
  -o jsonpath='{.spec.nodeName}')
target_pod=$("${kubectl_cmd[@]}" get pod -l app=node \
  --field-selector "spec.nodeName=${target_node}" -o jsonpath='{.items[0].metadata.name}')
redis_pod=$("${kubectl_cmd[@]}" get pod -l app=akernel-adx-redis \
  -o jsonpath='{.items[0].metadata.name}')
origin="https://${service}.${namespace}.svc.cluster.local:8443"

"${kubectl_cmd[@]}" get node "$target_node" -o yaml >"$output_dir/target-node.yaml"
"${kubectl_cmd[@]}" get pod "$target_pod" -o yaml >"$output_dir/target-pod.yaml"
"${kubectl_cmd[@]}" get deployment "$deployment" -o yaml >"$output_dir/deployment.yaml"
"${kubectl_cmd[@]}" get pod "$server_pod" -o yaml >"$output_dir/server-pod.yaml"

python3 - "$output_dir/manifest.json" "$repo_root" "$namespace" "$image" \
  "$target_node" "$target_pod" "$server_node" "$server_pod" "$sandbox_counts" \
  "$concurrency_factors" "$max_concurrency" "$warmup_seconds" "$duration_seconds" \
  "$cooldown_seconds" "$operation" "$generator_processes" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

(output, repo, namespace, image, target_node, target_pod, server_node, server_pod,
 counts, factors, max_concurrency, warmup, duration, cooldown, operation,
 generator_processes) = sys.argv[1:]
Path(output).write_text(json.dumps({
    "schema_version": 1,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "repository_revision": subprocess.check_output(
        ["git", "-C", repo, "rev-parse", "HEAD"], text=True
    ).strip(),
    "namespace": namespace,
    "operation": operation,
    "generator_processes": int(generator_processes),
    "image": image,
    "target_node": target_node,
    "target_pod": target_pod,
    "server_node": server_node,
    "server_pod": server_pod,
    "sandbox_counts": counts,
    "concurrency_factors": factors,
    "max_concurrency": int(max_concurrency),
    "warmup_seconds": float(warmup),
    "duration_seconds": float(duration),
    "cooldown_seconds": float(cooldown),
}, indent=2) + "\n")
PY

redis_snapshot() {
  local output=$1
  "${kubectl_cmd[@]}" exec "$redis_pod" -- sh -lc \
    'REDISCLI_AUTH="$REDIS_PASSWORD" exec /opt/adx/current/bin/redis-cli --no-auth-warning --json HGETALL '"'"'adx:{akernel}:control:v1'"'"'' \
    >"$output"
}

redis_snapshot "$output_dir/redis-before.json"

"${kubectl_cmd[@]}" create configmap "$configmap" \
  --from-file=raw_direct.py="$repo_root/sdk/python/benchmarks/direct_command/raw_direct.py" \
  --dry-run=client -o yaml | "${kubectl_cmd[@]}" apply -f - >/dev/null

cat <<EOF | "${kubectl_cmd[@]}" apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  labels:
    app: adx-direct-command-benchmark
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1800
  template:
    metadata:
      labels:
        app: adx-direct-command-benchmark
    spec:
      restartPolicy: Never
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: kubernetes.io/hostname
                    operator: NotIn
                    values: ["${target_node}", "${server_node}"]
      containers:
        - name: load
          image: ${image}
          imagePullPolicy: IfNotPresent
          command: [/usr/bin/python3]
          args:
            - /bench/raw_direct.py
            - --origin
            - ${origin}
            - --target-node
            - ${target_node}
            - --runtime
            - ${runtime}
            - --operation
            - ${operation}
            - --generator-processes
            - "${generator_processes}"
            - --sandbox-counts
            - "${sandbox_counts}"
            - --concurrency-factors
            - "${concurrency_factors}"
            - --max-concurrency
            - "${max_concurrency}"
            - --warmup-seconds
            - "${warmup_seconds}"
            - --duration-seconds
            - "${duration_seconds}"
            - --cooldown-seconds
            - "${cooldown_seconds}"
            - --timeout-seconds
            - "${timeout_seconds}"
            - --cpu
            - "${cpu}"
            - --memory
            - "${memory}"
          env:
            - name: BENCH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ${secret}
                  key: ${secret_key}
          resources:
            requests: {cpu: "${load_cpu}", memory: 256Mi}
            limits: {cpu: "${load_cpu}", memory: 1Gi}
          volumeMounts:
            - {name: client, mountPath: /bench, readOnly: true}
      volumes:
        - name: client
          configMap:
            name: ${configmap}
EOF

pod=
for _ in $(seq 1 60); do
  pod=$("${kubectl_cmd[@]}" get pod -l "job-name=${job}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [[ -n "$pod" ]] && break
  sleep 1
done
if [[ -z "$pod" ]]; then
  echo "benchmark Job did not create a Pod" >&2
  exit 1
fi

deadline=$((SECONDS + 1800))
: >"$output_dir/resource-samples.tsv"
while ((SECONDS < deadline)); do
  phase=$("${kubectl_cmd[@]}" get pod "$pod" -o jsonpath='{.status.phase}')
  {
    printf '%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$phase"
    for sampled_pod in "$pod" "$server_pod" "$target_pod"; do
      printf 'pod=%s\n' "$sampled_pod"
      "${kubectl_cmd[@]}" top pod "$sampled_pod" --containers 2>&1 || true
    done
  } >>"$output_dir/resource-samples.tsv"
  case "$phase" in
    Succeeded|Failed) break ;;
  esac
  sleep 2
done

"${kubectl_cmd[@]}" logs "$pod" >"$output_dir/job.log" 2>&1 || true
"${kubectl_cmd[@]}" get pod "$pod" -o yaml >"$output_dir/load-pod.yaml"
redis_snapshot "$output_dir/redis-after.json"

python3 - "$output_dir/job.log" "$output_dir/summary.json" \
  "$output_dir/redis-before.json" "$output_dir/redis-after.json" <<'PY'
import json
import sys
from pathlib import Path

log_path, summary_path, before_path, after_path = map(Path, sys.argv[1:])
prefix = "ADX_DIRECT_COMMAND_RESULT="
lines = [line for line in log_path.read_text(errors="replace").splitlines() if line.startswith(prefix)]
if not lines:
    raise SystemExit("benchmark log has no result marker")
summary = json.loads(lines[-1][len(prefix):])

def held(path):
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list) and len(raw) % 2 == 0:
        items = [raw[index:index + 2] for index in range(0, len(raw), 2)]
    else:
        raise ValueError(f"invalid Redis HGETALL result in {path}")
    result = set()
    for field, value in items:
        if not str(field).startswith("environment:"):
            continue
        try:
            record = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            continue
        if (record.get("result") or {}).get("resources_held"):
            result.add(str(field)[len("environment:"):])
    return result

def records(path):
    raw = json.loads(path.read_text())
    return raw if isinstance(raw, dict) else dict(zip(raw[::2], raw[1::2]))

before = held(before_path)
after = held(after_path)
new_held = sorted(after - before)
after_records = records(after_path)
placements = {}
placement_errors = []
for created in summary.get("created", []):
    sandbox_id = created["sandbox_id"]
    raw = after_records.get("environment:" + sandbox_id)
    if raw is None:
        placement_errors.append(f"{sandbox_id}: no persisted record")
        continue
    record = json.loads(raw)
    node_id = str((record.get("assignment") or {}).get("node_id") or "")
    resources_held = bool((record.get("result") or {}).get("resources_held"))
    placements[sandbox_id] = {
        "node_id": node_id,
        "resources_held_after_cleanup": resources_held,
    }
    if node_id != summary["target_node"]:
        placement_errors.append(
            f"{sandbox_id}: assigned to {node_id}, expected {summary['target_node']}"
        )
    if resources_held:
        placement_errors.append(f"{sandbox_id}: still holds resources")
summary["redis_audit"] = {
    "status": "no-new-held" if not new_held else "new-held",
    "held_before": len(before),
    "held_after": len(after),
    "new_held": new_held,
}
summary["placement_audit"] = {
    "status": "passed" if not placement_errors else "failed",
    "records": placements,
    "errors": placement_errors,
}
if new_held or placement_errors:
    summary["status"] = "failed"
summary_path.write_text(json.dumps(summary, indent=2) + "\n")
PY

python3 - "$output_dir/summary.json" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1]))
print(json.dumps({
    "status": value["status"],
    "peaks": value.get("peaks", {}),
    "fatal_error": value.get("fatal_error"),
    "cleanup_errors": value.get("cleanup_errors", []),
    "residue": value.get("residue", []),
    "redis_audit": value.get("redis_audit"),
    "placement_audit": value.get("placement_audit"),
}, indent=2))
raise SystemExit(0 if value["status"] == "passed" else 1)
PY
