{{/* Delegate shared chart conventions to Bitnami Common. */}}
{{- define "stalwart.name" -}}
{{- include "common.names.name" . -}}
{{- end -}}

{{- define "stalwart.fullname" -}}
{{- include "common.names.fullname" . -}}
{{- end -}}

{{- define "stalwart.labels" -}}
{{- include "common.labels.standard" (dict "customLabels" (omit .Values.additionalLabels "app.kubernetes.io/name" "app.kubernetes.io/instance") "context" .) -}}
{{- end -}}

{{- define "stalwart.selectorLabels" -}}
{{- include "common.labels.matchLabels" . -}}
{{- end -}}

{{/* Selects only the Stalwart server pods (StatefulSet, Services, anti-affinity). The plain
     selectorLabels also match the Bulwark webmail pods. */}}
{{- define "stalwart.serverSelectorLabels" -}}
{{ include "stalwart.selectorLabels" . }}
app.kubernetes.io/component: server
{{- end -}}

{{- define "stalwart.image" -}}
{{- include "common.images.image" (dict "imageRoot" .Values.image "global" .Values.global "chart" .Chart) -}}
{{- end -}}

{{- define "stalwart.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "stalwart.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "stalwart.managementPort" -}}
{{- if .Values.recoveryMode.enabled -}}
{{- .Values.recoveryMode.port -}}
{{- else -}}
{{- .Values.management.port -}}
{{- end -}}
{{- end -}}

{{- define "stalwart.annotations" -}}
{{- merge (.specific | default dict) .context.Values.additionalAnnotations | toYaml -}}
{{- end -}}

{{/* ingress.hosts as configured, or a single mail.<domain> default
     derived from the top-level domain when left empty. */}}
{{- define "stalwart.ingressHosts" -}}
{{- if .Values.ingress.hosts -}}
{{- .Values.ingress.hosts | toYaml -}}
{{- else if .Values.domain -}}
{{- list (dict "host" (printf "mail.%s" .Values.domain) "paths" (list (dict "path" "/" "pathType" "Prefix" "portName" "https"))) | toYaml -}}
{{- else -}}
{{- fail "ingress.enabled requires ingress.hosts or a top-level domain" -}}
{{- end -}}
{{- end -}}

{{/* The largest replica count that can actually be running, whether from
     a static replicaCount or an enabled HPA's maxReplicas. */}}
{{- define "stalwart.effectiveMaxReplicas" -}}
{{- if .Values.autoscaling.enabled -}}
{{- .Values.autoscaling.maxReplicas -}}
{{- else -}}
{{- .Values.replicaCount -}}
{{- end -}}
{{- end -}}

{{/* PodDisruptionBudget limit from a pdb values block: minAvailable when set,
     otherwise maxUnavailable. "" counts as unset; 0 does not. */}}
{{- define "stalwart.pdbBudget" -}}
{{- if ne (toString .minAvailable) "" -}}
minAvailable: {{ .minAvailable }}
{{- else -}}
maxUnavailable: {{ .maxUnavailable }}
{{- end -}}
{{- end -}}

{{- define "stalwart.validateValues" -}}
{{- if not (has .Values.ingress.backend (list "mail" "management")) -}}
{{- fail "ingress.backend must be mail or management" -}}
{{- end -}}
{{- $httpRouteToMail := and .Values.gatewayAPI.httpRoute.enabled (ne .Values.gatewayAPI.httpRoute.backend "management") -}}
{{- if or $httpRouteToMail .Values.gatewayAPI.tcpRoutes -}}
{{- if not .Values.service.enabled -}}
{{- fail "Gateway API routes require service.enabled" -}}
{{- end -}}
{{- end -}}
{{- if .Values.gatewayAPI.httpRoute.enabled -}}
{{- if not .Values.gatewayAPI.httpRoute.parentRefs -}}
{{- fail "gatewayAPI.httpRoute.parentRefs must reference an existing Gateway" -}}
{{- end -}}
{{- if and $httpRouteToMail (not (hasKey .Values.service.ports .Values.gatewayAPI.httpRoute.backendPortName)) -}}
{{- fail "gatewayAPI.httpRoute.backendPortName must reference a service.ports entry" -}}
{{- end -}}
{{- end -}}
{{- range $name, $route := .Values.gatewayAPI.tcpRoutes -}}
{{- if not (hasKey $.Values.service.ports $name) -}}
{{- fail (printf "gatewayAPI.tcpRoutes.%s must reference a service.ports entry" $name) -}}
{{- end -}}
{{- end -}}

