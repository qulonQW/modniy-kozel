"""SQLite access through the stdlib sqlite3 module.

Tables are created on startup (no migration tool). Every query in the app is
parameterized; never build SQL from user input with string formatting.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id                TEXT PRIMARY KEY
                      CHECK (length(id) BETWEEN 1 AND 64),
    title             TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    description       TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
    category          TEXT NOT NULL CHECK (category IN ('Взуття', 'Одяг')),
    price_retail      INTEGER NOT NULL CHECK (price_retail >= 0),
    price_supplier    INTEGER CHECK (price_supplier >= 0),
    sizes             TEXT NOT NULL DEFAULT '[]',
    in_stock          INTEGER NOT NULL DEFAULT 1 CHECK (in_stock IN (0, 1)),
    popular           INTEGER NOT NULL DEFAULT 0 CHECK (popular IN (0, 1)),
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'published', 'hidden')),
    edited_by_admin   INTEGER NOT NULL DEFAULT 0 CHECK (edited_by_admin IN (0, 1)),
    needs_review      INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
    review_reasons    TEXT NOT NULL DEFAULT '',
    price_reference   INTEGER CHECK (price_reference >= 0),
    duplicate_group   TEXT CHECK (duplicate_group IS NULL OR length(duplicate_group) BETWEEN 1 AND 64),
    search_text       TEXT NOT NULL DEFAULT '',
    source_channel    TEXT,
    source_message_id INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_status ON products (status);

CREATE TABLE IF NOT EXISTS product_images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    position   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_images_product
    ON product_images (product_id, position);
"""


_APOSTROPHES = re.compile(r"[’ʼ`´‘]")


def search_key(text: str) -> str:
    """Normalized form for storefront search. SQLite LOWER/LIKE fold ASCII
    only, so names are stored casefolded in products.search_text and the
    query is normalized the same way: casefold, ё -> е, one apostrophe,
    single spaces."""
    text = _APOSTROPHES.sub("'", text.casefold()).replace("ё", "е")
    return " ".join(text.split())


def backfill_search_text(conn: sqlite3.Connection) -> int:
    """Sets search_text for rows whose value is missing or stale."""
    stale = [
        (search_key(r["title"]), r["id"])
        for r in conn.execute("SELECT id, title, search_text FROM products")
        if r["search_text"] != search_key(r["title"])
    ]
    conn.executemany("UPDATE products SET search_text = ? WHERE id = ?", stale)
    return len(stale)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    # SQLite's LOWER/LIKE only fold ASCII; this handles Cyrillic for admin search.
    conn.create_function(
        "casefold", 1, lambda s: s.casefold() if isinstance(s, str) else s, deterministic=True
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after the first release: (name, definition). init_db adds any
# that an existing database is missing, so no migration tool is needed.
ADDED_COLUMNS = {
    "products": [
        ("needs_review", "INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1))"),
        ("review_reasons", "TEXT NOT NULL DEFAULT ''"),
        ("price_reference", "INTEGER CHECK (price_reference >= 0)"),
        (
            "duplicate_group",
            "TEXT CHECK (duplicate_group IS NULL OR length(duplicate_group) BETWEEN 1 AND 64)",
        ),
        ("search_text", "TEXT NOT NULL DEFAULT ''"),
    ],
}


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, columns in ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    # After the migration: older databases get the column only in the loop above.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_duplicate_group ON products (duplicate_group)"
    )
    backfill_search_text(conn)
    conn.commit()
