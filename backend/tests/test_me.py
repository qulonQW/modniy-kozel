"""GET /api/me: tells the storefront whether to show the admin link."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_auth import ADMIN_ID, STRANGER_ID, init_data

from app.application import create_app
from app.auth import HEADER

REPO_DIR = Path(__file__).resolve().parents[2]


def me(client, data: str | None = None):
    res = client.get("/api/me", headers={HEADER: data} if data is not None else {})
    assert res.status_code == 200, res.text
    assert res.headers["cache-control"] == "no-store"
    return res.json()


def test_admin_with_real_init_data(client):
    assert me(client, init_data(ADMIN_ID)) == {"is_admin": True}


@pytest.mark.parametrize(
    "data",
    [
        None,  # plain browser, no Telegram
        "",
        "garbage",
        init_data(STRANGER_ID),  # valid Telegram user, not an admin
        init_data(ADMIN_ID, age=7200),  # expired
        init_data(ADMIN_ID, token="987654321:AAAnotherBotTokenAbcdefghijklmnopqrstu"),
    ],
)
def test_everyone_else_gets_false_not_an_error(client, data):
    assert me(client, data) == {"is_admin": False}


def test_prod_mode(settings):
    with TestClient(create_app(dataclasses.replace(settings, env="prod"))) as c:
        assert me(c, init_data(ADMIN_ID)) == {"is_admin": True}
        assert me(c, init_data(STRANGER_ID)) == {"is_admin": False}


def test_dev_bypass_matches_admin_api(settings):
    with TestClient(create_app(dataclasses.replace(settings, dev_admin_id=42))) as c:
        assert me(c) == {"is_admin": True}


def test_storefront_shows_admin_link_only_after_api_me():
    js = (REPO_DIR / "app.js").read_text(encoding="utf-8")
    assert "/api/me" in js and "is_admin" in js and "'./admin/'" in js
    for page in ("index.html", "catalog.html", "cart.html", "orders.html"):
        assert "admin" not in (REPO_DIR / page).read_text(encoding="utf-8").lower(), page


def test_admin_page_loads_telegram_sdk_so_init_data_survives_navigation():
    # Telegram passes initData in the URL hash of the first page only; the SDK
    # keeps it in sessionStorage, so /admin/ must load the SDK to read it.
    html = (REPO_DIR / "admin" / "index.html").read_text(encoding="utf-8")
    assert '<script src="https://telegram.org/js/telegram-web-app.js"></script>' in html


def test_storefront_html_and_js_are_never_cached_stale(client):
    for path in ("/", "/index.html", "/app.js", "/products.js", "/cart.js", "/style.css", "/admin/", "/admin/admin.js"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