{{- if and (gt (len .Values.service.ipFamilies) 1) (not (has .Values.service.ipFamilyPolicy (list "PreferDualStack" "RequireDualStack"))) -}}
{{- fail "service.ipFamilies with two entries requires service.ipFamilyPolicy PreferDualStack or RequireDualStack" -}}
{{- end -}}
{{- if hasKey .Values.service.ports "mgmt" -}}
{{- fail "service.ports.mgmt is reserved; use management settings instead" -}}
{{- end -}}
{{- if and (gt (int (include "stalwart.effectiveMaxReplicas" .)) 1) (has (index (include "stalwart.dataStoreConfig" . | fromJson) "@type") (list "RocksDb" "Sqlite")) -}}
{{- fail "Multiple replicas require a shared external DataStore" -}}
{{- end -}}
{{- if and (gt (int (include "stalwart.effectiveMaxReplicas" .)) 1) (not .Values.coordinator.type) (not .Values.coordinator.externallyManaged) -}}
{{- fail "Multiple replicas require cluster coordination: set coordinator.type, or coordinator.externallyManaged=true if the Coordinator is configured outside Helm" -}}
{{- end -}}
{{- if and .Values.coordinator.type .Values.coordinator.externallyManaged -}}
{{- fail "coordinator.externallyManaged applies only when coordinator.type is empty" -}}
{{- end -}}
{{- if and .Values.pdb.create (eq (toString .Values.pdb.minAvailable) "") (eq (toString .Values.pdb.maxUnavailable) "") -}}
{{- fail "pdb.create requires pdb.maxUnavailable or pdb.minAvailable" -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled (gt (int .Values.autoscaling.minReplicas) (int .Values.autoscaling.maxReplicas)) -}}
{{- fail "autoscaling.minReplicas must not be greater than autoscaling.maxReplicas" -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled (not .Values.autoscaling.metrics) (not .Values.autoscaling.targetCPUUtilizationPercentage) (not .Values.autoscaling.targetMemoryUtilizationPercentage) -}}
{{- fail "autoscaling.enabled requires targetCPUUtilizationPercentage, targetMemoryUtilizationPercentage, or metrics" -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled .Values.autoscaling.targetCPUUtilizationPercentage (not (dig "requests" "cpu" false (.Values.resources | default dict))) -}}
{{- fail "autoscaling.targetCPUUtilizationPercentage requires resources.requests.cpu" -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled .Values.autoscaling.targetMemoryUtilizationPercentage (not (dig "requests" "memory" false (.Values.resources | default dict))) -}}
{{- fail "autoscaling.targetMemoryUtilizationPercentage requires resources.requests.memory" -}}
{{- end -}}
{{- if and .Values.ingress.enabled (ne .Values.ingress.backend "management") (not .Values.service.enabled) -}}
{{- fail "ingress.enabled requires service.enabled, or ingress.backend management" -}}
{{- end -}}
{{- if and (or .Values.metrics.auth.existingSecret .Values.metrics.auth.password) (not .Values.metrics.auth.username) -}}
{{- fail "metrics.auth.username is required when metrics.auth.existingSecret or metrics.auth.password is set" -}}
{{- end -}}
{{- if and .Values.metrics.serviceMonitor.enabled (not .Values.metrics.enabled) -}}
{{- fail "metrics.serviceMonitor.enabled requires metrics.enabled" -}}
{{- end -}}
{{- if and (not .Values.config) (eq .Values.dataStore.backend "postgresql") (not (default .Values.existingSecret .Values.dataStore.postgresql.existingSecret)) (not .Values.dataStore.postgresql.password) -}}
{{- fail "PostgreSQL requires dataStore.postgresql.existingSecret, existingSecret, or dataStore.postgresql.password" -}}
{{- end -}}
{{- if and (eq .Values.blobStore.backend "s3") (not .Values.blobStore.s3.existingSecret) (or (not .Values.blobStore.s3.accessKeyId) (not .Values.blobStore.s3.secretKey)) -}}
{{- fail "blobStore.s3 requires existingSecret, or both accessKeyId and secretKey" -}}
{{- end -}}
{{- if and (eq (include "stalwart.cliCredentialsNeeded" .) "true") (not .Values.provisioning.existingSecret) (not .Values.provisioning.password) -}}
{{- fail "provisioning.existingSecret or provisioning.password is required when S3 blob-store, metrics, email, OIDC or extra provisioning, console logging, or certificateReload is enabled" -}}
{{- end -}}
{{- if and (eq .Values.coordinator.type "nats") (not .Values.coordinator.nats.hosts) -}}
{{- fail "coordinator.nats.hosts is required when coordinator.type=nats" -}}
{{- end -}}
{{- if and (eq .Values.coordinator.type "redis") (not .Values.coordinator.redis.url) -}}
{{- fail "coordinator.redis.url is required when coordinator.type=redis" -}}
{{- end -}}
{{- if and (eq .Values.coordinator.type "kafka") (or (not .Values.coordinator.kafka.hosts) (not .Values.coordinator.kafka.groupId)) -}}
{{- fail "coordinator.kafka.hosts and coordinator.kafka.groupId are required when coordinator.type=kafka" -}}
{{- end -}}
{{- if and .Values.coordinator.type (ne .Values.inMemoryStore.type "redis") -}}
{{- fail "coordinator.type requires inMemoryStore.type=redis: Stalwart refuses a coordinator while the in-memory store is the local, single-node one" -}}
{{- end -}}
{{- if and (eq .Values.inMemoryStore.type "redis") (not .Values.inMemoryStore.redis.url) -}}
{{- fail "inMemoryStore.redis.url is required when inMemoryStore.type=redis" -}}
{{- end -}}
{{- if and (eq .Values.coordinator.type "zenoh") (not .Values.coordinator.zenoh.config) -}}
{{- fail "coordinator.zenoh.config is required when coordinator.type=zenoh" -}}
{{- end -}}
{{- if and .Values.hostname (not (.Values.mailDomain | default .Values.domain)) -}}
{{- fail "hostname requires mailDomain (or domain): Stalwart needs a default domain with the hostname" -}}
{{- end -}}
{{- if and .Values.oidc.activate (not .Values.oidc.enabled) -}}
{{- fail "oidc.activate requires oidc.enabled" -}}
{{- end -}}
{{- if and .Values.oidc.enabled (not .Values.oidc.issuerUrl) -}}
{{- fail "oidc.issuerUrl is required when oidc.enabled=true" -}}
{{- end -}}
{{- include "stalwart.validateWebmail" . -}}
{{- include "stalwart.validateSystemAccounts" . -}}
{{- end -}}

