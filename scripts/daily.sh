#!/bin/sh
# Nightly run for cron or launchd.
#
# Exists because cron starts with almost no environment: no PATH to your
# python, and none of the GRANT_SIFT_* variables. It also refuses to overlap
# with itself, which matters here because a first assess pass can run for half
# an hour and cron does not care that the last one is still going.

set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# mkdir is atomic on every filesystem that matters, and unlike flock it exists
# on macOS. A stale lock after a crash is cleared by removing the directory.
LOCK="$REPO/.daily.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) another run holds $LOCK; skipping" >&2
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

# The key lives in .env, which is gitignored and never committed.
if [ -f "$REPO/.env" ]; then
    set -a
    . "$REPO/.env"
    set +a
fi

PY="${GRANT_SIFT_PYTHON:-python3}"

echo "=============================================================="
echo "$(date -u +%FT%TZ) starting daily"
"$PY" run.py daily --limit "${GRANT_SIFT_DAILY_LIMIT:-400}"
echo "$(date -u +%FT%TZ) finished; source health follows"
"$PY" run.py status
