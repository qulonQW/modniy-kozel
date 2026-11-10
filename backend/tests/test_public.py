from __future__ import annotations

import pytest

from conftest import ORIGIN, insert_product

ADMIN_ONLY = {
    "price_supplier",
    "status",
    "edited_by_admin",
    "source_channel",
    "source_message_id",
    "created_at",
    "updated_at",
}
PUBLIC_FIELDS = {
    "id", "name", "description", "category", "price", "sizes",
    "inStock", "popular", "imageUrl", "images",
}


@pytest.fixture
def catalog(db):
    insert_product(db, "pub-1", popular=1)
    insert_product(db, "pub-2", in_stock=0, created_at="2026-02-01T00:00:00+00:00")
    insert_product(db, "draft-1", status="draft")
    insert_product(db, "hidden-1", status="hidden")
    db.executemany(
        "INSERT INTO product_images (product_id, path, position) VALUES (?, ?, ?)",
        [
            ("pub-1", "uploads/products/b.webp", 1),
            ("pub-1", "uploads/products/a.jpg", 0),
        ],
    )
    db.commit()


def test_list_returns_only_published(client, catalog):
    res = client.get("/api/products")

    assert res.status_code == 200
    assert [p["id"] for p in res.json()] == ["pub-2", "pub-1"]  # newest first


def test_list_leaks_no_admin_fields(client, catalog):
    res = client.get("/api/products")

    for product in res.json():
        assert set(product) == PUBLIC_FIELDS
        assert not ADMIN_ONLY & set(product)
    body = res.text
    for marker in ("price_supplier", "271828", "secret-channel", "31337", "edited_by_admin", "draft"):
        assert marker not in body


def test_product_shape_matches_storefront(client, catalog):
    p = client.get("/api/products/pub-1").json()

    assert p == {
        "id": "pub-1",
        "name": "Товар pub-1",
        "description": "Опис",
        "category": "Одяг",
        "price": 600,
        "sizes": ["S", "M"],
        "inStock": True,
        "popular": True,
        "imageUrl": "/uploads/products/a.jpg",
        "images": ["/uploads/products/a.jpg", "/uploads/products/b.webp"],
    }
    assert client.get("/api/products/pub-2").json()["imageUrl"] is None


@pytest.mark.parametrize("pid", ["draft-1", "hidden-1", "missing", "../etc", "BAD ID"])
def test_get_non_published_is_404(client, catalog, pid):
    res = client.get(f"/api/products/{pid}")

    assert res.status_code == 404
    assert "271828" not in res.text


def test_public_responses_are_not_cached(client, catalog):
    assert client.get("/api/products").headers["cache-control"] == "no-cache"
    assert client.get("/api/products").headers["x-content-type-options"] == "nosniff"


def test_cors_allows_only_configured_origin(client, catalog):
    ok = client.get("/api/products", headers={"Origin": ORIGIN})
    other = client.get("/api/products", headers={"Origin": "https://evil.example"})

    assert ok.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-origin" not in other.headers


def test_seeded_demo_images_are_served(client, db):
    from app import manage
    from app.importer import import_files

    import_files(db, manage.DEMO_FILE, None, mode="seed")
    products = client.get("/api/products").json()

    assert len(products) == 7
    image = client.get(products[0]["imageUrl"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"


def test_env_file_is_not_served(client):
    for path in ("/.env", "/backend/.env", "/public/images/products/../../../backend/.env"):
        assert client.get(path).status_code == 404