{{/* True when a chart-managed credentials Secret is needed for any plain-value fallback. */}}
{{- define "stalwart.needsManagedSecret" -}}
{{- if or
  (and .Values.recoveryAdmin.enabled (not .Values.recoveryAdmin.existingSecret))
  .Values.extraSecretEnv
  (and (not .Values.config) (eq .Values.dataStore.backend "postgresql") (not (default .Values.existingSecret .Values.dataStore.postgresql.existingSecret)) .Values.dataStore.postgresql.password)
  (and (eq .Values.blobStore.backend "s3") (not .Values.blobStore.s3.existingSecret) .Values.blobStore.s3.accessKeyId .Values.blobStore.s3.secretKey)
  (and (eq (include "stalwart.cliCredentialsNeeded" .) "true") (not .Values.provisioning.existingSecret) .Values.provisioning.password)
  (and .Values.metrics.enabled (not .Values.metrics.auth.existingSecret) .Values.metrics.auth.password)
  (and (eq .Values.coordinator.type "nats") (not .Values.coordinator.nats.existingSecret) .Values.coordinator.nats.password)
-}}
true
{{- end -}}
{{- end -}}

{{/* Raw config takes precedence for compatibility and unsupported backends. */}}
{{- define "stalwart.dataStoreConfig" -}}
{{- if .Values.config -}}
{{- .Values.config | toJson -}}
{{- else if eq .Values.dataStore.backend "postgresql" -}}
{{- $pg := .Values.dataStore.postgresql -}}
{{- dict "@type" "PostgreSql" "host" (required "dataStore.postgresql.host is required" $pg.host) "port" $pg.port "database" $pg.database "authUsername" $pg.username "authSecret" (dict "@type" "EnvironmentVariable" "variableName" "STALWART_DB_PASSWORD") "useTls" $pg.useTls "allowInvalidCerts" $pg.allowInvalidCerts "poolMaxConnections" $pg.poolMaxConnections | toJson -}}
{{- else -}}
{{- dict "@type" "RocksDb" "path" .Values.dataStore.rocksdb.path | toJson -}}
{{- end -}}
{{- end -}}

