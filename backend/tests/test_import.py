from __future__ import annotations

import pytest

from app import manage
from app.importer import import_files


def item(pid: str = "tg-shop-101", **overrides):
    data = {
        "id": pid,
        "title": "Кросівки тестові",
        "description": "Опис",
        "category": "Взуття",
        "price_retail": 1999,
        "sizes": [41, 42, "43"],
        "in_stock": True,
        "images": ["images/products/tg-shop-101-1.jpg"],
        "source_channel": "shop",
        "source_message_id": 101,
    }
    data.update(overrides)
    return data


def count(conn, table: str = "products") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_import_twice_is_idempotent(conn, write_json):
    public = write_json("p.json", [item("a-1"), item("a-2"), item("a-3")])
    costs = write_json("c.json", {"a-1": {"price_supplier": 900}})

    first = import_files(conn, public, costs)
    stamps = conn.execute("SELECT id, updated_at FROM products ORDER BY id").fetchall()
    second = import_files(conn, public, costs)

    assert (first.inserted, first.errors) == (3, [])
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 3)
    assert count(conn) == 3
    assert count(conn, "product_images") == 3
    assert conn.execute("SELECT id, updated_at FROM products ORDER BY id").fetchall() == stamps


def test_imported_rows_are_drafts_with_costs(conn, write_json):
    public = write_json("p.json", [item("a-1"), item("a-2")])
    costs = write_json("c.json", {"a-1": {"price_supplier": 900}})
    import_files(conn, public, costs)

    rows = {r["id"]: r for r in conn.execute("SELECT * FROM products")}
    assert {r["status"] for r in rows.values()} == {"draft"}
    assert rows["a-1"]["price_supplier"] == 900
    assert rows["a-2"]["price_supplier"] is None
    assert rows["a-1"]["sizes"] == '["41", "42", "43"]'
    assert rows["a-1"]["edited_by_admin"] == 0


def test_reimport_keeps_admin_edited_rows(conn, write_json):
    import_files(conn, write_json("p.json", [item("a-1")]), None)
    conn.execute(
        "UPDATE products SET title = 'Admin title', price_retail = 5, edited_by_admin = 1 "
        "WHERE id = 'a-1'"
    )
    conn.commit()

    changed = write_json("p2.json", [item("a-1", title="Parsed again", price_retail=7777)])
    report = import_files(conn, changed, None)

    row = conn.execute("SELECT title, price_retail FROM products WHERE id = 'a-1'").fetchone()
    assert report.protected == 1
    assert tuple(row) == ("Admin title", 5)
    assert count(conn) == 1


def test_reimport_never_touches_non_draft_rows(conn, write_json):
    import_files(conn, write_json("p.json", [item("a-1")]), None)
    conn.execute("UPDATE products SET status = 'published' WHERE id = 'a-1'")
    conn.commit()

    report = import_files(conn, write_json("p2.json", [item("a-1", title="New")]), None)

    row = conn.execute("SELECT title, status FROM products WHERE id = 'a-1'").fetchone()
    assert report.protected == 1
    assert tuple(row) == ("Кросівки тестові", "published")


def test_reimport_refreshes_untouched_drafts(conn, write_json):
    import_files(conn, write_json("p.json", [item("a-1")]), None)
    new_images = ["images/products/x-1.webp", "images/products/x-2.png"]
    report = import_files(
        conn, write_json("p2.json", [item("a-1", title="Updated", images=new_images)]), None
    )

    assert report.updated == 1
    assert conn.execute("SELECT title FROM products").fetchone()[0] == "Updated"
    paths = [r[0] for r in conn.execute("SELECT path FROM product_images ORDER BY position")]
    assert paths == new_images


@pytest.mark.parametrize(
    "bad",
    [
        {"category": "Іграшки"},
        {"price_retail": -1},
        {"title": ""},
        {"title": "x" * 121},
        {"id": "Bad ID"},
        {"images": ["../../etc/passwd.jpg"]},
        {"images": ["https://evil.example/a.jpg"]},
        {"images": ["images/products/a.exe"]},
        {"sizes": [str(i) for i in range(21)]},
        {"status": "published"},  # importer must not be able to set status
        {"unknown_field": 1},
    ],
)
def test_invalid_records_are_skipped(conn, write_json, bad):
    public = write_json("p.json", [item("ok-1"), item("bad-1", **bad)])
    report = import_files(conn, public, None)

    assert report.inserted == 1
    assert len(report.errors) == 1
    assert [r[0] for r in conn.execute("SELECT id FROM products")] == ["ok-1"]


def test_duplicate_ids_keep_first(conn, write_json):
    public = write_json("p.json", [item("a-1", title="First"), item("a-1", title="Second")])
    report = import_files(conn, public, None)

    assert report.inserted == 1 and len(report.errors) == 1
    assert conn.execute("SELECT title FROM products").fetchone()[0] == "First"


def test_seed_demo_is_published_and_idempotent(conn):
    first = import_files(conn, manage.DEMO_FILE, None, mode="seed")
    second = import_files(conn, manage.DEMO_FILE, None, mode="seed")

    assert (first.inserted, first.errors) == (7, [])
    assert (second.inserted, second.protected) == (0, 7)
    assert {r[0] for r in conn.execute("SELECT status FROM products")} == {"published"}
    assert count(conn, "product_images") == 7


def test_import_does_not_overwrite_seeded_products(conn, write_json):
    import_files(conn, manage.DEMO_FILE, None, mode="seed")
    report = import_files(conn, write_json("p.json", [item("sneaker-01", title="X")]), None)

    assert report.protected == 1
    row = conn.execute("SELECT title, status FROM products WHERE id = 'sneaker-01'").fetchone()
    assert row["status"] == "published" and row["title"] != "X"


def test_cli_import_json(tmp_path, monkeypatch, write_json, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.sqlite3"))
    public = write_json("p.json", [item("a-1"), item("a-2")])
    costs = write_json("c.json", {"a-2": {"price_supplier": 10}})
    args = ["import-json", "--public", str(public), "--costs", str(costs)]

    assert manage.main(args) == 0
    assert manage.main(args) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "inserted=2 updated=0 unchanged=0 protected=0 errors=0",
        "inserted=0 updated=0 unchanged=2 protected=0 errors=0",
    ]
