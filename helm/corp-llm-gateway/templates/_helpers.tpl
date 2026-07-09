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
{{- range $k, $v := .Values.config }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- end -}}
