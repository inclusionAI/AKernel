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
concurrency=1,4,16,32,64,128
cases=health,resources,instances,scheduling-queue
warmup_seconds=2
duration_seconds=10
timeout_seconds=10
kubeconfig=${KUBECONFIG:-}
output_dir="${repo_root}/out/performance/apiserver-$(date -u +%Y%m%dT%H%M%SZ)"
keep=0

usage() {
  cat <<'EOF'
Usage: run_k8s.sh --kubeconfig PATH [options]

Runs an in-cluster raw HTTP/1.1 throughput sweep against the deployed API Server.
The administrator key is injected from a Kubernetes Secret and is never printed.

Options:
  --namespace NAME          Kubernetes namespace (default: akernel)
  --deployment NAME         Ingress/API Server Deployment
  --service NAME            Ingress/API Server Service
  --secret NAME             Secret containing the benchmark API key
  --secret-key KEY          API key field in Secret (default: admin-key)
  --concurrency CSV         Closed-loop concurrency levels
  --cases CSV               Cases to run (default: all four)
  --warmup-seconds N        Warmup per level (default: 2)
  --duration-seconds N      Measurement per level (default: 10)
  --timeout-seconds N       Per-request timeout (default: 10)
  --output-dir PATH         Evidence directory
  --keep                    Keep ConfigMap and Jobs for inspection
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
    --concurrency) concurrency=$2; shift 2 ;;
    --cases) cases=$2; shift 2 ;;
    --warmup-seconds) warmup_seconds=$2; shift 2 ;;
    --duration-seconds) duration_seconds=$2; shift 2 ;;
    --timeout-seconds) timeout_seconds=$2; shift 2 ;;
    --output-dir) output_dir=$2; shift 2 ;;
    --keep) keep=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$kubeconfig" ]]; then
  echo "--kubeconfig is required" >&2
  exit 2
fi
if [[ ! -r "$kubeconfig" ]]; then
  echo "kubeconfig is not readable: $kubeconfig" >&2
  exit 2
fi

kubectl_cmd=(kubectl --kubeconfig "$kubeconfig" -n "$namespace")
mkdir -p "$output_dir"
run_id="adx-api-perf-$(date -u +%H%M%S)-$$"
configmap="${run_id}-client"
jobs=()

