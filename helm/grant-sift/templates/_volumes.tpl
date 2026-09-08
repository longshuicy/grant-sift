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
{{- if .Values.roster.staffKey }}
- name: roster
  mountPath: {{ .Values.roster.staffMountPath | default "/app/config/ncsa_staff.yaml" | quote }}
  subPath: {{ .Values.roster.staffKey | quote }}
  readOnly: true
{{- end }}
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
