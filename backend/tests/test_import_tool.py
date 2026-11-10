"""Tests for tools/import_products.py (Telegram HTML export -> import-json files)."""

from __future__ import annotations

import html
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.config import REPO_DIR
from app.importer import import_files
from app import manage
from conftest import insert_product

spec = importlib.util.spec_from_file_location("import_products", REPO_DIR / "tools" / "import_products.py")
tool = importlib.util.module_from_spec(spec)
sys.modules["import_products"] = tool  # dataclasses look the module up while executing it
spec.loader.exec_module(tool)

CONFIG = tool.load_config(REPO_DIR / "tools" / "import_config.json")
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"

PRODUCT = """Кросівки Nike Air Max
Колір : Чорний;Білий;
Розмір : 40-45
Маломірять на розмір
Дроп ціна : 1 500 UAH
Купити собі : 2100 UAH
Є опт від 3 пар : 1400 UAH
Зробити замовлення : @supplier_manager
Доп фото і заміри : https://t.me/+abcdef
Instagram: some_shop
Тел +380 67 123 45 67"""


# --- synthetic export ------------------------------------------------------


class Export:
    """Builds a minimal Telegram Desktop HTML export in a temp folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "photos").mkdir(parents=True)
        self.parts: list[str] = []
        self.next_id = 100
        self.photo_no = 0

    def photo(self, data: bytes = JPEG, ext: str = "jpg") -> str:
        self.photo_no += 1
        name = f"photo_{self.photo_no}@01-01-2026_12-00-00.{ext}"
        (self.root / "photos" / name).write_bytes(data + bytes([self.photo_no]))
        return f"photos/{name}"

    def message(self, text: str = "", photos: int | list[str] = 0, joined: bool = False,
                date: str = "01.01.2026 12:00:00 UTC+02:00", msg_id: int | None = None) -> int:
        msg_id = msg_id or self.next_id
        self.next_id = msg_id + 1
        hrefs = photos if isinstance(photos, list) else [self.photo() for _ in range(photos)]
        media = "".join(
            f'<div class="media_wrap clearfix"><a class="photo_wrap clearfix pull_left" '
            f'href="{h}"><img class="photo" src="{h}"></a></div>'
            for h in hrefs
        )
        body = "<br>".join(html.escape(line) for line in text.splitlines())
        text_div = f'<div class="text">\n {body}\n</div>' if text else ""
        cls = "message default clearfix" + (" joined" if joined else "")
        self.parts.append(
            f'<div class="{cls}" id="message{msg_id}"><div class="body">'
            f'<div class="pull_right date details" title="{date}">12:00</div>'
            f"{media}{text_div}</div></div>"
        )
        return msg_id

    def album(self, text: str, photos: int = 3, caption_at: int = 0, **kw) -> int:
        first = None
        for i in range(photos):
            mid = self.message(text if i == caption_at else "", 1, joined=i > 0, **kw)
            first = first or mid
        return first

    def service(self) -> None:
        self.parts.append('<div class="message service" id="message-1"><div class="body details">1 January</div></div>')

    def write(self) -> Path:
        page = '<html><body><div class="history">' + "".join(self.parts) + "</div></body></html>"
        (self.root / "messages.html").write_text(page, encoding="utf-8")
        return self.root


@pytest.fixture
def export(tmp_path):
    return Export(tmp_path / "ChatExport")


def build(export: Export) -> tool.Result:
    return tool.build_products(export.write(), json.loads(json.dumps(CONFIG)))


def only(result: tool.Result) -> dict:
    assert len(result.products) == 1, result.skipped
    return result.products[0].record


# --- album grouping --------------------------------------------------------


def test_album_with_caption_first_is_one_product(export):
    first = export.album(PRODUCT, photos=4)
    record = only(build(export))

    assert len(record["images"]) == 4
    assert record["source_message_id"] == first
    assert record["id"] == tool.product_id("avtorik_shop", first)
    assert "caption_not_first" not in record["review_reasons"]


def test_caption_not_in_first_block_needs_review(export):
    export.album(PRODUCT, photos=3, caption_at=2)
    record = only(build(export))

    assert len(record["images"]) == 3
    assert "caption_not_first" in record["review_reasons"]
    assert record["needs_review"] is True


def test_two_captioned_albums_in_same_second_are_split(export):
    export.album(PRODUCT, photos=2)
    export.album(PRODUCT.replace("Nike Air Max", "Adidas Samba"), photos=2)
    # the second album's first block is "joined" and has the same timestamp
    export.parts[2] = export.parts[2].replace('class="message default clearfix"', 'class="message default clearfix joined"')
    result = build(export)

    assert sorted(p.record["title"] for p in result.products) == ["Кросівки Adidas Samba", "Кросівки Nike Air Max"]
    assert all(len(p.record["images"]) == 2 for p in result.products)


def test_different_second_or_service_block_breaks_group(export):
    export.message(PRODUCT, 1)
    export.message("", 1, joined=True, date="01.01.2026 12:00:05 UTC+02:00")
    export.message(PRODUCT, 1)
    export.service()
    export.message("", 1, joined=True)
    result = build(export)

    assert len(result.products) == 2
    assert result.skipped["no_caption"] and len(result.skipped["no_caption"]) == 2


def test_skip_rules(export):
    restock = export.album("ПОПОВНЕННЯ 🔥\nДроп ціна : 900 UAH", photos=2)
    announcement = export.message("Завтра додамо нові позиції 🤝")
    no_photo = export.message(PRODUCT)
    ad = export.message("Дропшипінг | ОПТ | Shop", 1)
    no_caption = export.message("", 2)
    result = build(export)

    assert result.products == []
    assert result.skipped["restock_announcement"] == [restock]
    assert result.skipped["text_announcement"] == [announcement]
    assert result.skipped["no_photo"] == [no_photo]
    assert result.skipped["ad_or_announcement"] == [ad]
    assert result.skipped["no_caption"] == [no_caption]


# --- prices ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,drop,reference,unlabeled",
    [
        ("Дроп ціна : 1 500 UAH\nКупити собі : 2100 UAH", {1500}, {2100}, False),
        ("Дроп ціна :  999 грн", {999}, set(), False),
        ("Дроп ціна :\n1200 ₴", {1200}, set(), False),
        ("Ціна купити собі : 800 uah", set(), {800}, False),
        ("Є опт від 3 пар : 1400 UAH\nВід 5 шт : 700 грн(від кількості)", set(), set(), False),
        ("Ціна: 1300 грн", set(), set(), True),
        ("Дроп ціна : 900 UAH\nДроп ціна : 950 UAH", {900, 950}, set(), False),
        ("Артикул 1234567", set(), set(), False),
    ],
)
def test_extract_prices(text, drop, reference, unlabeled):
    p = tool.extract_prices(text)
    assert (p.drop, p.reference, p.unlabeled) == (drop, reference, unlabeled)


@pytest.mark.parametrize(
    "supplier,retail", [(750, 980), (1500, 1950), (1234, 1600), (999, 1300), (1000, 1300)]
)
def test_retail_price_rounds_to_10(supplier, retail):
    from decimal import Decimal

    assert tool.retail_price(supplier, Decimal("1.3"), 10) == retail


def test_price_flags_and_private_split(export):
    export.message("Худі Nike\nРозмір : M\nКупити собі : 1500 UAH", 1)
    export.message("Худі Adidas\nРозмір : M\nДроп ціна : 900 UAH\nДроп ціна : 950 UAH", 1)
    export.message("Худі Puma\nРозмір : M\nДроп ціна : 10 UAH", 1)
    export.message("Худі Fila\nРозмір : M\nДроп ціна : 1000 UAH\nКупити собі : 1400 UAH", 1)
    products = {p.record["title"]: p for p in build(export).products}

    assert products["Худі Nike"].record["price_retail"] == 0
    assert "no_drop_price" in products["Худі Nike"].record["review_reasons"]
    assert products["Худі Nike"].cost == {"price_supplier": None, "price_reference": 1500}
    assert "multiple_drop_prices" in products["Худі Adidas"].record["review_reasons"]
    assert "price_out_of_range" in products["Худі Puma"].record["review_reasons"]
    fila = products["Худі Fila"]
    assert (fila.record["price_retail"], fila.cost) == (1300, {"price_supplier": 1000, "price_reference": 1400})
    assert fila.record["review_reasons"] == []
    assert "price_supplier" not in fila.record and "price_reference" not in fila.record


# --- sizes -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("S,M,L;XL;", ["S", "M", "L", "XL"]),
        ("S-XL", ["S", "M", "L", "XL"]),
        ("M,L,XL,XXL", ["M", "L", "XL", "XXL"]),
        ("2XL 3XL", ["XXL", "XXXL"]),
        ("40-45", ["40", "41", "42", "43", "44", "45"]),
        ("40 41 42", ["40", "41", "42"]),
        ("41.5 42 43", ["41.5", "42", "43"]),
        ("42(27cm) 43(27.5cm)", ["42", "43"]),
        ("Onesize", ["One size"]),
        ("Універсальний;", ["One size"]),
        ("30х20х10", []),
        ("Маломірять на розмір", None),
        ("М,Л", ["M"]),  # Cyrillic М is read as M; Л is not a size
    ],
)
def test_parse_sizes(value, expected):
    assert tool.parse_sizes(value) == expected


def test_sizes_from_lines_after_empty_label():
    text = "Кросівки Nike\nРозмір :\n42(27cm)\n43(27.5cm) 44(28cm)\nМаломірять на розмір\nДроп ціна : 1000 UAH"
    assert tool.extract_sizes(text) == (["42", "43", "44"], None)


def test_sizes_unparsed_and_missing():
    assert tool.extract_sizes("Розмір : уточнюйте")[1] == "sizes_unparsed"
    assert tool.extract_sizes("Кросівки\nДроп ціна : 1000 UAH")[1] == "sizes_missing"
    assert tool.extract_sizes("Кросівки 40-45\nДроп ціна : 1000 UAH") == (
        ["40", "41", "42", "43", "44", "45"], None
    )


# --- category, title, description ------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Кросівки New Balance 9060", "Взуття"),
        ("Черевики Timberland", "Взуття"),
        ("Найпопулярніше худі", "Одяг"),
        ("Zip-Hoodie Nike Nocta", "Одяг"),
        ("Сумка від Coach", None),
        ("Месенджер від Nike", None),
        ("Кепка від Nike NSW", None),
        ("Новинка Nike", None),
    ],
)
def test_detect_category(title, expected):
    assert tool.detect_category(title, title, CONFIG["keywords"]) == expected


def test_description_drops_prices_contacts_and_links():
    title = tool.extract_title(PRODUCT)
    description = tool.extract_description(PRODUCT, title)

    assert title == "Кросівки Nike Air Max"
    assert description == "Колір: Чорний, Білий\nМаломірять на розмір"


def test_title_skips_contact_and_price_lines_and_is_limited():
    text = "@shop_manager\nДроп ціна : 900 UAH\n" + "Футболка " + "x" * 200
    title = tool.extract_title(text)
    assert title.startswith("Футболка") and len(title) == 100


def test_contacts_never_survive_cleaning():
    dirty = "Колір чорний @user t.me/x https://a.b/c www.x.com +380671234567 067 123 45 67 <b>x</b>"
    clean = tool.clean_text(dirty)
    for marker in ("@user", "t.me", "https", "www", "380", "067", "<b>"):
        assert marker not in clean


def test_generic_title_needs_review(export):
    export.message("Найпопулярніше худі\nРозмір : M\nДроп ціна : 900 UAH", 1)
    record = only(build(export))
    assert record["review_reasons"] == ["generic_title"]


# --- photos ----------------------------------------------------------------


def test_photo_validation(export):
    good = export.photo()
    exe = export.photo(b"MZ\x90\x00", ext="jpg")
    big = export.photo(JPEG + b"\x00" * (CONFIG["max_photo_bytes"] + 1))
    export.message(PRODUCT, [good, exe, big, "photos/../../secret.jpg", "photos/missing.jpg"])
    result = build(export)
    record = only(result)

    assert len(record["images"]) == 1
    assert "photo_rejected" in record["review_reasons"]
    reasons = {k: len(v) for k, v in result.photo_issues.items()}
    assert reasons == {"photo_bad_type": 1, "photo_too_large": 1, "photo_bad_path": 1, "photo_missing": 1}


def test_all_photos_invalid_skips_product(export):
    export.message(PRODUCT, [export.photo(b"GIF89a", ext="jpg")])
    result = build(export)
    assert result.products == [] and result.skipped["no_valid_photo"]


# --- end to end: files, dry run, idempotent DB import -----------------------


def run_tool(export_dir: Path, out: Path, *extra: str) -> None:
    tool.main([
        "--input", str(export_dir), "--out-public", str(out / "public.json"),
        "--out-costs", str(out / "private" / "costs.json"), "--report", str(out / "report.md"),
        "--uploads-dir", str(out / "uploads"), *extra,
    ])


def test_dry_run_writes_only_the_report(export, tmp_path):
    export.album(PRODUCT, photos=2)
    out = tmp_path / "out"
    run_tool(export.write(), out, "--dry-run")

    assert sorted(p.name for p in out.rglob("*")) == ["report.md"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "Nike" not in report and "supplier_manager" not in report


def test_full_pipeline_is_idempotent_and_safe(export, tmp_path, conn):
    export.album(PRODUCT, photos=3)
    export.album("Худі Stone Island\nРозмір : S,M\nДроп ціна : 1000 UAH", photos=2)
    export.message("Сумка від Coach\nРозмір : 30х20х10\nДроп ціна : 1200 UAH", 1)
    out = tmp_path / "out"
    export_dir = export.write()

    run_tool(export_dir, out)
    first = (out / "public.json").read_bytes()
    files = sorted(p.name for p in (out / "uploads" / "products").iterdir())
    run_tool(export_dir, out)

    assert (out / "public.json").read_bytes() == first
    assert sorted(p.name for p in (out / "uploads" / "products").iterdir()) == files
    assert len(files) == 6
    assert "price_supplier" not in first.decode() and "price_reference" not in first.decode()
    assert "1500" not in first.decode()  # supplier price stays private
    costs = json.loads((out / "private" / "costs.json").read_text(encoding="utf-8"))
    assert {"price_supplier": 1500, "price_reference": 2100} in costs.values()

    # DB: seed + edited row must survive two imports
    import_files(conn, manage.DEMO_FILE, None, mode="seed")
    seeded = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    public, private = out / "public.json", out / "private" / "costs.json"

    r1 = import_files(conn, public, private)
    edited_id = next(p["id"] for p in json.loads(first) if "Nike" in p["title"])
    conn.execute("UPDATE products SET title = 'Edited', edited_by_admin = 1 WHERE id = ?", (edited_id,))
    conn.commit()
    r2 = import_files(conn, public, private)

    assert (r1.inserted, r1.errors) == (3, [])
    assert (r2.inserted, r2.updated, r2.protected, r2.unchanged) == (0, 0, 1, 2)
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 10
    rows = conn.execute("SELECT status, needs_review FROM products WHERE source_channel = 'avtorik_shop'").fetchall()
    assert {r["status"] for r in rows} == {"draft"}
    assert conn.execute("SELECT title FROM products WHERE id = ?", (edited_id,)).fetchone()[0] == "Edited"
    assert conn.execute(
        "SELECT * FROM products WHERE source_channel IS NULL ORDER BY id"
    ).fetchall() == seeded
    bag = conn.execute("SELECT needs_review, review_reasons, category FROM products WHERE title LIKE 'Сумка%'").fetchone()
    assert tuple(bag) == (1, "category_unknown", "Одяг")