cleanup() {
  if [[ "$keep" == 0 ]]; then
    for job in "${jobs[@]}"; do
      "${kubectl_cmd[@]}" delete job "$job" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    done
    "${kubectl_cmd[@]}" delete configmap "$configmap" --ignore-not-found >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

image=$("${kubectl_cmd[@]}" get deployment "$deployment" \
  -o jsonpath='{.spec.template.spec.containers[0].image}')
server_pod=$("${kubectl_cmd[@]}" get pod -l "app=${deployment}" \
  -o jsonpath='{.items[0].metadata.name}')
server_pod_ip=$("${kubectl_cmd[@]}" get pod "$server_pod" \
  -o jsonpath='{.status.podIP}')
server_node=$("${kubectl_cmd[@]}" get pod "$server_pod" \
  -o jsonpath='{.spec.nodeName}')

python3 - "$output_dir/manifest.json" "$repo_root" "$namespace" "$deployment" \
  "$service" "$server_pod" "$server_node" "$image" "$concurrency" \
  "$warmup_seconds" "$duration_seconds" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

output, repo, namespace, deployment, service, pod, node, image, concurrency, warmup, duration = sys.argv[1:]
revision = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
Path(output).write_text(json.dumps({
    "schema_version": 1,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "repository_revision": revision,
    "namespace": namespace,
    "deployment": deployment,
    "service": service,
    "server_pod": pod,
    "server_node": node,
    "image": image,
    "concurrency": concurrency,
    "warmup_seconds": float(warmup),
    "duration_seconds": float(duration),
}, indent=2) + "\n")
PY

"${kubectl_cmd[@]}" get deployment "$deployment" -o yaml >"$output_dir/deployment.yaml"
"${kubectl_cmd[@]}" get pod "$server_pod" -o yaml >"$output_dir/server-pod.yaml"
"${kubectl_cmd[@]}" create configmap "$configmap" \
  --from-file=raw_http.py="$repo_root/sdk/python/benchmarks/apiserver/raw_http.py" \
  --dry-run=client -o yaml | "${kubectl_cmd[@]}" apply -f - >/dev/null

snapshot_server() {
  local output=$1
  printf 'captured_unix_ns=%s\n' "$(python3 -c 'import time; print(time.time_ns())')" >"$output"
  "${kubectl_cmd[@]}" exec "$server_pod" -- sh -lc '
    if test -f /sys/fs/cgroup/cpu.stat; then
      echo cgroup_version=2
      cat /sys/fs/cgroup/cpu.stat
      printf "memory_current="; cat /sys/fs/cgroup/memory.current
      if test -f /sys/fs/cgroup/memory.peak; then printf "memory_peak="; cat /sys/fs/cgroup/memory.peak; fi
    else
      echo cgroup_version=1
      printf "cpu_usage_ns="; cat /sys/fs/cgroup/cpu/cpuacct.usage
      printf "memory_current="; cat /sys/fs/cgroup/memory/memory.usage_in_bytes
      if test -f /sys/fs/cgroup/memory/memory.max_usage_in_bytes; then printf "memory_peak="; cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes; fi
    fi
  ' >>"$output"
}

run_case() {
  local name=$1 url=$2 auth=$3 expect=$4
  local job="${run_id}-${name}"
  local case_dir="$output_dir/$name"
  jobs+=("$job")
  mkdir -p "$case_dir"
  snapshot_server "$case_dir/server-before.txt"
  cat <<EOF | "${kubectl_cmd[@]}" apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  labels:
    app: adx-apiserver-benchmark
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 900
  template:
    metadata:
      labels:
        app: adx-apiserver-benchmark
    spec:
      restartPolicy: Never
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: ${deployment}
              topologyKey: kubernetes.io/hostname
      containers:
        - name: client
          image: ${image}
          imagePullPolicy: IfNotPresent
          command: [/usr/bin/python3]
          args:
            - /bench/raw_http.py
            - --case
            - ${name}|${url}|${auth}|${expect}
            - --concurrency
            - "${concurrency}"
            - --warmup-seconds
            - "${warmup_seconds}"
            - --duration-seconds
            - "${duration_seconds}"
            - --timeout-seconds
            - "${timeout_seconds}"
            - --token-env
            - BENCH_TOKEN
            - --output
            - /results/summary.json
          env:
            - name: BENCH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ${secret}
                  key: ${secret_key}
          resources:
            requests:
              cpu: "2"
              memory: 256Mi
            limits:
              cpu: "4"
              memory: 1Gi
          volumeMounts:
            - {name: client, mountPath: /bench, readOnly: true}
            - {name: results, mountPath: /results}
      volumes:
        - name: client
          configMap:
            name: ${configmap}
        - name: results
          emptyDir: {}
EOF

  local wait_status=0 deadline=$((SECONDS + 900)) condition
  while ((SECONDS < deadline)); do
    condition=$("${kubectl_cmd[@]}" get job "$job" \
      -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}')
    if grep -q '^Complete=True$' <<<"$condition"; then
      break
    fi
    if grep -q '^Failed=True$' <<<"$condition"; then
      wait_status=1
      break
    fi
    sleep 2
  done
  if ((SECONDS >= deadline)); then
    wait_status=1
  fi
  local client_pod
  client_pod=$("${kubectl_cmd[@]}" get pod -l "job-name=$job" -o jsonpath='{.items[0].metadata.name}')
  "${kubectl_cmd[@]}" logs "$client_pod" >"$case_dir/client.log" 2>&1 || true
  "${kubectl_cmd[@]}" get pod "$client_pod" -o yaml >"$case_dir/client-pod.yaml"
  sed -n 's/^RESULT_JSON=//p' "$case_dir/client.log" >"$case_dir/summary.json"
  snapshot_server "$case_dir/server-after.txt"
  if [[ "$wait_status" != 0 || ! -s "$case_dir/summary.json" ]]; then
    echo "case $name failed; see $case_dir/client.log" >&2
    return 1
  fi
}

control_base="https://${service}.${namespace}.svc.cluster.local:8443"
case_enabled() {
  [[ ",${cases}," == *",$1,"* ]]
}
case_enabled health && run_case health "http://${server_pod_ip}:18080/healthz" none text
case_enabled resources && run_case resources "${control_base}/api/sandbox/v1/resources" bearer json-object
case_enabled instances && run_case instances "${control_base}/api/instances" bearer json-array
case_enabled scheduling-queue && run_case scheduling-queue "${control_base}/global-scheduler/scheduling_queue" bearer json-object

python3 - "$output_dir" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
cases = {}

def snapshot(path):
    result = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result

for path in sorted(root.glob("*/summary.json")):
    value = json.loads(path.read_text())
    before = snapshot(path.parent / "server-before.txt")
    after = snapshot(path.parent / "server-after.txt")
    observed = (int(after["captured_unix_ns"]) - int(before["captured_unix_ns"])) / 1e9
    if "cpu_usage_ns" in before:
        cpu_seconds = (int(after["cpu_usage_ns"]) - int(before["cpu_usage_ns"])) / 1e9
    else:
        cpu_seconds = (int(after["usage_usec"]) - int(before["usage_usec"])) / 1e6
    value["server_resources"] = {
        "observed_window_seconds": round(observed, 3),
        "cpu_seconds": round(cpu_seconds, 3),
        "average_cpu_cores": round(cpu_seconds / observed, 3),
        "memory_before_bytes": int(before["memory_current"]),
        "memory_after_bytes": int(after["memory_current"]),
        "memory_peak_bytes": int(after["memory_peak"]),
    }
    cases[path.parent.name] = value
aggregate = {
    "schema_version": 1,
    "status": "passed" if cases and all(v["status"] == "passed" for v in cases.values()) else "failed",
    "cases": cases,
}
(root / "summary.json").write_text(json.dumps(aggregate, indent=2) + "\n")
print(f"status={aggregate['status']} output={root / 'summary.json'}")
if aggregate["status"] != "passed":
    raise SystemExit(1)
PY
