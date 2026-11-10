from __future__ import annotations

import sqlite3

from conftest import insert_product

from app.db import connect, init_db


def flagged(db, pid: str, price: int = 1000, status: str = "draft") -> None:
    insert_product(
        db, pid, status=status, price_retail=price, needs_review=1,
        review_reasons="no_drop_price,category_unknown", price_reference=1300,
    )


def row(db, pid):
    return db.execute(
        "SELECT status, needs_review, review_reasons FROM products WHERE id = ?", (pid,)
    ).fetchone()


def test_status_only_publish_is_blocked_while_needs_review(admin, db):
    flagged(db, "a-1")
    res = admin.patch("/api/admin/products/a-1", json={"status": "published"})

    assert res.status_code == 409
    assert tuple(row(db, "a-1")) == ("draft", 1, "no_drop_price,category_unknown")


def test_publish_blocked_when_price_is_zero(admin, db):
    insert_product(db, "a-1", status="draft", price_retail=0)

    assert admin.patch("/api/admin/products/a-1", json={"status": "published"}).status_code == 409
    assert admin.patch(
        "/api/admin/products/a-1", json={"title": "New", "status": "published"}
    ).status_code == 409
    assert row(db, "a-1")["status"] == "draft"
    assert admin.post(
        "/api/admin/products",
        json={"title": "x", "category": "Одяг", "price_retail": 0, "status": "published"},
    ).status_code == 409


def test_published_product_cannot_drop_to_zero_price(admin, db):
    insert_product(db, "a-1")
    assert admin.patch("/api/admin/products/a-1", json={"price_retail": 0}).status_code == 409


def test_content_edit_with_valid_values_clears_review(admin, db):
    flagged(db, "a-1")
    res = admin.patch("/api/admin/products/a-1", json={"price_retail": 1500, "category": "Взуття"})

    assert res.status_code == 200
    assert res.json()["needs_review"] is False and res.json()["review_reasons"] == []
    assert admin.patch("/api/admin/products/a-1", json={"status": "published"}).status_code == 200


def test_edit_and_publish_in_one_save(admin, db):
    flagged(db, "a-1")
    res = admin.patch("/api/admin/products/a-1", json={"title": "Checked", "status": "published"})

    assert res.status_code == 200 and res.json()["status"] == "published"


def test_edit_keeping_zero_price_does_not_clear_review(admin, db):
    flagged(db, "a-1", price=0)
    res = admin.patch("/api/admin/products/a-1", json={"title": "Still no price"})

    assert res.json()["needs_review"] is True


def test_bulk_publish_skips_blocked(admin, db):
    insert_product(db, "ok-1", status="draft")
    flagged(db, "rev-1")
    insert_product(db, "zero-1", status="draft", price_retail=0)

    res = admin.post(
        "/api/admin/products/bulk-status",
        json={"ids": ["ok-1", "rev-1", "zero-1", "missing"], "status": "published"},
    )

    assert res.json() == {"updated": 1, "blocked": ["rev-1", "zero-1"]}
    assert [p["id"] for p in admin.get("/api/products").json()] == ["ok-1"]
    hide = admin.post("/api/admin/products/bulk-status", json={"ids": ["rev-1"], "status": "hidden"})
    assert hide.json() == {"updated": 1, "blocked": []}


def test_needs_review_filter(admin, db):
    insert_product(db, "ok-1", status="draft")
    flagged(db, "rev-1")

    def ids(**params):
        return [p["id"] for p in admin.get("/api/admin/products", params=params).json()["items"]]

    assert ids(needs_review="true") == ["rev-1"]
    assert ids(needs_review="false") == ["ok-1"]
    item = admin.get("/api/admin/products/rev-1").json()
    assert item["review_reasons"] == ["no_drop_price", "category_unknown"]
    assert item["price_reference"] == 1300


def test_review_fields_never_public(admin, db):
    insert_product(db, "a-1", price_reference=4242, review_reasons="x_reason")
    body = admin.get("/api/products").text

    for marker in ("needs_review", "review_reasons", "price_reference", "4242", "x_reason"):
        assert marker not in body


def test_init_db_adds_missing_columns_to_old_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE products (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL "
        "DEFAULT '', category TEXT NOT NULL, price_retail INTEGER NOT NULL, price_supplier INTEGER, "
        "sizes TEXT NOT NULL DEFAULT '[]', in_stock INTEGER NOT NULL DEFAULT 1, popular INTEGER NOT "
        "NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'draft', edited_by_admin INTEGER NOT NULL "
        "DEFAULT 0, source_channel TEXT, source_message_id INTEGER, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    old.execute(
        "INSERT INTO products (id, title, category, price_retail, created_at, updated_at) "
        "VALUES ('a', 't', 'Одяг', 1, 'x', 'x')"
    )
    old.commit()
    old.close()

    conn = connect(path)
    init_db(conn)
    init_db(conn)  # second run is a no-op
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
    r = conn.execute("SELECT needs_review, review_reasons, price_reference FROM products").fetchone()
    conn.close()

    assert {"needs_review", "review_reasons", "price_reference"} <= cols
    assert tuple(r) == (0, "", None)


def test_explicit_review_confirmation(admin, db):
    flagged(db, "a-1")
    flagged(db, "zero-1", price=0)

    res = admin.patch("/api/admin/products/a-1", json={"needs_review": False})
    assert res.status_code == 200 and res.json()["needs_review"] is False
    assert admin.patch("/api/admin/products/zero-1", json={"needs_review": False}).status_code == 409
    assert admin.patch("/api/admin/products/a-1", json={"needs_review": True}).status_code == 422
