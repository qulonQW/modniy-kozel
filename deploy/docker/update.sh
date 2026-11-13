#!/usr/bin/env bash
# Deploy the latest code (run on the server in /opt/tovarka):
#   bash deploy/docker/update.sh
# Pull -> build (tests run inside the build; a failure keeps the old version
# running) -> database backup -> restart the app.
set -euo pipefail
cd "$(dirname "$0")/../.."

before=$(git rev-parse --short HEAD)
git pull -q --ff-only
after=$(git rev-parse --short HEAD)
echo "code: $before -> $after"

docker compose build app || { echo "build/tests failed: old version still running. Roll back: git checkout $before"; exit 1; }
docker compose exec -T app python /app/tools/backup.py --db-only
docker compose up -d
docker image prune -f >/dev/null
docker compose ps
