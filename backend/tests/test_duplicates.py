"""Admin view of possible duplicates (flagged by tools/import_products.py)."""

from __future__ import annotations

from conftest import insert_product


def group(db) -> None:
    """main-1 (published) <- dup-1, dup-2 (drafts); solo has no group."""
    insert_product(db, "main-1", "published", created_at="2026-01-01T00:00:00+00:00")
    for pid in ("dup-1", "dup-2"):
        insert_product(
            db, pid, "draft", needs_review=1, review_reasons="possible_duplicate",
            duplicate_group="main-1", created_at="2026-02-01T00:00:00+00:00",
        )
    insert_product(db, "solo", "draft")
    db.executemany(
        "INSERT INTO product_images (product_id, path, position) VALUES (?, ?, ?)",
        [("main-1", "uploads/products/a.jpg", 0), ("main-1", "uploads/products/b.jpg", 1),
         ("dup-1", "uploads/products/c.jpg", 0)],
    )
    db.commit()


def test_duplicates_filter_lists_whole_groups_main_first(admin, db):
    group(db)
    data = admin.get("/api/admin/products", params={"duplicates": "true"}).json()

    assert data["total"] == 3
    assert [p["id"] for p in data["items"]] == ["main-1", "dup-1", "dup-2"]
    assert {p["duplicate_group"] for p in data["items"]} == {"main-1"}
    assert all(p["duplicates"] == [] for p in data["items"])  # details only on a single product

    everything = admin.get("/api/admin/products").json()["items"]
    assert next(p for p in everything if p["id"] == "solo")["duplicate_group"] is None


def test_product_detail_shows_the_other_group_members_with_thumbnails(admin, db):
    group(db)
    dup = admin.get("/api/admin/products/dup-1").json()
    assert dup["duplicate_group"] == "main-1"
    assert [(m["id"], m["is_main"], m["status"], m["image_count"]) for m in dup["duplicates"]] == [
        ("main-1", True, "published", 2), ("dup-2", False, "draft", 0),
    ]
    assert dup["duplicates"][0]["image_url"] == "/uploads/products/a.jpg"
    assert dup["duplicates"][1]["image_url"] is None

    main = admin.get("/api/admin/products/main-1").json()
    assert main["duplicate_group"] == "main-1"
    assert [m["id"] for m in main["duplicates"]] == ["dup-1", "dup-2"]
    assert admin.get("/api/admin/products/solo").json()["duplicates"] == []


def test_confirming_review_says_not_a_duplicate(admin, db):
    group(db)
    res = admin.patch("/api/admin/products/dup-1", json={"needs_review": False})
    assert res.status_code == 200
    assert res.json()["duplicate_group"] is None and res.json()["review_reasons"] == []
    assert [p["id"] for p in admin.get("/api/admin/products", params={"duplicates": "true"}).json()["items"]] == [
        "main-1", "dup-2",
    ]


def test_content_edit_keeps_the_duplicate_link(admin, db):
    group(db)
    res = admin.patch("/api/admin/products/dup-2", json={"title": "Нова назва"})
    assert res.json()["duplicate_group"] == "main-1"


def test_deleting_the_main_product_keeps_the_rest_of_the_group(admin, db):
    group(db)
    assert admin.delete("/api/admin/products/main-1").status_code == 204
    items = admin.get("/api/admin/products", params={"duplicates": "true"}).json()["items"]
    assert [p["id"] for p in items] == ["dup-1", "dup-2"]
    detail = admin.get("/api/admin/products/dup-1").json()
    assert [m["id"] for m in detail["duplicates"]] == ["dup-2"]


def test_duplicate_fields_are_never_public(admin, client, db):
    group(db)
    db.execute("UPDATE products SET status = 'published', needs_review = 0 WHERE id = 'dup-1'")
    db.commit()
    for product in client.get("/api/products").json():
        assert "duplicate_group" not in product and "duplicates" not in product
    assert "duplicate" not in client.get("/api/products/dup-1").text


def test_duplicates_filter_requires_admin(client):
    assert client.get("/api/admin/products", params={"duplicates": "true"}).status_code == 401