{{- define "stalwart.blobStorePlan" -}}
{{- $s3 := .Values.blobStore.s3 -}}
{{- $region := dict "@type" $s3.region -}}
{{- if $s3.custom.url -}}
{{- $region = dict "@type" "Custom" "url" $s3.custom.url "region" $s3.custom.region -}}
{{- end -}}
{{- $blob := dict "@type" "S3" "bucket" (required "blobStore.s3.bucket is required" $s3.bucket) "region" $region "accessKeyId" (dict "@type" "EnvironmentVariable" "variableName" "STALWART_S3_ACCESS_KEY_ID") "secretKey" (dict "@type" "EnvironmentVariable" "variableName" "STALWART_S3_SECRET_KEY") "sessionToken" (dict "@type" "None") "profile" nil "keyPrefix" ($s3.keyPrefix | default nil) "allowInvalidCerts" $s3.allowInvalidCerts "verifyAfterWrite" $s3.verifyAfterWrite -}}
{{- dict "@type" "update" "object" "BlobStore" "value" $blob | toJson -}}
{{- end -}}

{{/* Enables the Prometheus exporter singleton. authUsername is a plain field in
     Stalwart's schema; only authSecret supports EnvironmentVariable indirection. */}}
{{- define "stalwart.metricsPlan" -}}
{{- $auth := .Values.metrics.auth -}}
{{- $prometheus := dict "@type" "Enabled" -}}
{{- if or $auth.existingSecret $auth.password -}}
{{- $prometheus = merge $prometheus (dict "authUsername" $auth.username "authSecret" (dict "@type" "EnvironmentVariable" "variableName" "STALWART_METRICS_PASSWORD")) -}}
{{- end -}}
{{- dict "@type" "update" "object" "Metrics" "value" (dict "prometheus" $prometheus) | toJson -}}
{{- end -}}

{{/* Declares the Email singleton's encryption-at-rest toggles as-is;
     these already match Stalwart's own defaults. */}}
{{- define "stalwart.emailPlan" -}}
{{- dict "@type" "update" "object" "Email" "value" (dict "encryptAtRest" .Values.email.encryptAtRest "encryptOnAppend" .Values.email.encryptOnAppend) | toJson -}}
{{- end -}}

{{/* Stalwart serializes Set<T> fields as objects of value->true (e.g.
     acceptedAudiences {"stalwart": true}), not arrays, so the list in values is
     converted here. Declares an OIDC Directory object (matched by its shared "description"
     field) for delegating authentication to a third-party provider like
     Keycloak. Inert on its own until oidc.activate references it. */}}
{{- define "stalwart.oidcDirectoryPlan" -}}
{{- $o := .Values.oidc -}}
{{- $audiences := dict -}}
{{- range $o.acceptedAudiences -}}
{{- $_ := set $audiences . true -}}
{{- end -}}
{{- $value := dict "@type" "Oidc" "description" $o.name "issuerUrl" $o.issuerUrl "acceptedAudiences" $audiences "unscopedTokens" $o.unscopedTokens "claimUsername" $o.claimUsername "claimName" $o.claimName -}}
{{- if $o.usernameDomain -}}
{{- $value = merge $value (dict "usernameDomain" $o.usernameDomain) -}}
{{- end -}}
{{- if $o.claimGroups -}}
{{- $value = merge $value (dict "claimGroups" $o.claimGroups) -}}
{{- end -}}
{{- dict "@type" "upsert" "object" "Directory" "matchOn" (list "description") "value" (dict (printf "#%s" $o.name) $value) | toJson -}}
{{- end -}}

