#!/bin/sh
# Persist SQLite + the exported dashboard JSON on /data (PVC in Kubernetes).
set -eu

mkdir -p /data

# The static dashboard reads web/opportunities.json. Point that path at the
# volume so a CronJob export is visible to the running server without a rebuild.
if [ ! -e /app/web/opportunities.json ] || [ -L /app/web/opportunities.json ]; then
    if [ ! -f /data/opportunities.json ]; then
        printf '%s\n' '{"generated_at":null,"opportunities":[],"stale_sources":[]}' \
            > /data/opportunities.json
    fi
    ln -sfn /data/opportunities.json /app/web/opportunities.json
fi

export GRANT_SIFT_DB="${GRANT_SIFT_DB:-/data/grant-sift.db}"

if [ ! -f "${GRANT_SIFT_ROSTER:-/app/config/roster.yaml}" ]; then
    echo "warning: no roster at ${GRANT_SIFT_ROSTER:-/app/config/roster.yaml};" \
         "assess needs config/roster.yaml (see config/roster.example.yaml)" >&2
fi

exec "$@"
