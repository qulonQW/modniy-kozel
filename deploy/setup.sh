#!/usr/bin/env bash
# First-time server setup for Ubuntu 24.04 / Debian 12+ (run as root).
#
#   sudo bash setup.sh <host name>
#   e.g. sudo bash setup.sh 203-0-113-7.sslip.io      (free name for IP 203.0.113.7)
#
# Safe to run again: existing code, .env and data are kept.
# It does NOT start the shop until backend/.env is filled in.
#
# Layout:
#   /opt/tovarka            code (git clone, read-only for the app)
#   /opt/tovarka/backend/.env   secrets (root:tovarka, 640)
#   /var/lib/tovarka/data   SQLite database + its backups
#   /var/lib/tovarka/uploads    product photos + thumbnails
set -euo pipefail

HOST="${1:?usage: sudo bash setup.sh <host name, e.g. 203-0-113-7.sslip.io>}"
REPO="${REPO:-https://github.com/qulonQW/modniy-kozel.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/tovarka
DATA_DIR=/var/lib/tovarka
APP_USER=tovarka

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
[[ "$HOST" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "bad host name: $HOST"; exit 1; }

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv git caddy ufw sqlite3

echo "== user and folders"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$DATA_DIR" "$DATA_DIR/data" "$DATA_DIR/uploads"

echo "== code"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -C "$APP_DIR" checkout -q "$BRANCH"
  git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO" "$APP_DIR" || {
    echo "git clone failed. If the GitHub repository is private, see RUNBOOK 'Deploy' (deploy key)."
    exit 1
  }
fi

echo "== python environment"
python3 -m venv "$APP_DIR/backend/.venv"
"$APP_DIR/backend/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/backend/.venv/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"

echo "== tests on this server's Python ($(python3 --version))"
(cd "$APP_DIR/backend" && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider) \
  || { echo "tests failed: not installing the service"; exit 1; }

echo "== .env"
ENV_FILE="$APP_DIR/backend/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<EOF
# Filled by deploy/setup.sh. Fill in BOT_TOKEN and ADMIN_TELEGRAM_IDS, then:
#   sudo systemctl restart tovarka
ENV=prod
BRAND=${BRAND:-modniy-kozel}
BOT_TOKEN=
ADMIN_TELEGRAM_IDS=
DEV_ADMIN_ID=
ALLOWED_ORIGIN=https://$HOST
DATABASE_PATH=$DATA_DIR/data/shop.sqlite3
UPLOADS_DIR=$DATA_DIR/uploads
EOF
  echo "created $ENV_FILE (BOT_TOKEN and ADMIN_TELEGRAM_IDS still empty)"
else
  echo "kept existing $ENV_FILE"
fi
chown root:"$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

echo "== systemd"
install -m 644 "$APP_DIR/deploy/tovarka.service" /etc/systemd/system/tovarka.service
install -m 644 "$APP_DIR/deploy/tovarka-backup.service" /etc/systemd/system/tovarka-backup.service
install -m 644 "$APP_DIR/deploy/tovarka-backup.timer" /etc/systemd/system/tovarka-backup.timer
systemctl daemon-reload
systemctl enable -q tovarka.service tovarka-backup.timer
systemctl start tovarka-backup.timer

echo "== caddy (HTTPS for $HOST)"
sed "s/SHOP_HOST/$HOST/" "$APP_DIR/deploy/Caddyfile.template" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
systemctl reload-or-restart caddy

echo "== firewall: SSH, HTTP, HTTPS only"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

if grep -qE '^BOT_TOKEN=.+' "$ENV_FILE" && grep -qE '^ADMIN_TELEGRAM_IDS=.+' "$ENV_FILE"; then
  systemctl restart tovarka
  sleep 2
  systemctl --no-pager --lines=5 status tovarka || true
else
  echo
  echo "Next: sudo nano $ENV_FILE   (BOT_TOKEN, ADMIN_TELEGRAM_IDS)"
  echo "      copy your data (RUNBOOK 'Deploy' step 4), then: sudo systemctl restart tovarka"
fi
echo "Shop URL: https://$HOST   admin: https://$HOST/admin/"