{{/* Activates the OIDC directory server-wide via Authentication.directoryId,
     cross-referencing the Directory declared above by its client-assigned id.
     See oidc.activate's comment in values.yaml for the risk this carries. */}}
{{- define "stalwart.oidcActivatePlan" -}}
{{- dict "@type" "update" "object" "Authentication" "value" (dict "directoryId" (printf "#%s" .Values.oidc.name)) | toJson -}}
{{- end -}}

{{/* Set<T> fields (hosts) are objects of value->true on the wire.
     credentials is required by the NATS variant even when unused. */}}
{{- define "stalwart.coordinatorPlan" -}}
{{- $c := .Values.coordinator -}}
{{- $value := dict -}}
{{- if eq $c.type "nats" -}}
{{- $hosts := dict -}}
{{- range $c.nats.hosts -}}
{{- $_ := set $hosts . true -}}
{{- end -}}
{{- $secret := dict "@type" "None" -}}
{{- if or $c.nats.existingSecret $c.nats.password -}}
{{- $secret = dict "@type" "EnvironmentVariable" "variableName" "STALWART_NATS_PASSWORD" -}}
{{- end -}}
{{- $value = dict "@type" "Nats" "hosts" $hosts "useTls" $c.nats.useTls "authUsername" $c.nats.authUsername "authSecret" $secret "credentials" (dict "@type" "None") -}}
{{- else if eq $c.type "redis" -}}
{{- $value = dict "@type" "Redis" "url" $c.redis.url -}}
{{- else if eq $c.type "kafka" -}}
{{- $hosts := dict -}}
{{- range $c.kafka.hosts -}}
{{- $_ := set $hosts . true -}}
{{- end -}}
{{- $value = dict "@type" "Kafka" "hosts" $hosts "groupId" $c.kafka.groupId -}}
{{- else if eq $c.type "zenoh" -}}
{{- $value = dict "@type" "Zenoh" "config" $c.zenoh.config -}}
{{- end -}}
{{- dict "@type" "update" "object" "Coordinator" "value" $value | toJson -}}
{{- end -}}

{{/* System accounts (systemAccounts.accounts) with defaults applied, as JSON:
     {"accounts": [...]}. Shared by the provisioning plan, the RBAC and the reconcile Job. */}}
{{- define "stalwart.systemAccounts" -}}
{{- $accounts := list -}}
{{- range .Values.systemAccounts.accounts -}}
{{- $domain := .domain | default $.Values.mailDomain | default $.Values.domain -}}
{{- $passwordPermissions := dict "@type" "Inherit" -}}
{{- with (dig "appPassword" "permissions" list .) -}}
{{- $set := dict -}}
{{- range . }}{{ $_ := set $set . true }}{{ end -}}
{{- $passwordPermissions = dict "@type" "Replace" "permissions" $set -}}
{{- end -}}
{{- $accounts = append $accounts (dict
  "id" .id
  "name" .name
  "domain" $domain
  "address" (printf "%s@%s" .name $domain)
  "aliases" (.aliases | default list)
  "permissions" (ternary .permissions (list "authenticate" "emailSend") (hasKey . "permissions"))
  "appPassword" (dict "allowedIps" (dig "appPassword" "allowedIps" list .) "permissions" $passwordPermissions)
  "secret" (dict "create" (dig "secret" "create" true .) "name" .secret.name "namespace" (dig "secret" "namespace" "" . | default $.Release.Namespace) "keys" (dig "secret" "keys" list . | default (list "password")))
) -}}
{{- end -}}
{{- dict "accounts" $accounts | toJson -}}
{{- end -}}

