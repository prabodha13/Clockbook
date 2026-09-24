#!/usr/bin/env bash
set -euo pipefail

: "${RESTORE_DATABASE_URL:?RESTORE_DATABASE_URL must point to a NON-PRODUCTION restore target}"
: "${1:?Usage: restore_postgres.sh path/to/backup.dump}"

case "$RESTORE_DATABASE_URL" in
  *production*|*prod*)
    echo "Refusing to restore to a URL that appears to be production." >&2
    exit 2
    ;;
esac

pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$RESTORE_DATABASE_URL" "$1"
echo "Restore completed. Run application health checks and regression tests against the restored database."
