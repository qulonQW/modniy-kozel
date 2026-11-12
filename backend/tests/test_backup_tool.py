"""tools/backup.py: timestamped copy of the database and uploads."""

from __future__ import annotations

import importlib.util
import sqlite3

import pytest

from app.config import REPO_DIR
from app.db import connect, init_db

spec = importlib.util.spec_from_file_location("backup_tool", REPO_DIR / "tools" / "backup.py")
backup_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_tool)


@pytest.fixture
def shop_files(tmp_path):
    db = tmp_path / "data" / "shop.sqlite3"
    conn = connect(db)
    init_db(conn)
    conn.execute(
        "INSERT INTO products (id, title, category, price_retail, created_at, updated_at) "
        "VALUES ('b-1', 'Backup', 'Одяг', 100, 'x', 'x')"
    )
    conn.commit()
    uploads = tmp_path / "uploads"
    (uploads / "products").mkdir(parents=True)
    (uploads / "thumbs").mkdir()
    (uploads / "products" / "a.jpg").write_bytes(b"original")
    (uploads / "thumbs" / "a.webp").write_bytes(b"thumb")
    yield db, uploads, conn
    conn.close()


def test_backup_copies_database_and_uploads(shop_files, tmp_path):
    db, uploads, conn = shop_files  # the connection stays open, like a running server
    target = backup_tool.backup(tmp_path / "backups", db, uploads, db_only=False, no_thumbs=False)
    copy = sqlite3.connect(target / "shop.sqlite3")
    assert copy.execute("SELECT id FROM products").fetchall() == [("b-1",)]
    copy.close()
    assert (target / "uploads" / "products" / "a.jpg").read_bytes() == b"original"
    assert (target / "uploads" / "thumbs" / "a.webp").exists()
    assert (uploads / "products" / "a.jpg").read_bytes() == b"original"  # source untouched


def test_backup_options(shop_files, tmp_path):
    db, uploads, _ = shop_files
    only_db = backup_tool.backup(tmp_path / "b1", db, uploads, db_only=True, no_thumbs=False)
    assert not (only_db / "uploads").exists()
    no_thumbs = backup_tool.backup(tmp_path / "b2", db, uploads, db_only=False, no_thumbs=True)
    assert (no_thumbs / "uploads" / "products" / "a.jpg").exists()
    assert not (no_thumbs / "uploads" / "thumbs").exists()


def test_backup_refuses_missing_database(tmp_path):
    with pytest.raises(SystemExit):
        backup_tool.backup(tmp_path / "b", tmp_path / "nope.sqlite3", tmp_path, db_only=True, no_thumbs=False)
