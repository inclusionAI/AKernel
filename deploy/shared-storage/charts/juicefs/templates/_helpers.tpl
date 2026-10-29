{{- define "juicefs.name" -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "juicefs.auth" -}}
{{- default (printf "%s-postgres" (include "juicefs.name" .)) .Values.postgres.existingSecret -}}
{{- end -}}

{{- define "juicefs.placement" -}}
{{- if . }}
nodeAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    nodeSelectorTerms:
      - matchExpressions:
          {{- range $key, $value := . }}
          - key: {{ $key | quote }}
            operator: In
            values: [{{ $value | quote }}]
          {{- end }}
{{- else }}
{}
{{- end }}
{{- end -}}

{{- define "juicefs.env" -}}
- name: META_URL
  {{- if eq .Values.postgres.mode "external" }}
  {{- $pg := .Values.postgres.external }}
  value: {{ printf "postgres://%s@%s:%d/%s?sslmode=%s&sslrootcert=/etc/juicefs/postgres/ca.crt" $pg.user $pg.host (int $pg.port) $pg.database $pg.sslMode | quote }}
  {{- else }}
  value: {{ printf "postgres://juicefs@%s-postgres.%s.svc.cluster.local:5432/juicefs?sslmode=disable" (include "juicefs.name" .) .Release.Namespace | quote }}
  {{- end }}
- name: META_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ include "juicefs.auth" . | quote }}, key: password}
- name: ACCESS_KEY
  valueFrom:
    secretKeyRef: {name: {{ printf "%s-bucket" (include "juicefs.name" .) | quote }}, key: AWS_ACCESS_KEY_ID}
- name: SECRET_KEY
  valueFrom:
    secretKeyRef: {name: {{ printf "%s-bucket" (include "juicefs.name" .) | quote }}, key: AWS_SECRET_ACCESS_KEY}
{{- end -}}

{{- define "juicefs.pgMounts" -}}
{{- if and (eq .Values.postgres.mode "external") .Values.postgres.external.caSecret }}
- {name: postgres-ca, mountPath: /etc/juicefs/postgres, readOnly: true}
{{- end }}
{{- end -}}

{{- define "juicefs.pgVolumes" -}}
{{- if and (eq .Values.postgres.mode "external") .Values.postgres.external.caSecret }}
- name: postgres-ca
  secret:
    secretName: {{ .Values.postgres.external.caSecret | quote }}
    items: [{key: ca.crt, path: ca.crt}]
{{- end }}
{{- end -}}

{{- define "juicefs.cephPlacement" -}}
{{ include "juicefs.placement" .Values.ceph.nodeSelector }}
{{- with .Values.ceph.tolerations }}
tolerations: {{ toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
