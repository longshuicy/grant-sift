#!/bin/sh
# Persist SQLite + the exported dashboard JSON on /data (PVC in Kubernetes).
#
# Layout (one RWX PVC mounted at /data on both Deployment and CronJob):
#   /data/grant-sift.db          ← SQLite
#   /data/opportunities.json     ← nightly export; serve reads this same file
#   /app/web/opportunities.json  ← symlink → /data/... so `run.py export` default
#                                   path also hits the volume
set -eu

mkdir -p /data

# The static dashboard / FileResponse prefer the PVC path; keep the symlink so
# local-style `python run.py export` (default web/opportunities.json) updates
# the volume without a CronJob-specific --out flag.
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