{{/* Plan lines creating the system accounts: their domains (matched by name, so existing ones
     are reused), then the accounts with aliases and permissions. Credentials are left to the
     system accounts job. */}}
{{- define "stalwart.systemAccountsPlan" -}}
{{- $lines := list -}}
{{- $domains := dict -}}
{{- range (include "stalwart.systemAccounts" . | fromJson).accounts -}}
{{- if not (hasKey $domains .domain) -}}
{{- $ref := printf "system-domain-%d" (len $domains) -}}
{{- $_ := set $domains .domain $ref -}}
{{- $lines = append $lines (dict "@type" "upsert" "object" "Domain" "matchOn" (list "name") "value" (dict $ref (dict "name" .domain)) | toJson) -}}
{{- end -}}
{{- $domainId := printf "#%s" (index $domains .domain) -}}
{{- $aliases := dict -}}
{{- range $i, $alias := .aliases -}}
{{- $_ := set $aliases (toString $i) (dict "name" $alias "domainId" $domainId "enable" true) -}}
{{- end -}}
{{- $permissions := dict -}}
{{- range .permissions }}{{ $_ := set $permissions . true }}{{ end -}}
{{- /* The description marks the account as managed by this chart, so that other tools (such as
       a provisioning connector adopting accounts without externalId) leave it alone. */}}
{{- $account := dict "@type" "User" "name" .name "domainId" $domainId "description" (printf "managed:%s/%s" $.Release.Name .id) "aliases" $aliases "permissions" (dict "@type" "Replace" "enabledPermissions" $permissions) -}}
{{- $lines = append $lines (dict "@type" "upsert" "object" "Account" "matchOn" (list "name") "value" (dict (printf "system-%s" .id) $account) | toJson) -}}
{{- end -}}
{{- join "\n" $lines -}}
{{- end -}}

{{/* Namespaces with Secrets of system accounts: {"<namespace>": ["<secret>", ...]}. */}}
{{- define "stalwart.systemAccountSecrets" -}}
{{- $scopes := dict -}}
{{- range (include "stalwart.systemAccounts" . | fromJson).accounts -}}
{{- $_ := set $scopes .secret.namespace (append (get $scopes .secret.namespace | default list) .secret.name | uniq) -}}
{{- end -}}
{{- $scopes | toJson -}}
{{- end -}}

{{- define "stalwart.validateSystemAccounts" -}}
{{- if eq (include "stalwart.provisioningEnabled" .) "true" -}}
{{- if eq .Values.updateStrategy.type "OnDelete" -}}
{{- fail "updateStrategy.type OnDelete: the reconcile Job applies settings by a rolling restart of the StatefulSet, which OnDelete never performs; use RollingUpdate" -}}
{{- end -}}
{{- if gt (int (dig "rollingUpdate" "partition" 0 .Values.updateStrategy)) 0 -}}
{{- fail "updateStrategy.rollingUpdate.partition above 0: the reconcile Job waits until every pod runs the new settings, which a partition prevents" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.systemAccounts.smtpCheck.enabled (not .Values.systemAccounts.smtpCheck.host) -}}
{{- fail "systemAccounts.smtpCheck.host is required when smtpCheck is enabled" -}}
{{- end -}}
{{- $ids := list -}}
{{- $names := list -}}
{{- $secrets := list -}}
{{- range .Values.systemAccounts.accounts -}}
{{- if has .id $ids -}}
{{- fail (printf "systemAccounts.accounts: id %q is used twice" .id) -}}
{{- end -}}
{{- $ids = append $ids .id -}}
{{- if has .name $names -}}
{{- fail (printf "systemAccounts.accounts: account name %q is used twice (Stalwart matches accounts by name)" .name) -}}
{{- end -}}
{{- $names = append $names .name -}}
{{- if not (.domain | default $.Values.mailDomain | default $.Values.domain) -}}
{{- fail (printf "systemAccounts.accounts[%s] needs a domain: set its domain, mailDomain or domain" .id) -}}
{{- end -}}
{{- $secret := printf "%s/%s" (dig "secret" "namespace" "" . | default $.Release.Namespace) .secret.name -}}
{{- if has $secret $secrets -}}
{{- fail (printf "systemAccounts.accounts[%s]: Secret %s is used by another account" .id $secret) -}}
{{- end -}}
{{- $secrets = append $secrets $secret -}}
{{- end -}}
{{- end -}}

{{/* True when the CLI apply Job has at least one plan line to run. */}}
{{- define "stalwart.provisioningEnabled" -}}
{{- if or (eq .Values.blobStore.backend "s3") (and .Values.metrics.enabled .Values.metrics.provision) .Values.email.provision .Values.oidc.enabled .Values.inMemoryStore.type .Values.coordinator.type .Values.systemAccounts.accounts .Values.provisioning.extraPlan .Values.logging.console.enabled .Values.hostname -}}
true
{{- end -}}
{{- end -}}

