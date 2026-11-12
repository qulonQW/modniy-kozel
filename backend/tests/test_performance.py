"""Thumbnails backfill, compact list payloads, gzip and cache headers."""

from __future__ import annotations

import hashlib
import io

import pytest
from conftest import insert_product
from PIL import Image

from app import manage
from app.db import connect, init_db
from app.thumbs import backfill


def jpeg(color, size=(1200, 1600)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", quality=90)
    return out.getvalue()


@pytest.fixture
def with_images(db, settings):
    """Two products with real originals in uploads/products (one shared photo)."""
    folder = settings.uploads_dir / "products"
    folder.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, color in (("a" * 32, (200, 0, 0)), ("b" * 32, (0, 200, 0))):
        (folder / f"{name}.jpg").write_bytes(jpeg(color))
        files[name] = folder / f"{name}.jpg"
    insert_product(db, "img-1", popular=1)
    insert_product(db, "img-2")
    rows = [("img-1", "a", 0), ("img-1", "b", 1), ("img-2", "b", 0)]
    for pid, name, pos in rows:
        db.execute(
            "INSERT INTO product_images (product_id, path, position) VALUES (?, ?, ?)",
            (pid, f"uploads/products/{name * 32}.jpg", pos),
        )
    db.execute(
        "INSERT INTO product_images (product_id, path, position) VALUES ('img-2', 'uploads/products/gone.jpg', 1)"
    )
    db.commit()
    return files


def digests(files) -> dict:
    return {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in files.items()}


def test_backfill_is_idempotent_and_never_touches_originals(with_images, settings, db):
    before = digests(with_images)
    conn = connect(settings.database_path)

    first = backfill(conn, settings.uploads_dir)
    assert first["created"] == 3 and first["missing"] == 1 and first["failed"] == 0
    assert first["backup"].endswith(".sqlite3")
    thumbs = sorted((settings.uploads_dir / "thumbs").iterdir())
    assert len(thumbs) == 2  # the shared photo has one thumbnail
    for t in thumbs:
        assert Image.open(t).format == "WEBP" and Image.open(t).width == 400

    second = backfill(conn, settings.uploads_dir)
    assert second == {"images": 4, "already_done": 3, "created": 0, "missing": 1, "failed": 0}  # no backup
    assert digests(with_images) == before  # originals untouched
    assert sorted((settings.uploads_dir / "thumbs").iterdir()) == thumbs

    # a deleted thumbnail file is recreated
    thumbs[0].unlink()
    assert backfill(conn, settings.uploads_dir)["created"] >= 1
    conn.close()
    backups = list(settings.database_path.parent.glob("backup-*.sqlite3"))
    assert len(backups) == 2


def test_backfill_cli(with_images, settings, monkeypatch, capsys):
    monkeypatch.setattr(manage, "load_settings", lambda: settings)
    assert manage.main(["thumbnails"]) == 0
    assert manage.main(["thumbnails"]) == 0
    out = capsys.readouterr().out
    assert "created=3" in out and "created=0" in out and "database backup" in out


def test_lists_return_cover_thumbnail_only(client, with_images, settings):
    conn = connect(settings.database_path)
    backfill(conn, settings.uploads_dir)
    conn.close()

    item = next(p for p in client.get("/api/products").json() if p["id"] == "img-1")
    assert item["images"] == []
    assert item["imageUrl"] == f"/uploads/products/{'a' * 32}.jpg"
    assert item["thumbUrl"].startswith("/uploads/thumbs/")
    cat = client.get("/api/catalog", params={"limit": 50}).json()["items"]
    assert all(p["images"] == [] for p in cat)

    detail = client.get("/api/products/img-1").json()  # product page keeps full size
    assert detail["images"] == [f"/uploads/products/{'a' * 32}.jpg", f"/uploads/products/{'b' * 32}.jpg"]
    assert detail["thumbUrl"] == item["thumbUrl"]


def test_ids_lookup_for_the_cart(client, db):
    for pid in ("c-1", "c-2", "c-3"):
        insert_product(db, pid)
    insert_product(db, "c-draft", status="draft")
    db.commit()
    res = client.get("/api/products", params={"ids": "c-1,c-3,c-draft,c-1,nope"})
    assert sorted(p["id"] for p in res.json()) == ["c-1", "c-3"]
    assert client.get("/api/products", params={"ids": ""}).json() == []
    assert client.get("/api/products", params={"ids": "BAD ID"}).status_code == 422
    many = ",".join(f"x-{i}" for i in range(51))
    assert client.get("/api/products", params={"ids": many}).status_code == 422


def test_home_popular_filter(client, db):
    insert_product(db, "pop-1", popular=1)
    insert_product(db, "pop-2", popular=0)
    db.commit()
    ids = [p["id"] for p in client.get("/api/catalog", params={"popular": "true", "limit": 12}).json()["items"]]
    assert ids == ["pop-1"]


def test_json_is_gzipped(client, db):
    for i in range(30):
        insert_product(db, f"g-{i}", description="Опис " * 20)
    db.commit()
    res = client.get("/api/products", headers={"Accept-Encoding": "gzip"})
    assert res.headers["content-encoding"] == "gzip"
    assert res.num_bytes_downloaded < len(res.content) / 3  # bytes on the wire vs decoded
    assert res.headers["cache-control"] == "no-cache"


def test_image_cache_headers(client, with_images):
    res = client.get(f"/uploads/products/{'a' * 32}.jpg")
    assert res.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "content-encoding" not in res.headers  # JPEG is not re-compressed
    res = client.get("/public/images/products/sneaker-01.jpg")
    assert res.status_code == 200 and res.headers["cache-control"] == "public, max-age=86400"
    assert "cache-control" not in client.get("/uploads/products/missing.jpg").headers or \
        "immutable" not in client.get("/uploads/products/missing.jpg").headers["cache-control"]


def test_thumb_path_column_is_added_to_old_databases(tmp_path):
    conn = connect(tmp_path / "old.sqlite3")
    conn.executescript(
        "CREATE TABLE products (id TEXT PRIMARY KEY, title TEXT, description TEXT, category TEXT,"
        " price_retail INTEGER, price_supplier INTEGER, sizes TEXT, in_stock INTEGER, popular INTEGER,"
        " status TEXT, edited_by_admin INTEGER, source_channel TEXT, source_message_id INTEGER,"
        " created_at TEXT, updated_at TEXT);"
        "CREATE TABLE product_images (id INTEGER PRIMARY KEY, product_id TEXT, path TEXT, position INTEGER);"
        "INSERT INTO products VALUES ('p-1','T','','Одяг',100,50,'[]',1,0,'published',1,NULL,NULL,'x','x');"
        "INSERT INTO product_images VALUES (1,'p-1','uploads/products/x.jpg',0);"
    )
    init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(product_images)")}
    assert "thumb_path" in cols
    row = conn.execute("SELECT status, edited_by_admin, price_supplier FROM products").fetchone()
    assert tuple(row) == ("published", 1, 50)  # existing data unchanged
    assert list(tmp_path.glob("backup-*.sqlite3"))
    conn.close()
