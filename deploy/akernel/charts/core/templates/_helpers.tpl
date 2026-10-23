{{/*
One pool selector drives all ADX control roles and the worker exclusion.
Use a single stable pool label (including the provider's node-pool ID label).
*/}}
{{- define "core.controlPlacement" -}}
{{- $placement := .Values.adx.controlPlane | default dict -}}
{{- if gt (len ($placement.nodeSelector | default dict)) 1 -}}
{{- fail "adx.controlPlane.nodeSelector must contain at most one pool label" -}}
{{- end -}}
{{- with $placement.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $placement.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{- define "core.workerPlacement" -}}
{{- $affinity := deepCopy (.Values.node.affinity | default dict) -}}
{{- $placement := .Values.adx.controlPlane | default dict -}}
{{- with $placement.nodeSelector -}}
  {{- $node := get $affinity "nodeAffinity" | default dict -}}
  {{- $hasRequired := kindIs "map" (get $node "requiredDuringSchedulingIgnoredDuringExecution") -}}
  {{- $required := get $node "requiredDuringSchedulingIgnoredDuringExecution" | default dict -}}
  {{- $terms := list (dict) -}}
  {{- if $hasRequired -}}
    {{- $terms = get $required "nodeSelectorTerms" | default list -}}
  {{- end -}}
  {{- range $key, $value := . -}}
    {{- range $term := $terms -}}
      {{- if or (not $hasRequired) (get $term "matchExpressions") (get $term "matchFields") -}}
      {{- $expressions := get $term "matchExpressions" | default list -}}
      {{- $_ := set $term "matchExpressions" (append $expressions (dict "key" $key "operator" "NotIn" "values" (list $value))) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- $_ := set $required "nodeSelectorTerms" $terms -}}
  {{- $_ := set $node "requiredDuringSchedulingIgnoredDuringExecution" $required -}}
  {{- $_ := set $affinity "nodeAffinity" $node -}}
{{- end -}}
{{- with $affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/* Expand the name of the chart. */}}
{{- define "core.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}


{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "core.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "core.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "core.labels" -}}
helm.sh/chart: {{ include "core.chart" . }}
{{ include "core.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "core.selectorLabels" -}}
app.kubernetes.io/name: {{ include "core.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "core.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "core.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Resolve component image settings. control/node default to the global
all-in-one image, while each component can still override repository/tag/policy.
*/}}
{{- define "core.image.repository" -}}
{{- $root := .root -}}
{{- $image := .image | default dict -}}
{{- default $root.Values.image.repository $image.repository -}}
{{- end }}

{{- define "core.image.tag" -}}
{{- $root := .root -}}
{{- $image := .image | default dict -}}
{{- default $root.Values.image.tag $image.tag -}}
{{- end }}

{{- define "core.image.pullPolicy" -}}
{{- $root := .root -}}
{{- $image := .image | default dict -}}
{{- default $root.Values.image.pullPolicy $image.pullPolicy -}}
{{- end }}

{{- define "core.image" -}}
{{- printf "%s:%s" (include "core.image.repository" .) (include "core.image.tag" .) -}}
{{- end }}
