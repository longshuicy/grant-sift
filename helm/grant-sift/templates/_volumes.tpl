{{- define "grant-sift.volumeMounts" -}}
{{- if .Values.persistence.enabled }}
- name: data
  mountPath: /data
{{- end }}
{{- if .Values.roster.existingConfigMap }}
- name: roster
  mountPath: {{ .Values.roster.mountPath | quote }}
  subPath: {{ .Values.roster.key | quote }}
  readOnly: true
{{- end }}
{{- end }}

{{- define "grant-sift.volumes" -}}
{{- if .Values.persistence.enabled }}
- name: data
  persistentVolumeClaim:
    claimName: {{ include "grant-sift.pvcName" . }}
{{- end }}
{{- if .Values.roster.existingConfigMap }}
- name: roster
  configMap:
    name: {{ .Values.roster.existingConfigMap | quote }}
{{- end }}
{{- end }}
