"""GET /api/catalog: search by name, category, sort, pagination."""

from __future__ import annotations

import pytest
from conftest import insert_product

from app.db import backfill_search_text, connect, init_db, search_key
from app.importer import import_files


def add(db, pid, title, price=1000, status="published", **fields):
    insert_product(db, pid, status, title=title, price_retail=price, **fields)


def ids(res) -> list[str]:
    assert res.status_code == 200, res.text
    return [p["id"] for p in res.json()["items"]]


@pytest.fixture
def shop(db):
    add(db, "puh-1", "Пуховик Moncler", 2900, created_at="2026-01-03T00:00:00+00:00")
    add(db, "puh-2", "ПУХОВИК The North Face", 2500, created_at="2026-01-02T00:00:00+00:00")
    add(db, "puh-3", "пуховик Nike", 2700, in_stock=0, created_at="2026-01-05T00:00:00+00:00")
    add(db, "hood-1", "Худі Stüssy 100% cotton", 1300, popular=1, created_at="2026-01-01T00:00:00+00:00")
    add(db, "snk-1", "Кросівки New_Balance 9060", 2500, category="Взуття", created_at="2026-01-04T00:00:00+00:00")
    add(db, "draft-1", "Пуховик чернетка", 100, status="draft")
    add(db, "hidden-1", "Пуховик прихований", 100, status="hidden")
    return db


@pytest.mark.parametrize("q", ["пуховик", "ПУХОВИК", "Пуховик", "  пУхОвИк  "])
def test_search_is_case_insensitive_for_cyrillic(client, shop, q):
    assert sorted(ids(client.get("/api/catalog", params={"q": q}))) == ["puh-1", "puh-2", "puh-3"]


def test_search_latin_and_partial_words(client, shop):
    assert ids(client.get("/api/catalog", params={"q": "north FACE"})) == ["puh-2"]
    assert ids(client.get("/api/catalog", params={"q": "STÜSSY"})) == ["hood-1"]
    assert ids(client.get("/api/catalog", params={"q": "кросів"})) == ["snk-1"]


@pytest.mark.parametrize("q,expected", [("%", ["hood-1"]), ("_", ["snk-1"]), ("\\", []), ("new_b", ["snk-1"]), ("new%b", [])])
def test_wildcards_match_literally(client, shop, q, expected):
    assert ids(client.get("/api/catalog", params={"q": q})) == expected


def test_nonsense_query_is_empty(client, shop):
    res = client.get("/api/catalog", params={"q": "zzzqqq"})
    assert res.json() == {"items": [], "total": 0}


def test_price_sorts_with_out_of_stock_last(client, shop):
    asc = ids(client.get("/api/catalog", params={"sort": "price_asc"}))
    desc = ids(client.get("/api/catalog", params={"sort": "price_desc"}))
    # puh-3 (2700) is out of stock: last in both directions
    assert asc == ["hood-1", "puh-2", "snk-1", "puh-1", "puh-3"]  # 2500 tie broken by id
    assert desc == ["puh-1", "puh-2", "snk-1", "hood-1", "puh-3"]


def test_popular_and_newest_sorts(client, shop):
    # popular first, then newest; out-of-stock puh-3 (the newest of all) still last
    assert ids(client.get("/api/catalog")) == ["hood-1", "snk-1", "puh-1", "puh-2", "puh-3"]
    assert ids(client.get("/api/catalog", params={"sort": "newest"})) == ["snk-1", "puh-1", "puh-2", "hood-1", "puh-3"]


def test_category_filter_works_with_search_and_sort(client, shop):
    assert ids(client.get("/api/catalog", params={"category": "Взуття"})) == ["snk-1"]
    assert ids(client.get("/api/catalog", params={"category": "Одяг", "q": "пуховик", "sort": "price_asc"})) == [
        "puh-2", "puh-1", "puh-3",
    ]
    assert client.get("/api/catalog", params={"category": "Аксесуари"}).status_code == 422


