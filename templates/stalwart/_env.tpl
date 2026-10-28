{{/* Chart-generated environment; overrides are merged by stalwart.env. */}}
{{- define "stalwart.generatedEnv" }}
{{- if and .Values.recoveryAdmin.enabled .Values.recoveryAdmin.existingSecret }}
- name: STALWART_RECOVERY_ADMIN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.recoveryAdmin.existingSecret | quote }}
      key: {{ .Values.recoveryAdmin.secretKey | quote }}
{{- end }}
{{- if and .Values.metrics.enabled (or .Values.metrics.auth.existingSecret .Values.metrics.auth.password) }}
- name: STALWART_METRICS_PASSWORD
  valueFrom:
    secretKeyRef:
      {{- if .Values.metrics.auth.existingSecret }}
      name: {{ .Values.metrics.auth.existingSecret | quote }}
      key: {{ .Values.metrics.auth.passwordSecretKey | quote }}
      {{- else }}
      name: {{ include "stalwart.fullname" . }}-env
      key: metricsPassword
      {{- end }}
{{- end }}
{{- if and (eq .Values.coordinator.type "nats") (or .Values.coordinator.nats.existingSecret .Values.coordinator.nats.password) }}
- name: STALWART_NATS_PASSWORD
  valueFrom:
    secretKeyRef:
      {{- if .Values.coordinator.nats.existingSecret }}
      name: {{ .Values.coordinator.nats.existingSecret | quote }}
      key: {{ .Values.coordinator.nats.passwordSecretKey | quote }}
      {{- else }}
      name: {{ include "stalwart.fullname" . }}-env
      key: natsPassword
      {{- end }}
{{- end }}
{{- if eq .Values.blobStore.backend "s3" }}
- name: STALWART_S3_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      {{- if .Values.blobStore.s3.existingSecret }}
      name: {{ .Values.blobStore.s3.existingSecret | quote }}
      key: {{ .Values.blobStore.s3.accessKeyIdSecretKey | quote }}
      {{- else }}
      name: {{ include "stalwart.fullname" . }}-env
      key: s3AccessKeyId
      {{- end }}
- name: STALWART_S3_SECRET_KEY
  valueFrom:
    secretKeyRef:
      {{- if .Values.blobStore.s3.existingSecret }}
      name: {{ .Values.blobStore.s3.existingSecret | quote }}
      key: {{ .Values.blobStore.s3.secretKeySecretKey | quote }}
      {{- else }}
      name: {{ include "stalwart.fullname" . }}-env
      key: s3SecretKey
      {{- end }}
{{- end }}
{{- if and (not .Values.config) (eq .Values.dataStore.backend "postgresql") }}
{{- $pgSecret := default .Values.existingSecret .Values.dataStore.postgresql.existingSecret }}
- name: STALWART_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      {{- if $pgSecret }}
      name: {{ $pgSecret | quote }}
      key: {{ .Values.dataStore.postgresql.passwordSecretKey | quote }}
      {{- else }}
      name: {{ include "stalwart.fullname" . }}-env
      key: postgresPassword
      {{- end }}
{{- end }}
{{- if .Values.role }}
- name: STALWART_ROLE
  value: {{ .Values.role | quote }}
{{- end }}
{{- if .Values.pushShard }}
- name: STALWART_PUSH_SHARD
  value: {{ .Values.pushShard | quote }}
{{- end }}
{{- if .Values.recoveryMode.enabled }}
- name: STALWART_RECOVERY_MODE
  value: "true"
- name: STALWART_RECOVERY_MODE_PORT
  value: {{ .Values.recoveryMode.port | quote }}
- name: STALWART_RECOVERY_MODE_LOG_LEVEL
  value: {{ .Values.recoveryMode.logLevel | quote }}
{{- end }}
{{- end }}

{{/* Same precedence as OpenCloud: extraEnvVars > extraEnv > generated values.
     Keep list order so Kubernetes can expand references to preceding variables. */}}
{{- define "stalwart.env" -}}
{{- $overrides := .Values.extraEnvVars | default list -}}
{{- $names := list -}}
{{- range $overrides -}}
{{- if has .name $names -}}
{{- fail (printf "extraEnvVars contains duplicate variable %q" .name) -}}
{{- end -}}
{{- $names = append $names .name -}}
{{- end -}}
{{- range $key, $value := .Values.extraEnv -}}
{{- if not (has $key $names) -}}
{{- $overrides = append $overrides (dict "name" $key "value" (toString $value)) -}}
{{- $names = append $names $key -}}
{{- end -}}
{{- end -}}
{{- $env := list -}}
{{- range include "stalwart.generatedEnv" . | fromYamlArray -}}
{{- if not (has .name $names) -}}{{- $env = append $env . -}}{{- end -}}
{{- end -}}
{{- concat $env $overrides | toYaml -}}
{{- end -}}
