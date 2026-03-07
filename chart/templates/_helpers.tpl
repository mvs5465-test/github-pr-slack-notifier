{{- define "github-pr-slack-notifier.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "github-pr-slack-notifier.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "github-pr-slack-notifier.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
