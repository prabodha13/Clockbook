#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/clockbook-$STAMP.dump"

pg_dump --format=custom --no-owner --no-privileges --file="$OUT" "$DATABASE_URL"
echo "Backup created: $OUT"
