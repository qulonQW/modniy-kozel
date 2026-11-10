from __future__ import annotations

import pytest

from conftest import insert_product

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 100
EXE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 100
URL = "/api/admin/products/a-1/images"


@pytest.fixture
def product(db):
    insert_product(db, "a-1", status="draft")


def upload(client, data: bytes, filename: str = "photo.jpg", content_type: str = "image/jpeg"):
    return client.post(URL, files={"file": (filename, data, content_type)})


def stored_files(settings):
    folder = settings.uploads_dir / "products"
    return sorted(folder.iterdir()) if folder.exists() else []


@pytest.mark.parametrize("data,ext", [(JPEG, "jpg"), (PNG, "png"), (WEBP, "webp")])
def test_upload_accepts_images(admin, product, settings, data, ext):
    res = upload(admin, data, filename="../../evil name.php")

    assert res.status_code == 201, res.text
    url = res.json()["images"][0]["url"]
    files = stored_files(settings)
    assert len(files) == 1
    assert files[0].suffix == f".{ext}"
    assert "evil" not in files[0].name and len(files[0].stem) == 32
    assert url == f"/uploads/products/{files[0].name}"
    assert admin.get(url).content == data
    assert admin.get(url).headers["x-content-type-options"] == "nosniff"


def test_upload_rejects_exe_renamed_to_jpg(admin, product, settings):
    res = upload(admin, EXE, filename="photo.jpg", content_type="image/jpeg")

    assert res.status_code == 415
    assert stored_files(settings) == []


def test_upload_rejects_svg_and_html(admin, product, settings):
    assert upload(admin, b"<svg onload=alert(1)>", "a.svg", "image/svg+xml").status_code == 415
    assert upload(admin, b"<html><script>", "a.jpg").status_code == 415
    assert stored_files(settings) == []


def test_upload_rejects_over_5mb(admin, product, settings):
    big = JPEG + b"\x00" * (5 * 1024 * 1024)
    res = upload(admin, big)

    assert res.status_code == 413
    assert stored_files(settings) == []


def test_upload_exactly_5mb_is_allowed(admin, product):
    data = JPEG + b"\x00" * (5 * 1024 * 1024 - len(JPEG))
    assert upload(admin, data).status_code == 201


def test_upload_requires_file_field(admin, product):
    res = admin.post(URL, files={"other": ("a.jpg", JPEG, "image/jpeg")})
    assert res.status_code == 422


def test_upload_to_missing_product(admin):
    assert admin.post("/api/admin/products/nope/images", files={"file": ("a.jpg", JPEG)}).status_code == 404


def test_upload_limit_per_product(admin, product):
    for _ in range(10):
        assert upload(admin, JPEG).status_code == 201
    assert upload(admin, JPEG).status_code == 409


def test_upload_rate_limit(admin, product):
    admin.app.state.limiters["upload"].limit = 2
    codes = [upload(admin, JPEG).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


def test_reorder_and_delete_images(admin, product, settings, db):
    ids = [upload(admin, data).json()["images"][-1]["id"] for data in (JPEG, PNG, WEBP)]

    res = admin.put(f"{URL}/order", json={"image_ids": [ids[2], ids[0], ids[1]]})
    assert [i["id"] for i in res.json()["images"]] == [ids[2], ids[0], ids[1]]
    assert [i["position"] for i in res.json()["images"]] == [0, 1, 2]

    for bad in ([ids[0], ids[1]], [ids[0], ids[0], ids[1]], [ids[0], ids[1], 999]):
        assert admin.put(f"{URL}/order", json={"image_ids": bad}).status_code == 422

    res = admin.delete(f"{URL}/{ids[0]}")
    assert [i["id"] for i in res.json()["images"]] == [ids[2], ids[1]]
    assert [i["position"] for i in res.json()["images"]] == [0, 1]
    assert len(stored_files(settings)) == 2
    assert admin.delete(f"{URL}/{ids[0]}").status_code == 404

    insert_product(db, "b-1")
    assert admin.delete(f"/api/admin/products/b-1/images/{ids[1]}").status_code == 404

    assert admin.delete("/api/admin/products/a-1").status_code == 204
    assert stored_files(settings) == []


def test_deleting_product_keeps_non_upload_files(admin, db, tmp_path):
    insert_product(db, "a-1")
    db.execute(
        "INSERT INTO product_images (product_id, path, position) "
        "VALUES ('a-1', 'public/images/products/sneaker-01.jpg', 0)"
    )
    db.commit()

    assert admin.delete("/api/admin/products/a-1").status_code == 204
    from app.config import REPO_DIR

    assert (REPO_DIR / "public/images/products/sneaker-01.jpg").exists()
