#!/usr/bin/env python3
"""Timestamped backup of the shop: SQLite database + uploads folder.

    python tools/backup.py                     # -> backend/data/backups/<UTC time>/
    python tools/backup.py --dest D:/mk-backups
    python tools/backup.py --db-only           # database only (seconds, small)
    python tools/backup.py --no-thumbs         # skip uploads/thumbs (regenerate with
                                               #   python -m app.manage thumbnails)

Paths come from backend/.env the same way the server reads them
(DATABASE_PATH, UPLOADS_DIR; defaults backend/data/shop.sqlite3 and
backend/uploads). The database is copied with SQLite's backup API, so it is
consistent even while the server is running. Nothing is ever deleted or
overwritten: every run makes a new folder. Prints paths and sizes only.

Restore (server stopped): copy shop.sqlite3 back to DATABASE_PATH and the
uploads folder back to UPLOADS_DIR.

Stdlib only.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from app.config import load_settings  # noqa: E402


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def backup(dest_root: Path, db_path: Path, uploads: Path, db_only: bool, no_thumbs: bool) -> Path:
    if not db_path.is_file():
        raise SystemExit(f"database not found: {db_path}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = dest_root / stamp
    target.mkdir(parents=True, exist_ok=False)  # never reuse a folder

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(target / db_path.name)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"database: {target / db_path.name} ({(target / db_path.name).stat().st_size / 1e6:.1f} MB)")

    if not db_only and uploads.is_dir():
        ignore = shutil.ignore_patterns("thumbs") if no_thumbs else None
        shutil.copytree(uploads, target / "uploads", ignore=ignore)
        print(f"uploads:  {target / 'uploads'} ({folder_size(target / 'uploads') / 1e6:.1f} MB)")
    return target


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(prog="python tools/backup.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dest", type=Path, default=settings.database_path.parent / "backups",
                        help="folder for backups (default: next to the database, in backups/)")
    parser.add_argument("--db-only", action="store_true", help="skip the uploads folder")
    parser.add_argument("--no-thumbs", action="store_true", help="skip uploads/thumbs (can be regenerated)")
    args = parser.parse_args(argv)
    target = backup(args.dest, settings.database_path, settings.uploads_dir, args.db_only, args.no_thumbs)
    print(f"done: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
