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
    position   INTEGER NOT NULL,
    thumb_path TEXT  -- WebP card thumbnail (app/thumbs.py); NULL -> use path
);

CREATE INDEX IF NOT EXISTS idx_product_images_product
    ON product_images (product_id, position);

-- Shopper favorites. user_telegram_id always comes from verified initData.
CREATE TABLE IF NOT EXISTS favorites (
    user_telegram_id INTEGER NOT NULL CHECK (user_telegram_id > 0),
    product_id       TEXT NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (user_telegram_id, product_id)
);

-- Orders. Prices and the total are computed on the server from products;
-- title/price snapshots keep the order readable after product edits.
CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_telegram_id INTEGER NOT NULL CHECK (user_telegram_id > 0),
    username         TEXT CHECK (username IS NULL OR length(username) <= 32),
    first_name       TEXT CHECK (first_name IS NULL OR length(first_name) <= 64),
    customer_name    TEXT NOT NULL CHECK (length(customer_name) BETWEEN 2 AND 100),
    phone            TEXT NOT NULL CHECK (length(phone) = 13),
    city             TEXT NOT NULL CHECK (length(city) BETWEEN 2 AND 100),
    delivery_point   TEXT NOT NULL CHECK (length(delivery_point) BETWEEN 1 AND 200),
    comment          TEXT NOT NULL DEFAULT '' CHECK (length(comment) <= 500),
    status           TEXT NOT NULL DEFAULT 'new'
                     CHECK (status IN ('new', 'confirmed', 'cancelled')),
    total            INTEGER NOT NULL CHECK (total >= 0),
    notified         INTEGER NOT NULL DEFAULT 0 CHECK (notified IN (0, 1)),
    notify_attempts  INTEGER NOT NULL DEFAULT 0 CHECK (notify_attempts >= 0),
    idempotency_key  TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 16 AND 64),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (user_telegram_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_orders_user ON orders (user_telegram_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status, created_at);

CREATE TABLE IF NOT EXISTS order_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id     TEXT NOT NULL,  -- no FK: the order outlives a deleted product
    title_snapshot TEXT NOT NULL,
    size           TEXT NOT NULL CHECK (length(size) <= 10),
    qty            INTEGER NOT NULL CHECK (qty BETWEEN 1 AND 10),
    price_snapshot INTEGER NOT NULL CHECK (price_snapshot >= 0)
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items (order_id);

-- Bot notification of one order to one admin. orders.notified is 1 once every
-- row of the order is 'sent'; a failure for one admin never blocks the others.
CREATE TABLE IF NOT EXISTS order_notifications (
    order_id        INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    admin_id        INTEGER NOT NULL CHECK (admin_id > 0),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT CHECK (last_error IS NULL OR length(last_error) <= 40),
    sent_at         TEXT,
    PRIMARY KEY (order_id, admin_id)
);

CREATE INDEX IF NOT EXISTS idx_order_notifications_due
    ON order_notifications (status, next_attempt_at);
"""

# Tables created after the first release. An existing database that lacks one
# of them (or an ADDED_COLUMNS column) is backed up before init_db changes it.
ADDED_TABLES = ("favorites", "orders", "order_items", "order_notifications")


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
    "product_images": [
        ("thumb_path", "TEXT"),
    ],
}


def _pending_migration(conn: sqlite3.Connection) -> bool:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "products" not in tables:
        return False  # new, empty database: nothing to protect
    if any(t not in tables for t in ADDED_TABLES):
        return True
    for table, columns in ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if any(name not in existing for name, _ in columns):
            return True
    return False


def backup_database(conn: sqlite3.Connection, directory: Path | None = None) -> Path | None:
    """Consistent copy (SQLite backup API, safe with WAL) next to the database
    as backup-<UTC timestamp>.sqlite3. Returns None for in-memory databases."""
    file = conn.execute("PRAGMA database_list").fetchone()[2]
    if not file:
        return None
    directory = directory or Path(file).parent
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = directory / f"backup-{stamp}.sqlite3"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def init_db(conn: sqlite3.Connection) -> None:
    """Creates missing tables/columns/indexes. Additive only: existing rows are
    never changed (except the derived search_text). Backs up first if an
    existing database is about to get new tables or columns."""
    if _pending_migration(conn):
        backup_database(conn)
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