@pytest.mark.parametrize("sort", ["popular", "price_asc", "price_desc", "newest"])
def test_paging_yields_every_item_exactly_once(client, db, sort):
    for n in range(23):  # many equal prices / dates / popularity -> ties everywhere
        add(db, f"p-{n:02d}", f"Товар {n}", price=1000 + (n % 3) * 100, popular=n % 2,
            in_stock=int(n % 5 != 0), created_at=f"2026-01-0{1 + n % 4}T00:00:00+00:00")
    seen, offset, total = [], 0, None
    while True:
        res = client.get("/api/catalog", params={"sort": sort, "limit": 5, "offset": offset}).json()
        total = res["total"]
        if not res["items"]:
            break
        seen += [p["id"] for p in res["items"]]
        offset += 5
    assert total == 23 and len(seen) == 23 and len(set(seen)) == 23
    stock = [p["inStock"] for p in client.get("/api/catalog", params={"sort": sort, "limit": 50}).json()["items"]]
    assert stock == sorted(stock, reverse=True)  # all in-stock first


def test_total_counts_all_matches_not_the_page(client, shop):
    res = client.get("/api/catalog", params={"q": "пуховик", "limit": 1}).json()
    assert res["total"] == 3 and len(res["items"]) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "price"}, {"sort": "id; DROP TABLE products"}, {"limit": 0}, {"limit": 51},
        {"offset": -1}, {"q": "я" * 51}, {"q": "x" * 201},
    ],
)
def test_invalid_params_are_rejected(client, shop, params):
    assert client.get("/api/catalog", params=params).status_code == 422


def test_q_spaces_are_collapsed_before_the_length_check(client, shop):
    assert client.get("/api/catalog", params={"q": "пуховик" + " " * 100}).status_code == 200


def test_drafts_hidden_and_private_fields_never_appear(client, shop):
    res = client.get("/api/catalog", params={"q": "пуховик", "limit": 50})
    body = res.text
    assert "draft-1" not in body and "hidden-1" not in body
    for field in ("price_supplier", "271828", "needs_review", "review_reasons", "search_text",
                  "duplicate_group", "status", "secret-channel"):
        assert field not in body
    assert res.headers["cache-control"] == "no-cache"


def test_catalog_is_rate_limited(client, shop):
    limiter = client.app.state.limiters["catalog"]
    limiter.limit = 3
    codes = [client.get("/api/catalog").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_products_endpoint_is_unchanged(client, shop):
    data = client.get("/api/products").json()
    assert isinstance(data, list) and len(data) == 5


# --- search_text maintenance ---------------------------------------------------


def test_search_key_normalization():
    assert search_key("  ЧЁРНЫЙ   Пуховик ") == "черный пуховик"
    assert search_key("Зʼїзд’s") == "з'їзд's"


def test_admin_create_and_rename_update_search_text(admin, client, db):
    res = admin.post("/api/admin/products", json={
        "title": "Світшот Carhartt", "category": "Одяг", "price_retail": 1500, "status": "published",
    })
    pid = res.json()["id"]
    assert ids(client.get("/api/catalog", params={"q": "СВІТШОТ"})) == [pid]
    admin.patch(f"/api/admin/products/{pid}", json={"title": "Лонгслів Carhartt"})
    assert ids(client.get("/api/catalog", params={"q": "світшот"})) == []
    assert ids(client.get("/api/catalog", params={"q": "лонгслів"})) == [pid]


def test_import_json_fills_search_text(conn, write_json):
    public = write_json("p.json", [{
        "id": "imp-1", "title": "ПУХОВИК Moncler", "category": "Одяг", "price_retail": 100,
    }])
    import_files(conn, public, None)
    assert conn.execute("SELECT search_text FROM products WHERE id = 'imp-1'").fetchone()[0] == "пуховик moncler"


def test_backfill_on_startup_for_existing_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    c = connect(path)
    init_db(c)
    insert_product(c, "old-1", title="Старий ПУХОВИК", search_text="")
    c.close()
    c = connect(path)
    init_db(c)  # what the app does on startup
    assert c.execute("SELECT search_text FROM products").fetchone()[0] == "старий пуховик"
    assert backfill_search_text(c) == 0  # idempotent
    c.close()
