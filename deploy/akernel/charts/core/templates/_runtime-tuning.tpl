{{/* Optional runtime controls require markers in the correct TOML section. */}}
{{- define "akernel.runtimeTuningInject" -}}
{{- $config := .config -}}
{{- if .fields -}}
{{- $target := .section -}}
{{- $marker := .marker -}}
{{- $section := "" -}}
{{- $count := 0 -}}
{{- range $line := splitList "\n" $config -}}
{{- $trimmed := trim $line -}}
{{- if regexMatch "^\\[[^\\[\\]]+\\][\\t ]*(#.*)?$" $trimmed -}}
{{- $section = regexFind "^\\[[^\\[\\]]+\\]" $trimmed -}}
{{- end -}}
{{- if eq $trimmed $marker -}}
{{- $count = add1 $count -}}
{{- if ne $section $target -}}
{{- fail (printf "%s must be under %s" $marker $target) -}}
{{- end -}}
{{- end -}}
{{- if eq $section $target -}}
{{- range $key, $_ := $.fields -}}
{{- if regexMatch (printf "^[\\t ]*%s[\\t ]*=" $key) $line -}}
{{- fail (printf "%s is already set under %s; use the config template or runtimeTuning" $key $target) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if ne (int $count) 1 -}}
{{- fail (printf "runtime tuning requires exactly one %s under %s" $marker $target) -}}
{{- end -}}
{{- $lines := list -}}
{{- range $key, $value := .fields -}}
{{- $lines = append $lines (printf "%s=%s" $key $value) -}}
{{- end -}}
{{- $config = replace $marker (join "\n" $lines) $config -}}
{{- end -}}
{{- $config -}}
{{- end -}}

{{- define "akernel.sandboxdRuntimeTuning" -}}
{{- if and (not (kindIs "invalid" .tuning)) (not (kindIs "map" .tuning)) -}}
{{- fail "node.config.sandboxd.runtimeTuning must be an object" -}}
{{- end -}}
{{- $tuning := .tuning | default dict -}}
{{- $config := .config -}}
{{- $resource := dict -}}
{{- $runsc := dict -}}
{{- $names := dict "firecrackerCheckpointConcurrency" "firecracker_checkpoint_concurrency" "firecrackerRestoreConcurrency" "firecracker_restore_concurrency" "runscCliGomaxprocs" "cli_gomaxprocs" -}}
{{- range $name, $value := $tuning -}}
{{- if not (or (hasKey $names $name) (eq $name "runscExtraArgs")) -}}
{{- fail (printf "unknown node.config.sandboxd.runtimeTuning option: %s" $name) -}}
{{- end -}}
{{- if not (kindIs "invalid" $value) -}}
{{- if eq $name "runscExtraArgs" -}}
{{- if not (kindIs "slice" $value) -}}
{{- fail "node.config.sandboxd.runtimeTuning.runscExtraArgs must be an array of strings" -}}
{{- end -}}
{{- range $arg := $value -}}
{{- if not (kindIs "string" $arg) -}}
{{- fail "node.config.sandboxd.runtimeTuning.runscExtraArgs must be an array of strings" -}}
{{- end -}}
{{- if contains (printf "%c" 0) $arg -}}
{{- fail "node.config.sandboxd.runtimeTuning.runscExtraArgs must not contain NUL" -}}
{{- end -}}
{{- end -}}
{{- $_ := set $runsc "extra_args" (toJson $value) -}}
{{- else -}}
{{- $raw := toString $value -}}
{{- if or (not (regexMatch "^(0|[1-9][0-9]{0,3})$" $raw)) (gt (int $raw) 1024) -}}
{{- fail (printf "node.config.sandboxd.runtimeTuning.%s must be an integer between 0 and 1024" $name) -}}
{{- end -}}
{{- $key := get $names $name -}}
{{- if eq $name "runscCliGomaxprocs" -}}
{{- $_ := set $runsc $key $raw -}}
{{- else -}}
{{- $_ := set $resource $key $raw -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $config = include "akernel.runtimeTuningInject" (dict "config" $config "section" "[plugin.resource]" "marker" "# AKERNEL_RUNTIME_RESOURCE_TUNING" "fields" $resource) -}}
{{- include "akernel.runtimeTuningInject" (dict "config" $config "section" "[plugin.runtime.runsc]" "marker" "# AKERNEL_RUNTIME_RUNSC_TUNING" "fields" $runsc) -}}
{{- end -}}