{{/* Console (stdout) tracer for `kubectl logs`, and the default file tracer switched off: the
     root file system is read-only, so its /var/log/stalwart output would go nowhere. Tracers
     have no name, so both are matched by their variant. */}}
{{- define "stalwart.loggingPlan" -}}
{{- $console := .Values.logging.console -}}
{{- $common := dict "ansi" false "lossy" false "multiline" false "eventsPolicy" "exclude" "events" dict -}}
{{- dict "@type" "upsert" "object" "Tracer" "matchOn" (list "@type") "value" (dict "console" (merge (dict "@type" "Stdout" "enable" true "level" $console.level "buffered" false) $common)) | toJson }}
{{ dict "@type" "upsert" "object" "Tracer" "matchOn" (list "@type") "value" (dict "file" (merge (dict "@type" "Log" "enable" false "level" $console.level "path" "/var/log/stalwart" "prefix" "stalwart.log" "rotate" "daily") $common)) | toJson -}}
{{- end -}}

{{/* True when a stalwart-cli workload (provisioning Job or certificate reload CronJob)
     needs the provisioning credentials. */}}
{{- define "stalwart.cliCredentialsNeeded" -}}
{{- if or (eq (include "stalwart.provisioningEnabled" .) "true") .Values.certificateReload.enabled -}}
true
{{- end -}}
{{- end -}}

{{/* Server settings applied by the provisioning Job: the chart's own, then extraPlan. Stalwart
     reads them at startup, so the reconcile Job restarts it when this plan changed. */}}
{{- define "stalwart.settingsPlan" -}}
{{- $lines := list -}}
{{- if eq .Values.blobStore.backend "s3" -}}
{{- $lines = append $lines (include "stalwart.blobStorePlan" .) -}}
{{- end -}}
{{- if and .Values.metrics.enabled .Values.metrics.provision -}}
{{- $lines = append $lines (include "stalwart.metricsPlan" .) -}}
{{- end -}}
{{- if .Values.email.provision -}}
{{- $lines = append $lines (include "stalwart.emailPlan" .) -}}
{{- end -}}
{{- if .Values.inMemoryStore.type -}}
{{- $lines = append $lines (dict "@type" "update" "object" "InMemoryStore" "value" (dict "@type" "Redis" "url" .Values.inMemoryStore.redis.url) | toJson) -}}
{{- end -}}
{{- if .Values.coordinator.type -}}
{{- $lines = append $lines (include "stalwart.coordinatorPlan" .) -}}
{{- end -}}
{{- if .Values.oidc.enabled -}}
{{- $lines = append $lines (include "stalwart.oidcDirectoryPlan" .) -}}
{{- if .Values.oidc.activate -}}
{{- $lines = append $lines (include "stalwart.oidcActivatePlan" .) -}}
{{- end -}}
{{- end -}}
{{- if .Values.logging.console.enabled -}}
{{- $lines = concat $lines (splitList "\n" (include "stalwart.loggingPlan" .)) -}}
{{- end -}}
{{- if .Values.hostname -}}
{{/* Stalwart requires a default domain with the hostname; the domain is matched by name, so an
     existing one is reused. */}}
{{- $lines = append $lines (dict "@type" "upsert" "object" "Domain" "matchOn" (list "name") "value" (dict "default-domain" (dict "name" (.Values.mailDomain | default .Values.domain))) | toJson) -}}
{{- $lines = append $lines (dict "@type" "update" "object" "SystemSettings" "value" (dict "defaultHostname" .Values.hostname "defaultDomainId" "#default-domain") | toJson) -}}
{{- end -}}
{{- range .Values.provisioning.extraPlan -}}
{{- $lines = append $lines (toJson .) -}}
{{- end -}}
{{- join "\n" $lines -}}
{{- end -}}

{{/* The provisioning Job's plan: the settings, then the system accounts, which need no restart. */}}
{{- define "stalwart.provisioningPlan" -}}
{{- $lines := list -}}
{{- with include "stalwart.settingsPlan" . }}{{ $lines = append $lines . }}{{ end -}}
{{- if .Values.systemAccounts.accounts -}}
{{- $lines = append $lines (include "stalwart.systemAccountsPlan" .) -}}
{{- end -}}
{{- join "\n" $lines -}}
{{- end -}}
