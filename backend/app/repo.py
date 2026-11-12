"""Product queries (parameterized only) and row -> response model mapping."""

from __future__ import annotations

import json
import sqlite3

from .db import search_key, utc_now
from .schemas import AdminImage, AdminProduct, DuplicateMember, ProductCreate, PublicProduct

# Newest first; rows created in the same second keep insertion order.
ORDER_BY = "ORDER BY p.created_at DESC, p.rowid ASC"


def image_url(path: str) -> str:
    """Stored paths are relative ("uploads/products/x.webp"); the API serves
    them root-relative to the backend, and the storefront resolves them
    against its API_BASE_URL."""
    return "/" + path


def images_by_product(conn: sqlite3.Connection, ids: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {pid: [] for pid in ids}
    if not ids:
        return result
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT product_id, path FROM product_images WHERE product_id IN ({placeholders}) "
        "ORDER BY product_id, position, id",
        ids,
    )
    for row in rows:
        result[row["product_id"]].append(row["path"])
    return result


def to_public(row: sqlite3.Row, image_paths: list[str]) -> PublicProduct:
    urls = [image_url(p) for p in image_paths]
    return PublicProduct(
        id=row["id"],
        name=row["title"],
        description=row["description"],
        category=row["category"],
        price=row["price_retail"],
        sizes=json.loads(row["sizes"]),
        inStock=bool(row["in_stock"]),
        popular=bool(row["popular"]),
        imageUrl=urls[0] if urls else None,
        images=urls,
    )


def list_published(conn: sqlite3.Connection) -> list[PublicProduct]:
    rows = conn.execute(f"SELECT * FROM products p WHERE p.status = 'published' {ORDER_BY}").fetchall()
    images = images_by_product(conn, [r["id"] for r in rows])
    return [to_public(r, images[r["id"]]) for r in rows]


# Sort keys are fixed SQL fragments chosen by name; user input never reaches
# the ORDER BY text. In-stock items always come first; p.id is unique, so the
# order is total and offset pagination never skips or repeats a row.
CATALOG_SORTS = {
    "popular": "p.popular DESC, p.created_at DESC",
    "newest": "p.created_at DESC",
    "price_asc": "p.price_retail ASC",
    "price_desc": "p.price_retail DESC",
}


