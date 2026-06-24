{{/* Chart name (optionally overridden). */}}
{{- define "pharos-sre-bot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified app name. */}}
{{- define "pharos-sre-bot.fullname" -}}
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

{{- define "pharos-sre-bot.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "pharos-sre-bot.labels" -}}
helm.sh/chart: {{ include "pharos-sre-bot.chart" . }}
{{ include "pharos-sre-bot.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "pharos-sre-bot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "pharos-sre-bot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* ServiceAccount name to use. */}}
{{- define "pharos-sre-bot.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "pharos-sre-bot.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Name of the Secret holding LLM_API_KEY / GRAFANA_SERVICE_ACCOUNT_TOKEN. */}}
{{- define "pharos-sre-bot.secretName" -}}
{{- if .Values.secret.existingSecret -}}
{{- .Values.secret.existingSecret -}}
{{- else -}}
{{- printf "%s-secret" (include "pharos-sre-bot.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* Agent image ref; tag falls back to chart appVersion. */}}
{{- define "pharos-sre-bot.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}

{{/*
  Read-only RBAC rules over NAMESPACED resources only (safe in a Role or a
  ClusterRole). The ClusterRole additionally grants the cluster-scoped reads
  (nodes, namespaces) inline.
*/}}
{{- define "pharos-sre-bot.k8sReadRulesNamespaced" -}}
- apiGroups: [""]
  resources:
    - pods
    - pods/log
    - events
    - services
    - endpoints
    - configmaps
    - persistentvolumeclaims
    - replicationcontrollers
  verbs: ["get", "list", "watch"]
- apiGroups: ["apps"]
  resources:
    - deployments
    - replicasets
    - statefulsets
    - daemonsets
  verbs: ["get", "list", "watch"]
- apiGroups: ["batch"]
  resources:
    - jobs
    - cronjobs
  verbs: ["get", "list", "watch"]
- apiGroups: ["metrics.k8s.io"]
  resources:
    - pods
  verbs: ["get", "list"]
{{- end -}}

{{/* Namespaces for namespace-scoped RBAC; defaults to the release namespace. */}}
{{- define "pharos-sre-bot.k8sRbacNamespaces" -}}
{{- if .Values.kubernetesMcp.rbac.namespaces -}}
{{- toYaml .Values.kubernetesMcp.rbac.namespaces -}}
{{- else -}}
{{- toYaml (list .Release.Namespace) -}}
{{- end -}}
{{- end -}}
