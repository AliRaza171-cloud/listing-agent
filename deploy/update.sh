#!/usr/bin/env bash
# Pull the latest code and (re)start everything. Run from anywhere on the server:
#   bash ~/listing-agent/deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps
