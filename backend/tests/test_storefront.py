from __future__ import annotations

import pytest

from app.config import REPO_DIR


@pytest.mark.parametrize("path", ["/", "/index.html", "/catalog.html", "/product.html", "/cart.html",
                                  "/checkout.html", "/orders.html", "/favorites.html", "/app.js", "/products.js", "/style.css",
                                  "/brand/texts.js", "/brand/theme.css", "/brand/logo.png"])
def test_storefront_files_are_served(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", [
    "/.env", "/.gitignore", "/package.json", "/index.js", "/backend/.env", "/backend/data/shop.sqlite3",
    "/tools/import_products.py", "/import/private/costs.json", "/import/products.public.json",
    "/import/import_report.md", "/..%2Fbackend%2F.env", "/%2e%2e/backend/.env", "/admin.js",
    "/texts.js", "/brand/brand.json", "/brand/logo-full.jpg", "/brand/..%2F..%2Fbackend%2F.env",
    "/brands/modniy-kozel/brand.json", "/public/images/logo/logo-mark.png",
])
def test_nothing_else_from_repo_root_is_served(client, path):
    assert client.get(path).status_code == 404


def test_admin_and_api_are_not_shadowed(client):
    assert client.get("/admin/").status_code == 200
    assert client.get("/admin", follow_redirects=False).status_code in (307, 308)
    assert client.get("/api/products").status_code == 200


def test_storefront_uses_api_not_hardcoded_products():
    js = (REPO_DIR / "products.js").read_text(encoding="utf-8")
    assert "/api/products?" in js and "ids:" in js and "const PRODUCTS = [];" in js
    assert "sneaker-01" not in js
    # cart pages wait for the cart's products; the others fetch their own data
    for page in ("cart.html", "checkout.html"):
        assert "PRODUCTS_READY.then" in (REPO_DIR / page).read_text(encoding="utf-8"), page
    home = (REPO_DIR / "index.html").read_text(encoding="utf-8")
    assert "/api/catalog?" in home and "popular" in home and "HOME_LIMIT" in home
    product = (REPO_DIR / "product.html").read_text(encoding="utf-8")
    assert "/api/products/${encodeURIComponent(id)}" in product
    # the catalog pages through the server-side search instead of the full list
    catalog = (REPO_DIR / "catalog.html").read_text(encoding="utf-8")
    assert "/api/catalog?" in catalog and "AbortController" in catalog


def test_storefront_never_renders_html_strings():
    """Product data comes from the admin and the supplier import, so the
    storefront builds DOM with createElement/textContent only."""
    files = ["app.js", "cart.js", "products.js", "index.html", "catalog.html", "product.html",
             "cart.html", "checkout.html", "orders.html", "favorites.html", "brands/modniy-kozel/texts.js"]
    for name in files:
        text = (REPO_DIR / name).read_text(encoding="utf-8")
        for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "MK.esc"):
            assert api not in text, f"{name}: {api}"


def test_html_uses_versioned_assets_that_are_cached_forever(client):
    import re

    html = client.get("/catalog.html")
    assert html.headers["cache-control"] == "no-cache"
    refs = dict(re.findall(r'"\./(app\.js|style\.css|cart\.js|products\.js|brand/[a-z]+\.[a-z]+)\?v=([0-9a-f]{12})"', html.text))
    assert {"app.js", "style.css", "products.js", "cart.js", "brand/theme.css", "brand/logo.png"} <= set(refs)
    fresh = client.get(f"/app.js?v={refs['app.js']}")
    assert fresh.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert fresh.headers["content-type"].startswith("text/javascript")
    # an old or missing version is never cached for long
    assert client.get("/app.js?v=000000000000").headers["cache-control"] == "no-cache"
    assert client.get("/app.js").headers["cache-control"] == "no-cache"


def test_unchanged_html_is_a_304(client):
    first = client.get("/index.html")
    again = client.get("/index.html", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b""


def test_shoppers_never_see_order_numbers():
    for name in ("checkout.html", "orders.html", "brands/modniy-kozel/texts.js"):
        text = (REPO_DIR / name).read_text(encoding="utf-8")
        assert "order.id" not in text and "o.id" not in text and "№ ${" not in text, name
