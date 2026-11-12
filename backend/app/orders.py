"""Order creation and queries. Parameterized SQL only.

The server is the only source of prices: every line is re-priced from the
products table, and only published, in-stock products with an existing size
are accepted. Nothing the client sends about money is stored.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .auth import TelegramUser
from .db import utc_now
from .schemas import (
    MAX_LINE_QTY,
    AdminOrder,
    OrderCreate,
    OrderItemOut,
    OrderNotificationOut,
    OrderOut,
)


@dataclass
class CartProblem(Exception):
    """Some lines cannot be ordered. problems: [{product_id, size, reason}],
    reason in unavailable | out_of_stock | invalid_size | too_many."""

    problems: list[dict] = field(default_factory=list)


@dataclass
class PriceChanged(Exception):
    """expected_total differs from the server total; nothing was created."""

    total: int
    prices: dict[str, int]


def _merge_lines(body: OrderCreate) -> list[tuple[str, str, int]]:
    """Same product + size twice -> one line with the summed quantity."""
    merged: dict[tuple[str, str], int] = {}
    for item in body.items:
        key = (item.product_id, item.size)
        merged[key] = merged.get(key, 0) + item.qty
    return [(pid, size, qty) for (pid, size), qty in merged.items()]


def find_by_key(conn: sqlite3.Connection, user_id: int, key: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM orders WHERE user_telegram_id = ? AND idempotency_key = ?", (user_id, key)
    ).fetchone()
    return row["id"] if row else None


def create_order(
    conn: sqlite3.Connection, user: TelegramUser, body: OrderCreate, admin_ids: frozenset[int]
) -> tuple[int, bool]:
    """Returns (order id, created). The same idempotency key returns the
    existing order (created=False). Raises CartProblem / PriceChanged."""
    existing = find_by_key(conn, user.id, body.idempotency_key)
    if existing is not None:
        return existing, False

    lines = _merge_lines(body)
    ids = sorted({pid for pid, _, _ in lines})
    marks = ",".join("?" * len(ids))
    products = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT id, title, price_retail, sizes, in_stock, status FROM products WHERE id IN ({marks})",
            ids,
        )
    }

    problems, priced = [], []
    for pid, size, qty in lines:
        p = products.get(pid)
        reason = None
        if p is None or p["status"] != "published":
            reason = "unavailable"
        elif not p["in_stock"]:
            reason = "out_of_stock"
        else:
            sizes = json.loads(p["sizes"])
            if (size not in sizes) if sizes else size != "":
                reason = "invalid_size"
            elif qty > MAX_LINE_QTY:
                reason = "too_many"
        if reason:
            problems.append({"product_id": pid, "size": size, "reason": reason})
        else:
            priced.append((pid, p["title"], size, qty, p["price_retail"]))
    if problems:
        raise CartProblem(problems)

    total = sum(qty * price for _, _, _, qty, price in priced)
    if body.expected_total is not None and body.expected_total != total:
        raise PriceChanged(total, {pid: price for pid, _, _, _, price in priced})

    now = utc_now()
    try:
        with conn:
            order_id = conn.execute(
                "INSERT INTO orders (user_telegram_id, username, first_name, customer_name, phone, "
                "city, delivery_point, comment, status, total, idempotency_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)",
                (
                    user.id, user.username, user.first_name, body.customer_name, body.phone,
                    body.city, body.delivery_point, body.comment, total, body.idempotency_key,
                    now, now,
                ),
            ).lastrowid
            conn.executemany(
                "INSERT INTO order_items (order_id, product_id, title_snapshot, size, qty, price_snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(order_id, *line) for line in priced],
            )
            conn.executemany(
                "INSERT INTO order_notifications (order_id, admin_id, next_attempt_at) VALUES (?, ?, ?)",
                [(order_id, admin_id, now) for admin_id in sorted(admin_ids)],
            )
    except sqlite3.IntegrityError:
        # A concurrent request with the same key won the race.
        existing = find_by_key(conn, user.id, body.idempotency_key)
        if existing is None:
            raise
        return existing, False
    return order_id, True


def _items(conn: sqlite3.Connection, order_ids: list[int]) -> dict[int, list[OrderItemOut]]:
    result: dict[int, list[OrderItemOut]] = {oid: [] for oid in order_ids}
    if order_ids:
        marks = ",".join("?" * len(order_ids))
        for r in conn.execute(
            f"SELECT * FROM order_items WHERE order_id IN ({marks}) ORDER BY order_id, id", order_ids
        ):
            result[r["order_id"]].append(
                OrderItemOut(
                    product_id=r["product_id"], title=r["title_snapshot"], size=r["size"],
                    qty=r["qty"], price=r["price_snapshot"],
                )
            )
    return result


def _public(row: sqlite3.Row, items: list[OrderItemOut]) -> OrderOut:
    return OrderOut(
        id=row["id"], status=row["status"], total=row["total"], created_at=row["created_at"],
        customer_name=row["customer_name"], phone=row["phone"], city=row["city"],
        delivery_point=row["delivery_point"], comment=row["comment"], items=items,
    )


def get_own(conn: sqlite3.Connection, user_id: int, order_id: int) -> OrderOut | None:
    row = conn.execute(
        "SELECT * FROM orders WHERE id = ? AND user_telegram_id = ?", (order_id, user_id)
    ).fetchone()
    return _public(row, _items(conn, [order_id])[order_id]) if row else None


MAX_OWN_ORDERS = 50


def list_own(conn: sqlite3.Connection, user_id: int) -> list[OrderOut]:
    rows = conn.execute(
        "SELECT * FROM orders WHERE user_telegram_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, MAX_OWN_ORDERS),
    ).fetchall()
    items = _items(conn, [r["id"] for r in rows])
    return [_public(r, items[r["id"]]) for r in rows]


# --- admin ----------------------------------------------------------------------


def _admin(row: sqlite3.Row, items: list[OrderItemOut], notes: list[OrderNotificationOut]) -> AdminOrder:
    return AdminOrder(
        **_public(row, items).model_dump(),
        user_telegram_id=row["user_telegram_id"], username=row["username"],
        first_name=row["first_name"], notified=bool(row["notified"]),
        notify_attempts=row["notify_attempts"], updated_at=row["updated_at"], notifications=notes,
    )


def _notifications(conn: sqlite3.Connection, order_ids: list[int]) -> dict[int, list[OrderNotificationOut]]:
    result: dict[int, list[OrderNotificationOut]] = {oid: [] for oid in order_ids}
    if order_ids:
        marks = ",".join("?" * len(order_ids))
        for r in conn.execute(
            f"SELECT * FROM order_notifications WHERE order_id IN ({marks}) ORDER BY order_id, admin_id",
            order_ids,
        ):
            result[r["order_id"]].append(
                OrderNotificationOut(
                    admin_id=r["admin_id"], status=r["status"], attempts=r["attempts"],
                    last_error=r["last_error"], sent_at=r["sent_at"],
                )
            )
    return result


def admin_list(
    conn: sqlite3.Connection, status: str | None, limit: int, offset: int
) -> tuple[list[AdminOrder], int]:
    where, params = ("WHERE status = ?", [status]) if status else ("", [])
    total = conn.execute(f"SELECT COUNT(*) FROM orders {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*params, limit, offset]
    ).fetchall()
    ids = [r["id"] for r in rows]
    items, notes = _items(conn, ids), _notifications(conn, ids)
    return [_admin(r, items[r["id"]], notes[r["id"]]) for r in rows], total


def admin_get(conn: sqlite3.Connection, order_id: int) -> AdminOrder | None:
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        return None
    return _admin(row, _items(conn, [order_id])[order_id], _notifications(conn, [order_id])[order_id])


def set_status(conn: sqlite3.Connection, order_id: int, status: str) -> bool:
    return (
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (status, utc_now(), order_id)
        ).rowcount
        == 1
    )
