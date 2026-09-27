{{- define "corp-llm-gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "corp-llm-gateway.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "corp-llm-gateway.labels" -}}
app.kubernetes.io/name: {{ include "corp-llm-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "corp-llm-gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "corp-llm-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "corp-llm-gateway.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "corp-llm-gateway.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "corp-llm-gateway.secretName" -}}
{{- if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else -}}
{{- printf "%s-env" (include "corp-llm-gateway.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Non-secret env for the gateway container AND the config-check initContainer, so
both see identical config. Templated CORP_* keys first, then the operator-set
`config:` passthrough. Secrets ride envFrom.secretRef, never here.
*/}}
{{- define "corp-llm-gateway.gatewayEnv" -}}
- name: CORP_LLM_AUTH_PROVIDER
  value: {{ .Values.corpLlm.authProvider | quote }}
- name: CORP_LLM_ENDPOINT
  value: {{ .Values.corpLlm.endpoint | quote }}
- name: CORP_LLM_MODEL
  value: {{ .Values.corpLlm.model | quote }}
- name: CORP_AUDIT_SINK
  value: {{ .Values.audit.sink | quote }}
- name: CORP_LANGFUSE_URL
  value: {{ .Values.audit.sinks.langfuse.endpoint | quote }}
- name: CORP_LLM_LOCAL_FIRST
  value: {{ .Values.guardrail.localFirst | quote }}
- name: CORP_LLM_GAZETTEER
  value: {{ .Values.guardrail.gazetteer | quote }}
- name: CORP_LLM_RULES_DIR
  value: {{ .Values.guardrail.rulesDir | quote }}
- name: CORP_METRICS_EXPORTER
  value: {{ .Values.metrics.exporter | quote }}
{{- if .Values.caBundle.enabled }}
# httpx (oracle client) reads CORP_LLM_CA_BUNDLE; litellm's aiohttp reads
# SSL_CERT_FILE — both point at the mounted internal-CA bundle.
- name: CORP_LLM_CA_BUNDLE
  value: {{ printf "%s/ca-bundle.pem" .Values.caBundle.mountPath | quote }}
- name: SSL_CERT_FILE
  value: {{ printf "%s/ca-bundle.pem" .Values.caBundle.mountPath | quote }}
{{- end }}
{{- if .Values.issuance.enabled }}
- name: CORP_LLM_GATEWAY_CONFIG_FILE
  value: {{ include "corp-llm-gateway.configTomlPath" . | quote }}
{{- end }}
{{- range $k, $v := .Values.config }}
{{- if hasPrefix "CORP_GATEWAY_ISSUE_" $k }}
{{- fail (printf "config.%s: set developer token issuance through issuance.*, not config: (an env var would shadow the rendered config file)" $k) }}
{{- end }}
{{- if and $.Values.issuance.enabled (eq $k "CORP_LLM_GATEWAY_CONFIG_FILE") }}
{{- fail "config.CORP_LLM_GATEWAY_CONFIG_FILE: the chart sets it when issuance.enabled is true" }}
{{- end }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- end -}}

{{- define "corp-llm-gateway.configTomlPath" -}}
/etc/corp-llm-gateway/config.toml
{{- end -}}

{{/*
The gateway config file rendered from issuance.*. Strings go through toJson: a
JSON string is a valid TOML basic string, so group names with `/`, quotes or
Cyrillic survive. teamMap is a list because the table's order is significant.
*/}}
{{- define "corp-llm-gateway.issuanceToml" -}}
{{- $i := .Values.issuance -}}
{{- range $field := list "issuer" "audience" "clientId" }}
{{- $_ := required (printf "issuance.%s is required when issuance.enabled is true" $field) (get $i $field) }}
{{- end }}
{{- if not $i.teamMap }}
{{- fail "issuance.teamMap is required when issuance.enabled is true" }}
{{- end }}
{{- if and .Values.networkPolicy.enabled (not .Values.networkPolicy.keycloak.enabled) }}
{{- fail "issuance.enabled with networkPolicy.enabled needs networkPolicy.keycloak.{enabled,cidr}: the gateway fetches the realm JWKS, and without the egress rule every issuance answers 503" }}
{{- end -}}
# Rendered by the corp-llm-gateway chart from the issuance.* values.
CORP_GATEWAY_ISSUE_OIDC_ISSUER = {{ $i.issuer | toString | toJson }}
CORP_GATEWAY_ISSUE_OIDC_AUDIENCE = {{ $i.audience | toString | toJson }}
CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID = {{ $i.clientId | toString | toJson }}
{{- with $i.jwksUrl }}
CORP_GATEWAY_ISSUE_OIDC_JWKS_URL = {{ . | toString | toJson }}
{{- end }}
CORP_GATEWAY_ISSUE_OIDC_TEAM_CLAIM = {{ $i.teamClaim | toString | toJson }}
CORP_GATEWAY_ISSUE_OIDC_USER_CLAIM = {{ $i.userClaim | toString | toJson }}
CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS = {{ $i.ttlDays | int64 | quote }}
CORP_GATEWAY_ISSUE_MAX_ACTIVE = {{ $i.maxActive | int64 | quote }}
CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS = {{ $i.minIntervalSeconds | int64 | quote }}
CORP_GATEWAY_ISSUE_MAX_INFLIGHT = {{ $i.maxInflight | int64 | quote }}
CORP_GATEWAY_ISSUE_RATE_PER_MINUTE = {{ $i.ratePerMinute | int64 | quote }}
CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS = {{ $i.storeTimeoutSeconds | int64 | quote }}

# Ordered: the first group in this list that the user belongs to wins.
[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]
{{- $seen := dict }}
{{- range $i.teamMap }}
{{- if not (and .group .team) }}
{{- fail "issuance.teamMap entries need a non-empty group and team" }}
{{- end }}
{{- $group := .group | toString }}
{{- if hasKey $seen $group }}
{{- fail (printf "issuance.teamMap lists group %s twice" ($group | toJson)) }}
{{- end }}
{{- $_ := set $seen $group true }}
{{ $group | toJson }} = {{ .team | toString | toJson }}
{{- end }}
{{- end -}}
