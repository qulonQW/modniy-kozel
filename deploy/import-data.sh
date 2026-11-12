#!/usr/bin/env bash
# Move the local shop data onto the server ONCE (run as root):
#   sudo bash /opt/tovarka/deploy/import-data.sh /root/shop-data.tar.gz
# The archive comes from tools/backup.py on your computer (see RUNBOOK):
#   shop.sqlite3 + uploads/ at its top level.
# Refuses to overwrite a database that is already on the server.
set -euo pipefail
ARCHIVE="${1:?usage: import-data.sh <archive.tar.gz>}"
DATA_DIR=/var/lib/tovarka
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
[[ -f "$ARCHIVE" ]] || { echo "not found: $ARCHIVE"; exit 1; }
if [[ -f "$DATA_DIR/data/shop.sqlite3" ]]; then
  echo "$DATA_DIR/data/shop.sqlite3 already exists: nothing changed."
  echo "(To replace it on purpose: stop the service and move the old file away first.)"
  exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
tar -xzf "$ARCHIVE" -C "$tmp"
[[ -f "$tmp/shop.sqlite3" ]] || { echo "archive has no shop.sqlite3 at its top level"; exit 1; }
sqlite3 "$tmp/shop.sqlite3" "PRAGMA integrity_check;" | grep -qx ok || { echo "database integrity check failed"; exit 1; }

systemctl stop tovarka 2>/dev/null || true
install -o tovarka -g tovarka -m 640 "$tmp/shop.sqlite3" "$DATA_DIR/data/shop.sqlite3"
if [[ -d "$tmp/uploads" ]]; then
  cp -a "$tmp/uploads/." "$DATA_DIR/uploads/"
fi
chown -R tovarka:tovarka "$DATA_DIR"
echo "products: $(sqlite3 "$DATA_DIR/data/shop.sqlite3" "SELECT status || '=' || COUNT(*) FROM products GROUP BY status;" | tr '\n' ' ')"
echo "photos:   $(find "$DATA_DIR/uploads/products" -type f | wc -l), thumbnails: $(find "$DATA_DIR/uploads/thumbs" -type f 2>/dev/null | wc -l)"
systemctl start tovarka
echo "started. Delete the archive when everything works: rm $ARCHIVE"
