#!/usr/bin/env bash
# Copy a local grant-sift.db (and optional opportunities.json) into the
# Kubernetes PVC used by the Helm release.
#
# Usage:
#   ./scripts/k8s-transfer-db.sh [local.db] [namespace] [release]
#
# Defaults: ./grant-sift.db, namespace=default, release=grant-sift
set -euo pipefail

LOCAL_DB="${1:-grant-sift.db}"
NAMESPACE="${2:-default}"
RELEASE="${3:-grant-sift}"
REMOTE_DB="/data/grant-sift.db"
REMOTE_JSON="/data/opportunities.json"

if [[ ! -f "$LOCAL_DB" ]]; then
  echo "missing local database: $LOCAL_DB" >&2
  exit 1
fi

POD="$(kubectl -n "$NAMESPACE" get pods -l "app.kubernetes.io/name=grant-sift,app.kubernetes.io/instance=${RELEASE}" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"

if [[ -z "$POD" ]]; then
  # fullnameOverride=grant-sift → labels may use instance = release name
  POD="$(kubectl -n "$NAMESPACE" get pods -l "app.kubernetes.io/name=grant-sift" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi

if [[ -z "$POD" ]]; then
  echo "no grant-sift pod found in namespace $NAMESPACE" >&2
  exit 1
fi

echo "using pod $POD in $NAMESPACE"

# Checkpoint WAL into the main file so the copy is consistent.
if command -v sqlite3 >/dev/null 2>&1; then
  echo "checkpointing WAL on local copy..."
  sqlite3 "$LOCAL_DB" "PRAGMA wal_checkpoint(TRUNCATE);"
fi

TMP="$(mktemp)"
cp "$LOCAL_DB" "$TMP"

echo "stopping writes: scaling deployment to 0"
kubectl -n "$NAMESPACE" scale deploy/"$RELEASE" --replicas=0
kubectl -n "$NAMESPACE" wait --for=delete pod/"$POD" --timeout=120s 2>/dev/null || true

# Start a short-lived pod that mounts the same PVC to receive the file.
CLAIM="$(kubectl -n "$NAMESPACE" get pvc -l "app.kubernetes.io/name=grant-sift" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "${RELEASE}-data")"

JOB_POD="grant-sift-db-load-$$"
cleanup() {
  kubectl -n "$NAMESPACE" delete pod "$JOB_POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  rm -f "$TMP"
}
trap cleanup EXIT

cat <<EOF | kubectl -n "$NAMESPACE" apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${JOB_POD}
  labels:
    app.kubernetes.io/name: grant-sift-db-load
spec:
  restartPolicy: Never
  containers:
    - name: load
      image: busybox:1.36
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: ${CLAIM}
EOF

kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${JOB_POD}" --timeout=120s

echo "copying database -> ${JOB_POD}:${REMOTE_DB}"
kubectl -n "$NAMESPACE" cp "$TMP" "${JOB_POD}:${REMOTE_DB}"

LOCAL_JSON="$(dirname "$LOCAL_DB")/web/opportunities.json"
if [[ ! -f "$LOCAL_JSON" ]]; then
  LOCAL_JSON="web/opportunities.json"
fi
if [[ -f "$LOCAL_JSON" ]]; then
  echo "copying opportunities.json -> ${JOB_POD}:${REMOTE_JSON}"
  kubectl -n "$NAMESPACE" cp "$LOCAL_JSON" "${JOB_POD}:${REMOTE_JSON}"
else
  echo "no local opportunities.json; run: python run.py export"
fi

echo "scaling deployment back to 1"
kubectl -n "$NAMESPACE" scale deploy/"$RELEASE" --replicas=1
kubectl -n "$NAMESPACE" rollout status deploy/"$RELEASE" --timeout=180s

echo "done. Verify with:"
echo "  kubectl -n $NAMESPACE exec deploy/$RELEASE -- python run.py status"
