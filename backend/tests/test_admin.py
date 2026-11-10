from __future__ import annotations

import pytest

from conftest import insert_product

NEW = {
    "title": "Худі чорне",
    "description": "Тепле",
    "category": "Одяг",
    "price_retail": 1500,
    "price_supplier": 800,
    "sizes": ["M", "L", "L"],
}


def create(admin, **overrides):
    res = admin.post("/api/admin/products", json={**NEW, **overrides})
    assert res.status_code == 201, res.text
    return res.json()


def test_admin_routes_fail_closed_without_auth(client, db):
    insert_product(db, "a-1")
    calls = [
        ("GET", "/api/admin/products", None),
        ("GET", "/api/admin/products/a-1", None),
        ("POST", "/api/admin/products", NEW),
        ("PATCH", "/api/admin/products/a-1", {"title": "x"}),
        ("DELETE", "/api/admin/products/a-1", None),
        ("POST", "/api/admin/products/bulk-status", {"ids": ["a-1"], "status": "hidden"}),
        ("PUT", "/api/admin/products/a-1/images/order", {"image_ids": []}),
        ("DELETE", "/api/admin/products/a-1/images/1", None),
        ("POST", "/api/admin/products/a-1/images", None),
    ]
    for method, url, body in calls:
        assert client.request(method, url, json=body).status_code == 401, (method, url)
    assert db.execute("SELECT title FROM products").fetchone()[0] == "Товар a-1"


def test_create_returns_admin_fields(admin):
    p = create(admin)

    assert p["id"].startswith("p-")
    assert p["status"] == "draft"
    assert p["price_supplier"] == 800
    assert p["sizes"] == ["M", "L"]
    assert p["edited_by_admin"] is True
    assert admin.get(f"/api/products/{p['id']}").status_code == 404  # draft is not public


def test_full_crud_cycle(admin):
    p = create(admin)
    pid = p["id"]

    res = admin.patch(f"/api/admin/products/{pid}", json={"price_retail": 1700, "status": "published"})
    assert res.status_code == 200
    assert (res.json()["price_retail"], res.json()["status"]) == (1700, "published")
    assert res.json()["title"] == NEW["title"]  # untouched fields stay

    public = admin.get(f"/api/products/{pid}").json()
    assert public["price"] == 1700 and "price_supplier" not in public

    assert admin.patch(f"/api/admin/products/{pid}", json={"price_supplier": None}).json()[
        "price_supplier"
    ] is None

    assert admin.delete(f"/api/admin/products/{pid}").status_code == 204
    assert admin.get(f"/api/admin/products/{pid}").status_code == 404
    assert admin.delete(f"/api/admin/products/{pid}").status_code == 404


def test_update_marks_imported_row_as_admin_edited(admin, db):
    insert_product(db, "tg-1", status="draft")
    admin.patch("/api/admin/products/tg-1", json={"title": "Fixed"})

    assert db.execute("SELECT edited_by_admin FROM products").fetchone()[0] == 1


@pytest.mark.parametrize(
    "body",
    [
        {"price_retail": -1},
        {"price_supplier": -5},
        {"category": "Іграшки"},
        {"status": "deleted"},
        {"title": ""},
        {"title": "x" * 121},
        {"description": "x" * 2001},
        {"sizes": [str(i) for i in range(21)]},
        {"sizes": ["x" * 11]},
        {"title": None},
        {"edited_by_admin": False},
        {"id": "hijack"},
    ],
)
def test_update_validation(admin, db, body):
    insert_product(db, "a-1")
    assert admin.patch("/api/admin/products/a-1", json=body).status_code == 422


def test_create_validation(admin):
    assert admin.post("/api/admin/products", json={**NEW, "price_retail": -1}).status_code == 422
    assert admin.post("/api/admin/products", json={"title": "x"}).status_code == 422


def test_list_filters_and_search(admin, db):
    insert_product(db, "a-1", title="Кросівки Nike", category="Взуття")
    insert_product(db, "a-2", title="КРОСІВКИ adidas", status="draft", category="Взуття")
    insert_product(db, "a-3", title="Футболка 100% бавовна", status="hidden")

    def ids(**params):
        res = admin.get("/api/admin/products", params=params)
        assert res.status_code == 200
        return sorted(p["id"] for p in res.json()["items"])

    assert ids() == ["a-1", "a-2", "a-3"]
    assert ids(status="draft") == ["a-2"]
    assert ids(category="Взуття") == ["a-1", "a-2"]
    assert ids(search="кросівки") == ["a-1", "a-2"]  # Cyrillic, case-insensitive
    assert ids(search="кросівки", status="published") == ["a-1"]
    assert ids(search="100%") == ["a-3"]
    assert ids(search="%") == ["a-3"]  # wildcard is literal
    assert ids(search="' OR 1=1 --") == []
    assert admin.get("/api/admin/products", params={"status": "bogus"}).status_code == 422
    assert admin.get("/api/admin/products").json()["items"][0]["price_supplier"] == 271828


def test_bulk_status(admin, db):
    for pid in ("a-1", "a-2", "a-3"):
        insert_product(db, pid, status="draft")

    res = admin.post(
        "/api/admin/products/bulk-status", json={"ids": ["a-1", "a-2", "nope"], "status": "published"}
    )

    assert res.json() == {"updated": 2, "blocked": []}
    public = sorted(p["id"] for p in admin.get("/api/products").json())
    assert public == ["a-1", "a-2"]
    assert admin.post(
        "/api/admin/products/bulk-status", json={"ids": [], "status": "published"}
    ).status_code == 422


def test_admin_rate_limit(admin):
    admin.app.state.limiters["admin"].limit = 3
    codes = [admin.get("/api/admin/products").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
