{{- $config := fromToml .config -}}
{{- if hasKey $config "Error" -}}
{{- fail "PVM sandboxd.config must be valid TOML with no duplicate fields" -}}
{{- end -}}
{{- $table := $config -}}
{{- range $key := list "plugin" "resource" "memory_limit" -}}
{{- /* sandboxd also accepts uppercase aliases for its struct fields. */ -}}
{{- if hasKey $table (upper $key) -}}
{{- fail "PVM memory policy requires lowercase plugin.resource.memory_limit keys" -}}
{{- end -}}
{{- if hasKey $table $key -}}
{{- $table = get $table $key -}}
{{- if not (kindIs "map" $table) -}}
{{- fail (printf "PVM memory policy requires %s to be a TOML table" $key) -}}
{{- end -}}
{{- else -}}
{{- $table = dict -}}
{{- end -}}
{{- end -}}
{{- if hasKey $table "enabled" -}}
{{- if not (kindIs "bool" (get $table "enabled")) -}}
{{- fail "PVM memory policy requires a boolean enabled field" -}}
{{- end -}}
{{- if ne (get $table "enabled") .enabled -}}
{{- $_ := set $table "enabled" .enabled -}}
{{- $rendered := toToml $config -}}
{{- if or (not $rendered) (hasKey (fromToml $rendered) "Error") -}}
{{- fail "Could not render the PVM sandboxd configuration as TOML" -}}
{{- end -}}
{{- $rendered -}}
{{- else -}}
{{- .config -}}
{{- end -}}
{{- else if .enabled -}}
{{- fail "PVM memory policy requires a boolean enabled field when opting in" -}}
{{- else -}}
{{- .config -}}
{{- end -}}
