#!/usr/bin/env bash
# Nightly backup of all databases and product photos into ~/backups (keeps 7 days).
# Install once:  (crontab -l 2>/dev/null; echo "30 3 * * * bash $HOME/listing-agent/deploy/backup.sh") | crontab -
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="$HOME/backups"; mkdir -p "$DEST"
STAMP=$(date +%Y%m%d-%H%M)
C="docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml"
$C exec -T postgres pg_dumpall -U lagent | gzip > "$DEST/db-$STAMP.sql.gz"
$C exec -T catalog tar -C /data -czf - media > "$DEST/media-$STAMP.tar.gz"
find "$DEST" -name '*.gz' -mtime +7 -delete
echo "backup ok: $DEST/*-$STAMP.*"
