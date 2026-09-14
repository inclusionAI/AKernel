{{/* Merge optional telemetry into the shared collector config for each role. */}}
{{- define "core.otelCollectorConfig" -}}
{{- $root := .root -}}
{{- $config := $root.Files.Get "files/otel-collector/config.yaml" | fromYaml -}}
{{- $process := $root.Values.monitoring.processMetrics | default dict -}}
{{- $gateway := $root.Values.monitoring.dataPlaneMetrics | default dict -}}
{{- if or (get $process "enabled") (get $gateway "enabled") -}}
{{- $_ := required "monitoring.prometheusEndpoint is required for process/data-plane metrics" $root.Values.monitoring.prometheusEndpoint -}}
{{- end -}}
{{- if get $process "enabled" -}}
{{- $extra := $root.Files.Get "files/otel-collector/process-metrics.yaml" | fromYaml -}}
{{- $_ := set (index $extra.receivers "hostmetrics/process") "collection_interval" (get $process "interval" | default "15s") -}}
{{- $config = mergeOverwrite $config $extra -}}
{{- end -}}
{{- if and (get $gateway "enabled") $root.Values.dataPlane.enabled (or (eq .role "frontend") (eq .role "node")) -}}
{{- $port := $root.Values.dataPlane.edge.healthPort -}}
{{- $component := "edge_frontend" -}}
{{- if eq .role "node" -}}
{{- $port = $root.Values.dataPlane.nodeProxy.healthPort -}}
{{- $component = "node_proxy" -}}
{{- end -}}
{{- $labels := dict "instance" (printf "${env:POD_NAMESPACE:-unknown}/${env:POD_NAME:-unknown}:%v" $port) "component" $component "namespace" "${env:POD_NAMESPACE:-unknown}" "pod" "${env:POD_NAME:-unknown}" "node" "${env:NODE_NAME:-unknown}" -}}
{{- $job := dict "job_name" (printf "data-plane-%s" .role) "scrape_interval" (get $gateway "interval" | default "5s") "static_configs" (list (dict "targets" (list (printf "127.0.0.1:%v" $port)) "labels" $labels)) -}}
{{- $_ := set $config.receivers "prometheus/data_plane" (dict "config" (dict "scrape_configs" (list $job))) -}}
{{- $_ := set $config.service.pipelines "metrics/data_plane" (dict "receivers" (list "prometheus/data_plane") "processors" (list "memory_limiter" "resource" "batch") "exporters" (list "prometheusremotewrite")) -}}
{{- end -}}
{{- toYaml $config -}}
{{- end -}}
