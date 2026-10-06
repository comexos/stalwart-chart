{{/* webmail.ingress.host as configured, or webmail.<domain> derived
     from the top-level domain when left empty. */}}
{{- define "stalwart.webmailIngressHost" -}}
{{- if .Values.webmail.ingress.host -}}
{{- .Values.webmail.ingress.host -}}
{{- else if .Values.domain -}}
{{- printf "webmail.%s" .Values.domain -}}
{{- else -}}
{{- fail "webmail.ingress.enabled requires webmail.ingress.host or a top-level domain" -}}
{{- end -}}
{{- end -}}

{{/* webmail.gatewayAPI.httpRoute.hostnames as configured, or a single
     webmail.<domain> default derived from the top-level domain. */}}
{{- define "stalwart.webmailGatewayHostnames" -}}
{{- if .Values.webmail.gatewayAPI.httpRoute.hostnames -}}
{{- .Values.webmail.gatewayAPI.httpRoute.hostnames | toYaml -}}
{{- else if .Values.domain -}}
{{- list (printf "webmail.%s" .Values.domain) | toYaml -}}
{{- else -}}
{{- fail "webmail.gatewayAPI.httpRoute.enabled requires hostnames or a top-level domain" -}}
{{- end -}}
{{- end -}}

{{/* Bulwark webmail: a separate component, so it gets its own name,
     selector (name+instance+component=webmail, to avoid matching the
     main StatefulSet's pods or the internal management Service), and
     image reference. */}}
{{- define "stalwart.webmailFullname" -}}
{{- printf "%s-webmail" (include "stalwart.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "stalwart.webmailAnnotations" -}}
{{- merge (.specific | default dict) .context.Values.webmail.additionalAnnotations | toYaml -}}
{{- end -}}

{{- define "stalwart.webmailLabels" -}}
{{- include "common.labels.standard" (dict "customLabels" (merge (dict "app.kubernetes.io/component" "webmail") (omit .Values.webmail.additionalLabels "app.kubernetes.io/name" "app.kubernetes.io/instance" "app.kubernetes.io/component")) "context" .) -}}
{{- end -}}

{{- define "stalwart.webmailSelectorLabels" -}}
{{ include "common.labels.matchLabels" . }}
app.kubernetes.io/component: webmail
{{- end -}}

{{- define "stalwart.webmailImage" -}}
{{- include "common.images.image" (dict "imageRoot" .Values.webmail.image "global" .Values.global "chart" .Chart) -}}
{{- end -}}

{{- define "stalwart.webmailServiceAccountName" -}}
{{- if .Values.webmail.serviceAccount.create -}}
{{- default (include "stalwart.webmailFullname" .) .Values.webmail.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.webmail.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Falls back to this release's own internal HTTP Service when no
     public URL is configured; requires service.enabled with an "http"
     port, otherwise an explicit webmail.jmapServerUrl is required. */}}
{{- define "stalwart.webmailJmapUrl" -}}
{{- if .Values.webmail.jmapServerUrl -}}
{{- .Values.webmail.jmapServerUrl -}}
{{- else if and .Values.service.enabled (hasKey .Values.service.ports "http") -}}
{{- printf "http://%s:%v" (include "stalwart.fullname" .) (index .Values.service.ports "http").port -}}
{{- else -}}
{{- fail "webmail.jmapServerUrl is required, or service.enabled with a service.ports.http entry" -}}
{{- end -}}
{{- end -}}

{{/* True when a chart-managed credentials Secret is needed for the
     webmail component's plain-value fallbacks. */}}
{{- define "stalwart.webmailNeedsManagedSecret" -}}
{{- if and .Values.webmail.enabled (or
  (and (not .Values.webmail.sessionSecret.existingSecret) .Values.webmail.sessionSecret.value)
  (and (not .Values.webmail.adminPassword.existingSecret) .Values.webmail.adminPassword.value)
  (and .Values.webmail.oauth.enabled (not .Values.webmail.oauth.existingSecret) .Values.webmail.oauth.clientSecret)
) -}}
true
{{- end -}}
{{- end -}}

{{- define "stalwart.webmailEffectiveMaxReplicas" -}}
{{- if .Values.webmail.autoscaling.enabled -}}
{{- .Values.webmail.autoscaling.maxReplicas -}}
{{- else -}}
{{- .Values.webmail.replicaCount -}}
{{- end -}}
{{- end -}}

{{/* Bulwark-specific checks, called from stalwart.validateValues. */}}
{{- define "stalwart.validateWebmail" -}}
{{- if and (gt (len .Values.webmail.service.ipFamilies) 1) (not (has .Values.webmail.service.ipFamilyPolicy (list "PreferDualStack" "RequireDualStack"))) -}}
{{- fail "webmail.service.ipFamilies with two entries requires webmail.service.ipFamilyPolicy PreferDualStack or RequireDualStack" -}}
{{- end -}}
{{- if and .Values.webmail.ingress.enabled (not .Values.webmail.enabled) -}}
{{- fail "webmail.ingress.enabled requires webmail.enabled" -}}
{{- end -}}
{{- if and .Values.webmail.gatewayAPI.httpRoute.enabled (not .Values.webmail.enabled) -}}
{{- fail "webmail.gatewayAPI.httpRoute.enabled requires webmail.enabled" -}}
{{- end -}}
{{- if and .Values.webmail.gatewayAPI.httpRoute.enabled (not .Values.webmail.gatewayAPI.httpRoute.parentRefs) -}}
{{- fail "webmail.gatewayAPI.httpRoute.parentRefs must reference an existing Gateway" -}}
{{- end -}}
{{- if and .Values.webmail.oauth.enabled (not .Values.webmail.oauth.clientId) -}}
{{- fail "webmail.oauth.clientId is required when webmail.oauth.enabled=true" -}}
{{- end -}}
{{- if and .Values.webmail.autoscaling.enabled (not .Values.webmail.enabled) -}}
{{- fail "webmail.autoscaling.enabled requires webmail.enabled" -}}
{{- end -}}
{{- if and (gt (int (include "stalwart.webmailEffectiveMaxReplicas" .)) 1) .Values.webmail.persistence.enabled (eq .Values.webmail.persistence.accessMode "ReadWriteOnce") -}}
{{- fail "webmail.replicaCount/autoscaling.maxReplicas > 1 requires a ReadWriteMany webmail.persistence.accessMode, or webmail.persistence.enabled=false to run without shared storage" -}}
{{- end -}}
{{- if and .Values.webmail.settingsSync.enabled (not .Values.webmail.persistence.enabled) -}}
{{- fail "webmail.settingsSync.enabled requires webmail.persistence.enabled" -}}
{{- end -}}
{{- if and (gt (int (include "stalwart.webmailEffectiveMaxReplicas" .)) 1) (not .Values.webmail.adminPassword.existingSecret) (not .Values.webmail.adminPassword.value) -}}
{{- fail "webmail.replicaCount/autoscaling.maxReplicas > 1 requires webmail.adminPassword.existingSecret or .value (each replica would otherwise generate its own random password)" -}}
{{- end -}}
{{- if and (gt (int (include "stalwart.webmailEffectiveMaxReplicas" .)) 1) (not .Values.webmail.sessionSecret.existingSecret) -}}
{{- if not .Values.webmail.sessionSecret.value -}}
{{- fail "webmail.replicaCount/autoscaling.maxReplicas > 1 requires webmail.sessionSecret.existingSecret or .value (admin session cookies require a shared SESSION_SECRET)" -}}
{{- else if lt (len .Values.webmail.sessionSecret.value) 32 -}}
{{- fail "webmail.sessionSecret.value must be at least 32 characters for multi-replica admin sessions" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.webmail.oauth.autoSso (not .Values.webmail.oauth.only) -}}
{{- fail "webmail.oauth.autoSso requires webmail.oauth.only" -}}
{{- end -}}
{{- if and .Values.webmail.autoscaling.enabled (gt (int .Values.webmail.autoscaling.minReplicas) (int .Values.webmail.autoscaling.maxReplicas)) -}}
{{- fail "webmail.autoscaling.minReplicas must not be greater than webmail.autoscaling.maxReplicas" -}}
{{- end -}}
{{- if and .Values.webmail.autoscaling.enabled (not .Values.webmail.autoscaling.metrics) (not .Values.webmail.autoscaling.targetCPUUtilizationPercentage) (not .Values.webmail.autoscaling.targetMemoryUtilizationPercentage) -}}
{{- fail "webmail.autoscaling.enabled requires targetCPUUtilizationPercentage, targetMemoryUtilizationPercentage, or metrics" -}}
{{- end -}}
{{- if and .Values.webmail.autoscaling.enabled .Values.webmail.autoscaling.targetCPUUtilizationPercentage (not (dig "requests" "cpu" false (.Values.webmail.resources | default dict))) -}}
{{- fail "webmail.autoscaling.targetCPUUtilizationPercentage requires webmail.resources.requests.cpu" -}}
{{- end -}}
{{- if and .Values.webmail.autoscaling.enabled .Values.webmail.autoscaling.targetMemoryUtilizationPercentage (not (dig "requests" "memory" false (.Values.webmail.resources | default dict))) -}}
{{- fail "webmail.autoscaling.targetMemoryUtilizationPercentage requires webmail.resources.requests.memory" -}}
{{- end -}}
{{- end -}}
