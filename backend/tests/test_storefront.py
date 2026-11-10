from __future__ import annotations

import pytest

from app.config import REPO_DIR


@pytest.mark.parametrize("path", ["/", "/index.html", "/catalog.html", "/product.html", "/cart.html",
                                  "/checkout.html", "/orders.html", "/app.js", "/products.js", "/style.css",
                                  "/public/images/logo/logo-mark.png"])
def test_storefront_files_are_served(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", [
    "/.env", "/.gitignore", "/package.json", "/index.js", "/backend/.env", "/backend/data/shop.sqlite3",
    "/tools/import_products.py", "/import/private/costs.json", "/import/products.public.json",
    "/import/import_report.md", "/..%2Fbackend%2F.env", "/%2e%2e/backend/.env", "/admin.js",
])
def test_nothing_else_from_repo_root_is_served(client, path):
    assert client.get(path).status_code == 404


def test_admin_and_api_are_not_shadowed(client):
    assert client.get("/admin/").status_code == 200
    assert client.get("/admin", follow_redirects=False).status_code in (307, 308)
    assert client.get("/api/products").status_code == 200


def test_storefront_uses_api_not_hardcoded_products():
    js = (REPO_DIR / "products.js").read_text(encoding="utf-8")
    assert "/api/products" in js and "const PRODUCTS = [];" in js
    assert "sneaker-01" not in js
    for page in ("index.html", "catalog.html", "product.html", "cart.html", "checkout.html"):
        assert "PRODUCTS_READY.then" in (REPO_DIR / page).read_text(encoding="utf-8"), page


def test_storefront_never_renders_html_strings():
    """Product data comes from the admin and the supplier import, so the
    storefront builds DOM with createElement/textContent only."""
    files = ["app.js", "cart.js", "products.js", "index.html", "catalog.html", "product.html",
             "cart.html", "checkout.html", "orders.html"]
    for name in files:
        text = (REPO_DIR / name).read_text(encoding="utf-8")
        for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "MK.esc"):
            assert api not in text, f"{name}: {api}"
