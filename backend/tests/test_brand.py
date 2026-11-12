"""Brands: one folder per shop (brands/<slug>/), the core names no shop.

These tests are the contract that keeps "make a new shop" a copy-and-edit
job: every brand folder must be complete and match the reference brand,
and nothing outside brands/ may hard-code a shop's name or colours.
"""

from __future__ import annotations

import re
import shutil

import pytest
from fastapi.testclient import TestClient

from app import brand as brand_module
from app.application import STOREFRONT_FILES, create_app
from app.brand import BRANDS_DIR, SERVED_FILES, load_brand
from app.config import REPO_DIR, ConfigError

REFERENCE = "modniy-kozel"
BRANDS = sorted(p.name for p in BRANDS_DIR.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))
PAGES = [n for n in STOREFRONT_FILES if n.endswith(".html")]
CORE_FILES = [
    *STOREFRONT_FILES,
    "admin/index.html", "admin/admin.js", "admin/admin.css",
    *(str(p.relative_to(REPO_DIR)) for p in (REPO_DIR / "backend" / "app").glob("*.py")),
]


def css_vars_defined(text: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", text))


def css_vars_used(text: str) -> set[str]:
    return set(re.findall(r"var\((--[a-z0-9-]+)", text))


def text_keys(js: str) -> set[str]:
    """Keys of the TEXTS object, e.g. 'checkout', 'nameLabel'."""
    return set(re.findall(r"^\s*([A-Za-z_]\w*)\s*:", js, flags=re.M))


def read(rel: str) -> str:
    return (REPO_DIR / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("slug", BRANDS)
def test_every_brand_folder_is_complete(slug):
    b = load_brand(slug)  # raises ConfigError with the reason
    for name in SERVED_FILES:
        assert (b.directory / name).stat().st_size > 0, name


@pytest.mark.parametrize("slug", BRANDS)
def test_theme_defines_every_variable_the_pages_use(slug):
    style = read("style.css")
    needed = css_vars_used(style) - css_vars_defined(style)
    needed -= {v for v in needed if v.startswith("--tg-")}  # set by Telegram
    defined = css_vars_defined((BRANDS_DIR / slug / "theme.css").read_text(encoding="utf-8"))
    assert needed <= defined, f"theme.css of {slug} lacks {sorted(needed - defined)}"


@pytest.mark.parametrize("slug", BRANDS)
def test_texts_have_the_same_keys_as_the_reference(slug):
    ref = text_keys((BRANDS_DIR / REFERENCE / "texts.js").read_text(encoding="utf-8"))
    own = text_keys((BRANDS_DIR / slug / "texts.js").read_text(encoding="utf-8"))
    assert own == ref, f"missing {sorted(ref - own)}, extra {sorted(own - ref)}"


@pytest.mark.parametrize("slug", BRANDS)
def test_no_shop_is_named_outside_its_brand_folder(slug):
    b = load_brand(slug)
    needles = {b.name, *(line for line in b.hero_lines if len(line) > 3)}
    for rel in CORE_FILES:
        text = read(rel)
        for needle in needles:
            assert needle not in text, f"{rel} hard-codes '{needle}': use {{{{brand.*}}}} or MK.shopName"


def test_shared_css_has_no_colours_of_its_own():
    style = read("style.css")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", style), "put colours in brands/<slug>/theme.css"


def test_every_page_loads_the_brand_before_the_shared_css():
    for page in PAGES:
        html = read(page)
        assert html.index('href="./brand/theme.css"') < html.index('href="./style.css"'), page
        assert "<title>" in html and "{{brand.name}}</title>" in html, page
        assert 'href="./brand/logo.png"' in html, page


def test_pages_are_filled_with_the_brand(client):
    b = load_brand(REFERENCE)
    for page in PAGES:
        html = client.get(f"/{page}").text
        assert "{{" not in html, page
        assert f"{b.name}</title>" in html and f'lang="{b.lang}"' in html, page
    home = client.get("/").text
    assert "<br>".join(b.hero_lines) in home and b.tagline in home
    admin = client.get("/admin/").text
    assert "{{" not in admin and f"Адмінка · {b.name}" in admin


def make_brand(tmp_path, slug="test-shop", **changes):
    """A copy of the reference brand under tmp_path/brands/<slug>."""
    target = tmp_path / "brands" / slug
    shutil.copytree(BRANDS_DIR / REFERENCE, target)
    if changes:
        import json

        data = json.loads((target / "brand.json").read_text(encoding="utf-8"))
        data.update(changes)
        (target / "brand.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return target


def test_switching_brand_changes_name_logo_and_theme(tmp_path, monkeypatch, settings):
    target = make_brand(tmp_path, name="Друга <Крамниця> & Co", short_name="ДК", hero_lines=["Друга", "Крамниця"])
    (target / "theme.css").write_bytes(b":root { --bg: #fafafa; }\n")
    monkeypatch.setattr(brand_module, "BRANDS_DIR", tmp_path / "brands")
    app = create_app(settings.__class__(**{**settings.__dict__, "brand": "test-shop"}))
    with TestClient(app) as c:
        html = c.get("/catalog.html").text
        # escaped exactly once: brand text can never inject markup
        assert "<title>Каталог — Друга &lt;Крамниця&gt; &amp; Co</title>" in html
        assert c.get("/").text.count("Друга<br>Крамниця") == 1
        assert c.get("/brand/theme.css").text == ":root { --bg: #fafafa; }\n"
        assert c.get("/brand/logo.png").headers["content-type"] == "image/png"


@pytest.mark.parametrize("slug", ["../modniy-kozel", "Modniy", "", "a/b", "nope-not-here"])
def test_bad_brand_names_refuse_to_start(slug):
    with pytest.raises(ConfigError):
        load_brand(slug)


@pytest.mark.parametrize("change", [
    {"fonts_url": "https://evil.example/font.css"},
    {"hero_lines": []},
    {"hero_lines": ["a", "b", "c", "d"]},
    {"name": ""},
    {"short_name": "TOOLONG"},
    {"lang": "ukrainian"},
])
def test_invalid_brand_json_refuses_to_start(tmp_path, change):
    make_brand(tmp_path, **change)
    with pytest.raises(ConfigError):
        load_brand("test-shop", tmp_path / "brands")


def test_missing_brand_file_refuses_to_start(tmp_path):
    (make_brand(tmp_path) / "theme.css").unlink()
    with pytest.raises(ConfigError, match="theme.css"):
        load_brand("test-shop", tmp_path / "brands")
