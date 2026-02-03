{{/*
Expand the name of the chart.
*/}}
{{- define "nextcloud-mcp-server.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "nextcloud-mcp-server.fullname" -}}
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
{{- define "nextcloud-mcp-server.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "nextcloud-mcp-server.labels" -}}
helm.sh/chart: {{ include "nextcloud-mcp-server.chart" . }}
{{ include "nextcloud-mcp-server.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "nextcloud-mcp-server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nextcloud-mcp-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "nextcloud-mcp-server.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "nextcloud-mcp-server.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Create the name of the secret to use for basic auth
*/}}
{{- define "nextcloud-mcp-server.basicAuthSecretName" -}}
{{- if .Values.auth.basic.existingSecret }}
{{- .Values.auth.basic.existingSecret }}
{{- else }}
{{- include "nextcloud-mcp-server.fullname" . }}-basic-auth
{{- end }}
{{- end }}

{{/*
Create the name of the secret to use for multi-user basic auth
*/}}
{{- define "nextcloud-mcp-server.multiUserBasicSecretName" -}}
{{- if .Values.auth.multiUserBasic.existingSecret }}
{{- .Values.auth.multiUserBasic.existingSecret }}
{{- else }}
{{- include "nextcloud-mcp-server.fullname" . }}-multi-user-basic
{{- end }}
{{- end }}

{{/*
Create the name of the PVC to use for multi-user basic token storage
*/}}
{{- define "nextcloud-mcp-server.multiUserBasicPvcName" -}}
{{- if .Values.auth.multiUserBasic.persistence.existingClaim }}
{{- .Values.auth.multiUserBasic.persistence.existingClaim }}
{{- else }}
{{- include "nextcloud-mcp-server.fullname" . }}-token-storage
{{- end }}
{{- end }}

{{/*
Create the name of the secret to use for OAuth
*/}}
{{- define "nextcloud-mcp-server.oauthSecretName" -}}
{{- if .Values.auth.oauth.existingSecret }}
{{- .Values.auth.oauth.existingSecret }}
{{- else }}
{{- include "nextcloud-mcp-server.fullname" . }}-oauth
{{- end }}
{{- end }}

{{/*
Create the name of the PVC to use for OAuth storage
*/}}
{{- define "nextcloud-mcp-server.oauthPvcName" -}}
{{- if .Values.auth.oauth.persistence.existingClaim }}
{{- .Values.auth.oauth.persistence.existingClaim }}
{{- else }}
{{- include "nextcloud-mcp-server.fullname" . }}-oauth-storage
{{- end }}
{{- end }}

{{/*
Create the name of the PVC to use for Qdrant local persistent storage
*/}}
{{- define "nextcloud-mcp-server.qdrantPvcName" -}}
{{- if .Values.qdrant.localPersistence.existingClaim }}
{{- .Values.qdrant.localPersistence.existingClaim }}
{{- else }}
{{- include "nextcloud-mcp-server.fullname" . }}-qdrant-data
{{- end }}
{{- end }}

{{/*
Create the name of the PVC to use for /app/data storage
*/}}
{{- define "nextcloud-mcp-server.dataStoragePvcName" -}}
{{- if .Values.dataStorage.existingClaim }}
{{- .Values.dataStorage.existingClaim }}
{{- else }}
{{- include "nextcloud-mcp-server.fullname" . }}-data-storage
{{- end }}
{{- end }}

{{/*
Determine if data storage PVC should be enabled (backward compatible)
Checks new dataStorage.enabled OR legacy persistence configs
*/}}
{{- define "nextcloud-mcp-server.dataStorageEnabled" -}}
{{- if .Values.dataStorage.enabled -}}
true
{{- else if and (eq .Values.auth.mode "multi-user-basic") .Values.auth.multiUserBasic.enableOfflineAccess .Values.auth.multiUserBasic.persistence.enabled -}}
true
{{- else if and (eq .Values.qdrant.mode "persistent") .Values.qdrant.localPersistence.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end }}

{{/*
Check if legacy multi-user-basic persistence config is being used
*/}}
{{- define "nextcloud-mcp-server.legacyMultiUserBasicPersistence" -}}
{{- if and (eq .Values.auth.mode "multi-user-basic") .Values.auth.multiUserBasic.enableOfflineAccess .Values.auth.multiUserBasic.persistence.enabled (not .Values.dataStorage.enabled) -}}
true
{{- else -}}
false
{{- end -}}
{{- end }}

{{/*
Check if legacy qdrant persistence config is being used
*/}}
{{- define "nextcloud-mcp-server.legacyQdrantPersistence" -}}
{{- if and (eq .Values.qdrant.mode "persistent") .Values.qdrant.localPersistence.enabled (not .Values.dataStorage.enabled) -}}
true
{{- else -}}
false
{{- end -}}
{{- end }}

{{/*
Return the MCP server port
*/}}
{{- define "nextcloud-mcp-server.port" -}}
{{- .Values.mcp.port }}
{{- end }}

{{/*
Return the image tag (always uses chart appVersion)
*/}}
{{- define "nextcloud-mcp-server.imageTag" -}}
{{- .Chart.AppVersion }}
{{- end }}

{{/*
Return the public issuer URL for OAuth
Defaults to nextcloud.host if not specified
*/}}
{{- define "nextcloud-mcp-server.publicIssuerUrl" -}}
{{- if .Values.nextcloud.publicIssuerUrl }}
{{- .Values.nextcloud.publicIssuerUrl }}
{{- else }}
{{- .Values.nextcloud.host }}
{{- end }}
{{- end }}

{{/*
Return the MCP server URL for OAuth callbacks
If not specified:
  - Uses ingress host if ingress is enabled
  - Otherwise defaults to http://localhost:8000 (for port-forward setups)
*/}}
{{- define "nextcloud-mcp-server.mcpServerUrl" -}}
{{- if .Values.nextcloud.mcpServerUrl }}
{{- .Values.nextcloud.mcpServerUrl }}
{{- else if .Values.ingress.enabled }}
{{- $host := index .Values.ingress.hosts 0 }}
{{- if .Values.ingress.tls }}
{{- printf "https://%s" $host.host }}
{{- else }}
{{- printf "http://%s" $host.host }}
{{- end }}
{{- else }}
{{- printf "http://localhost:%d" (int .Values.mcp.port) }}
{{- end }}
{{- end }}