def _escape_like_backslash(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def catalog(
    conn: sqlite3.Connection,
    q: str | None,
    category: str | None,
    sort: str,
    limit: int,
    offset: int,
) -> tuple[list[PublicProduct], int]:
    where, params = ["p.status = 'published'"], []
    if q:
        where.append("p.search_text LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like_backslash(search_key(q))}%")
    if category:
        where.append("p.category = ?")
        params.append(category)
    clause = " AND ".join(where)
    order = f"p.in_stock DESC, {CATALOG_SORTS[sort]}, p.id ASC"
    total = conn.execute(f"SELECT COUNT(*) FROM products p WHERE {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM products p WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    images = images_by_product(conn, [r["id"] for r in rows])
    return [to_public(r, images[r["id"]]) for r in rows], total


def get_published(conn: sqlite3.Connection, product_id: str) -> PublicProduct | None:
    row = conn.execute(
        "SELECT * FROM products WHERE id = ? AND status = 'published'", (product_id,)
    ).fetchone()
    if row is None:
        return None
    return to_public(row, images_by_product(conn, [product_id])[product_id])


# --- admin -----------------------------------------------------------------

_ADMIN_COLUMNS = (
    "title", "description", "category", "price_retail", "price_supplier",
    "sizes", "in_stock", "popular", "status",
)


def _escape_like(text: str) -> str:
    """Escapes LIKE wildcards using '!' as the ESCAPE character."""
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def admin_images(conn: sqlite3.Connection, product_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, path, position FROM product_images WHERE product_id = ? ORDER BY position, id",
        (product_id,),
    ).fetchall()


def _admin_images_many(conn: sqlite3.Connection, ids: list[str]) -> dict[str, list[sqlite3.Row]]:
    result: dict[str, list[sqlite3.Row]] = {pid: [] for pid in ids}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in conn.execute(
            f"SELECT id, product_id, path, position FROM product_images "
            f"WHERE product_id IN ({marks}) ORDER BY position, id",
            ids,
        ):
            result[r["product_id"]].append(r)
    return result


# A product is in a duplicate group if it points to a main product or if
# other products point to it.
IN_DUPLICATE_GROUP_SQL = (
    "(p.duplicate_group IS NOT NULL OR EXISTS "
    "(SELECT 1 FROM products d WHERE d.duplicate_group = p.id))"
)


def _group_keys(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, str]:
    """id -> duplicate group key for the given rows."""
    keys = {r["id"]: r["duplicate_group"] for r in rows if r["duplicate_group"]}
    ids = [r["id"] for r in rows if r["id"] not in keys]
    if ids:
        marks = ",".join("?" * len(ids))
        for r in conn.execute(
            f"SELECT DISTINCT duplicate_group FROM products WHERE duplicate_group IN ({marks})", ids
        ):
            keys[r[0]] = r[0]
    return keys


def duplicate_members(conn: sqlite3.Connection, product_id: str, key: str) -> list[DuplicateMember]:
    rows = conn.execute(
        "SELECT id, title, status FROM products WHERE (id = ? OR duplicate_group = ?) AND id != ? "
        "ORDER BY id = ? DESC, created_at, id",
        (key, key, product_id, key),
    ).fetchall()
    images = _admin_images_many(conn, [r["id"] for r in rows])
    return [
        DuplicateMember(
            id=r["id"],
            title=r["title"],
            status=r["status"],
            is_main=r["id"] == key,
            image_url=image_url(images[r["id"]][0]["path"]) if images[r["id"]] else None,
            image_count=len(images[r["id"]]),
        )
        for r in rows
    ]


def to_admin(
    row: sqlite3.Row,
    images: list[sqlite3.Row],
    group: str | None = None,
    duplicates: list[DuplicateMember] | None = None,
) -> AdminProduct:
    return AdminProduct(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        category=row["category"],
        price_retail=row["price_retail"],
        price_supplier=row["price_supplier"],
        price_reference=row["price_reference"],
        sizes=json.loads(row["sizes"]),
        in_stock=bool(row["in_stock"]),
        popular=bool(row["popular"]),
        status=row["status"],
        edited_by_admin=bool(row["edited_by_admin"]),
        needs_review=bool(row["needs_review"]),
        review_reasons=[r for r in row["review_reasons"].split(",") if r],
        source_channel=row["source_channel"],
        source_message_id=row["source_message_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        images=[AdminImage(id=i["id"], url=image_url(i["path"]), position=i["position"]) for i in images],
        duplicate_group=group,
        duplicates=duplicates or [],
    )


def admin_list(
    conn: sqlite3.Connection,
    status: str | None,
    category: str | None,
    search: str | None,
    limit: int,
    offset: int,
    needs_review: bool | None = None,
    duplicates: bool = False,
) -> tuple[list[AdminProduct], int]:
    where, params = [], []
    order = ORDER_BY
    if duplicates:
        where.append(IN_DUPLICATE_GROUP_SQL)
        # members of one group next to each other, main product first
        order = (
            "ORDER BY COALESCE(p.duplicate_group, p.id), p.duplicate_group IS NOT NULL, "
            "p.created_at, p.rowid"
        )
    if needs_review is not None:
        where.append("p.needs_review = ?")
        params.append(int(needs_review))
    if status:
        where.append("p.status = ?")
        params.append(status)
    if category:
        where.append("p.category = ?")
        params.append(category)
    if search:
        pattern = f"%{_escape_like(search.casefold())}%"
        where.append(
            "(casefold(p.title) LIKE ? ESCAPE '!' OR casefold(p.description) LIKE ? ESCAPE '!' "
            "OR p.id LIKE ? ESCAPE '!')"
        )
        params += [pattern, pattern, pattern]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM products p {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM products p {clause} {order} LIMIT ? OFFSET ?", [*params, limit, offset]
    ).fetchall()
    images = _admin_images_many(conn, [r["id"] for r in rows])
    groups = _group_keys(conn, rows)
    return [to_admin(r, images[r["id"]], groups.get(r["id"])) for r in rows], total


def admin_get(conn: sqlite3.Connection, product_id: str) -> AdminProduct | None:
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        return None
    group = _group_keys(conn, [row]).get(product_id)
    members = duplicate_members(conn, product_id, group) if group else []
    return to_admin(row, admin_images(conn, product_id), group, members)


def _db_value(name: str, value):
    if name == "sizes":
        return json.dumps(value, ensure_ascii=False)
    if name in ("in_stock", "popular"):
        return int(value)
    return value


def admin_create(conn: sqlite3.Connection, product_id: str, data: ProductCreate) -> None:
    now = utc_now()
    values = [_db_value(c, getattr(data, c)) for c in _ADMIN_COLUMNS]
    cols = ", ".join(_ADMIN_COLUMNS)
    marks = ", ".join("?" * (len(_ADMIN_COLUMNS) + 4))
    conn.execute(
        f"INSERT INTO products (id, {cols}, search_text, edited_by_admin, created_at, updated_at) "
        f"VALUES ({marks}, ?)",
        (product_id, *values, search_key(data.title), 1, now, now),
    )


def touch(conn: sqlite3.Connection, product_id: str) -> None:
    conn.execute(
        "UPDATE products SET edited_by_admin = 1, updated_at = ? WHERE id = ?",
        (utc_now(), product_id),
    )


class PublishBlocked(Exception):
    """Publishing requires needs_review = 0 and price_retail > 0."""


PUBLISHABLE_SQL = "needs_review = 0 AND price_retail > 0"


def admin_update(conn: sqlite3.Connection, product_id: str, changes: dict) -> bool:
    """Applies a partial admin edit. Returns False if the product is missing.

    A content edit (anything besides status) that leaves price_retail > 0
    counts as a review and clears needs_review, as does an explicit
    {"needs_review": False}. A status-only change never clears it, so
    "publish" alone cannot skip the review.
    Raises PublishBlocked if the result would be published but not publishable,
    or if the review is confirmed while price_retail is still 0.
    """
    changes = dict(changes)
    confirm_review = changes.pop("needs_review", None) is False
    if not set(changes) <= set(_ADMIN_COLUMNS):  # column names go into SQL text
        raise ValueError("unknown column")
    row = conn.execute(
        "SELECT status, price_retail, needs_review FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if row is None:
        return False

    new_price = changes.get("price_retail", row["price_retail"])
    new_status = changes.get("status", row["status"])
    if confirm_review and new_price <= 0:
        raise PublishBlocked
    clears_review = (confirm_review or bool(set(changes) - {"status"})) and new_price > 0
    new_needs_review = 0 if clears_review else row["needs_review"]
    if new_status == "published" and (new_needs_review or new_price <= 0):
        raise PublishBlocked

    updates = {c: _db_value(c, v) for c, v in changes.items()}
    if "title" in changes:
        updates["search_text"] = search_key(changes["title"])
    if clears_review:
        updates.update(needs_review=0, review_reasons="")
    if confirm_review:
        # "Checked" also answers "possible duplicate": it is not one.
        updates.update(duplicate_group=None)
    if updates:
        assignments = ", ".join(f"{c} = ?" for c in updates)
        conn.execute(
            f"UPDATE products SET {assignments} WHERE id = ?", (*updates.values(), product_id)
        )
    touch(conn, product_id)
    return True


def bulk_set_status(conn: sqlite3.Connection, ids: list[str], status: str) -> tuple[int, list[str]]:
    """Returns (updated count, ids that exist but cannot be published)."""
    marks = ",".join("?" * len(ids))
    blocked: list[str] = []
    guard = ""
    if status == "published":
        blocked = [
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM products WHERE id IN ({marks}) AND NOT ({PUBLISHABLE_SQL})", ids
            )
        ]
        guard = f" AND {PUBLISHABLE_SQL}"
    updated = conn.execute(
        f"UPDATE products SET status = ?, edited_by_admin = 1, updated_at = ? "
        f"WHERE id IN ({marks}){guard}",
        (status, utc_now(), *ids),
    ).rowcount
    return updated, sorted(blocked)


def renumber_images(conn: sqlite3.Connection, product_id: str, ordered_ids: list[int]) -> None:
    conn.executemany(
        "UPDATE product_images SET position = ? WHERE id = ? AND product_id = ?",
        [(pos, image_id, product_id) for pos, image_id in enumerate(ordered_ids)],
    )


# --- favorites ----------------------------------------------------------------

MAX_FAVORITES = 200


class FavoritesFull(Exception):
    """The user already has MAX_FAVORITES favorites."""


def list_favorites(conn: sqlite3.Connection, user_id: int) -> list[PublicProduct]:
    """The user's favorites that are still published, newest favorite first."""
    rows = conn.execute(
        "SELECT p.* FROM favorites f JOIN products p ON p.id = f.product_id "
        "WHERE f.user_telegram_id = ? AND p.status = 'published' "
        "ORDER BY f.created_at DESC, f.rowid DESC",
        (user_id,),
    ).fetchall()
    images = images_by_product(conn, [r["id"] for r in rows])
    return [to_public(r, images[r["id"]]) for r in rows]


def add_favorite(conn: sqlite3.Connection, user_id: int, product_id: str) -> bool:
    """Idempotent. False if the product is not published. Raises FavoritesFull."""
    published = conn.execute(
        "SELECT 1 FROM products WHERE id = ? AND status = 'published'", (product_id,)
    ).fetchone()
    if published is None:
        return False
    exists = conn.execute(
        "SELECT 1 FROM favorites WHERE user_telegram_id = ? AND product_id = ?", (user_id, product_id)
    ).fetchone()
    if exists:
        return True
    count = conn.execute(
        "SELECT COUNT(*) FROM favorites WHERE user_telegram_id = ?", (user_id,)
    ).fetchone()[0]
    if count >= MAX_FAVORITES:
        raise FavoritesFull
    conn.execute(
        "INSERT INTO favorites (user_telegram_id, product_id, created_at) VALUES (?, ?, ?)",
        (user_id, product_id, utc_now()),
    )
    return True


def remove_favorite(conn: sqlite3.Connection, user_id: int, product_id: str) -> None:
    conn.execute(
        "DELETE FROM favorites WHERE user_telegram_id = ? AND product_id = ?", (user_id, product_id)
    )
