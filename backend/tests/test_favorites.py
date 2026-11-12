"""Favorites: per-user, published products only, limit, auth."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import insert_product
from test_auth import init_data

from app import repo
from app.auth import HEADER, USER_MAX_AGE_SECONDS
from app.db import backup_database, connect, init_db

ALICE, BOB = 5001, 5002  # ordinary shoppers, not admins


def as_user(user_id: int, age: int = 0) -> dict[str, str]:
    return {HEADER: init_data(user_id, age=age)}


@pytest.fixture
def shop(db):
    insert_product(db, "fav-1", title="Кросівки")
    insert_product(db, "fav-2", title="Худі")
    insert_product(db, "fav-draft", status="draft")
    insert_product(db, "fav-hidden", status="hidden")
    db.commit()
    return db


def fav_ids(client, user_id: int) -> list[str]:
    res = client.get("/api/favorites", headers=as_user(user_id))
    assert res.status_code == 200, res.text
    assert res.headers["cache-control"] == "no-store"
    return [p["id"] for p in res.json()]


def test_add_list_remove(client, shop):
    assert client.put("/api/favorites/fav-1", headers=as_user(ALICE)).status_code == 204
    assert client.put("/api/favorites/fav-2", headers=as_user(ALICE)).status_code == 204
    assert client.put("/api/favorites/fav-2", headers=as_user(ALICE)).status_code == 204  # idempotent
    assert sorted(fav_ids(client, ALICE)) == ["fav-1", "fav-2"]
    assert client.delete("/api/favorites/fav-1", headers=as_user(ALICE)).status_code == 204
    assert client.delete("/api/favorites/fav-1", headers=as_user(ALICE)).status_code == 204
    assert fav_ids(client, ALICE) == ["fav-2"]


def test_users_never_see_each_others_favorites(client, shop):
    client.put("/api/favorites/fav-1", headers=as_user(ALICE))
    assert fav_ids(client, BOB) == []
    # Bob deleting "his" fav-1 does not touch Alice's.
    client.delete("/api/favorites/fav-1", headers=as_user(BOB))
    assert fav_ids(client, ALICE) == ["fav-1"]


@pytest.mark.parametrize("pid", ["fav-draft", "fav-hidden", "missing"])
def test_only_published_products_can_be_added(client, shop, pid):
    assert client.put(f"/api/favorites/{pid}", headers=as_user(ALICE)).status_code == 404


def test_unpublished_favorite_disappears_from_list(client, shop):
    client.put("/api/favorites/fav-1", headers=as_user(ALICE))
    shop.execute("UPDATE products SET status = 'hidden' WHERE id = 'fav-1'")
    shop.commit()
    assert fav_ids(client, ALICE) == []


def test_public_fields_only(client, shop):
    client.put("/api/favorites/fav-1", headers=as_user(ALICE))
    item = client.get("/api/favorites", headers=as_user(ALICE)).json()[0]
    for field in ("price_supplier", "price_reference", "status", "needs_review", "review_reasons"):
        assert field not in item


def test_limit(client, db):
    for i in range(repo.MAX_FAVORITES + 1):
        insert_product(db, f"many-{i}")
        if i < repo.MAX_FAVORITES:
            db.execute(
                "INSERT INTO favorites (user_telegram_id, product_id, created_at) VALUES (?, ?, '2026-01-01')",
                (ALICE, f"many-{i}"),
            )
    db.commit()
    res = client.put(f"/api/favorites/many-{repo.MAX_FAVORITES}", headers=as_user(ALICE))
    assert res.status_code == 409 and "favorites_full" in res.json()["detail"]
    # re-adding an existing one is still fine
    assert client.put("/api/favorites/many-0", headers=as_user(ALICE)).status_code == 204


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_requires_valid_init_data(client, shop, method):
    url = "/api/favorites" if method == "get" else "/api/favorites/fav-1"
    assert getattr(client, method)(url).status_code == 401
    tampered = init_data(ALICE).replace("hash=", "hash=0")[:-1]
    assert getattr(client, method)(url, headers={HEADER: tampered}).status_code == 401
    expired = as_user(ALICE, age=USER_MAX_AGE_SECONDS + 10)
    assert getattr(client, method)(url, headers=expired).status_code == 401


def test_user_init_data_lives_a_day_admin_an_hour(client, shop):
    two_hours = 2 * 3600
    assert client.get("/api/favorites", headers=as_user(ALICE, age=two_hours)).status_code == 200
    assert client.get("/api/admin/products", headers=as_user(111, age=two_hours)).status_code == 401
    assert client.get("/api/admin/products", headers=as_user(111)).status_code == 200


def test_user_id_from_body_or_query_is_ignored(client, shop):
    client.put("/api/favorites/fav-1?user_telegram_id=5002", headers=as_user(ALICE))
    assert fav_ids(client, BOB) == [] and fav_ids(client, ALICE) == ["fav-1"]


# --- migration safety -----------------------------------------------------------


def test_existing_database_is_backed_up_before_new_tables(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = connect(path)
    init_db(conn)
    conn.execute(
        "INSERT INTO products (id, title, category, price_retail, status, edited_by_admin, "
        "created_at, updated_at) VALUES ('old-1', 'Old', 'Одяг', 500, 'published', 1, 'x', 'x')"
    )
    conn.execute("DROP TABLE favorites")  # simulate a database from before this release
    conn.commit()

    init_db(conn)
    backups = list(tmp_path.glob("backup-*.sqlite3"))
    assert len(backups) == 1
    copy = sqlite3.connect(backups[0])
    assert copy.execute("SELECT status, edited_by_admin FROM products").fetchall() == [("published", 1)]
    copy.close()
    # rows untouched, new table present
    assert [tuple(r) for r in conn.execute("SELECT status, edited_by_admin FROM products")] == [("published", 1)]
    assert conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 0

    init_db(conn)  # nothing to migrate: no second backup
    assert len(list(tmp_path.glob("backup-*.sqlite3"))) == 1
    conn.close()


def test_new_database_is_not_backed_up(tmp_path):
    conn = connect(tmp_path / "new.sqlite3")
    init_db(conn)
    conn.close()
    assert not list(tmp_path.glob("backup-*"))
    assert backup_database(sqlite3.connect(":memory:")) is None
