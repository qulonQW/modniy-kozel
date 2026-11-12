#!/usr/bin/env bash
# Deploy the latest code from GitHub (run as root on the server):
#   sudo bash /opt/tovarka/deploy/update.sh
# Pulls main, installs requirements, runs the tests, backs up the database,
# restarts. Nothing is restarted if the tests fail. Data is never touched.
set -euo pipefail
APP_DIR=/opt/tovarka
BRANCH="${BRANCH:-main}"
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }

before=$(git -C "$APP_DIR" rev-parse --short HEAD)
git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH"
after=$(git -C "$APP_DIR" rev-parse --short HEAD)
echo "code: $before -> $after"

"$APP_DIR/backend/.venv/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"
(cd "$APP_DIR/backend" && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider) \
  || { echo "tests failed: still running the old version. Roll back: git -C $APP_DIR checkout $before"; exit 1; }

# systemd units may have changed too
install -m 644 "$APP_DIR"/deploy/tovarka.service "$APP_DIR"/deploy/tovarka-backup.service \
  "$APP_DIR"/deploy/tovarka-backup.timer /etc/systemd/system/
systemctl daemon-reload

sudo -u tovarka PYTHONDONTWRITEBYTECODE=1 "$APP_DIR/backend/.venv/bin/python" "$APP_DIR/tools/backup.py" --db-only
systemctl restart tovarka
sleep 2
systemctl is-active --quiet tovarka && echo "running $after" || { journalctl -u tovarka -n 30 --no-pager; exit 1; }
