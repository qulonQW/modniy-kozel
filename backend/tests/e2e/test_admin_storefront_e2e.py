"""Browser end-to-end: the spec's admin -> storefront scenario.

    RUN_E2E=1 .venv/Scripts/python -m pytest tests/e2e -s

Starts a real uvicorn on a free port with a throwaway database (7 seeded
products + one clean draft) and throwaway uploads, then drives Microsoft Edge:
  1. storefront shows "Футболка, модель 1" at 600 грн
  2. admin: change its price to 777 and publish the draft
  3. storefront reload: 777 грн and the new product are visible

Waits are tied to concrete server responses, never fixed sleeps.

Prices: the storefront's formatPrice (since the first demo commit) puts a
NO-BREAK SPACE (U+00A0) before "грн" and between thousands, so the price never
wraps. Text checks must use NBSP; a plain-space check prints False even though
the page is correct (that was the cause of the one-off False in step 8).
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

if os.environ.get("RUN_E2E") != "1":
    pytest.skip("browser e2e: set RUN_E2E=1", allow_module_level=True)
sync_api = pytest.importorskip("playwright.sync_api", reason="pip install -r requirements-e2e.txt")

from app import manage  # noqa: E402
from app.config import BACKEND_DIR  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.importer import import_files  # noqa: E402

NBSP = " "
DRAFT_ID = "e2e-draft-1"
DRAFT_TITLE = "E2E Худі для перевірки"
CHANNEL = os.environ.get("E2E_BROWSER_CHANNEL", "msedge")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    db_path = tmp_path / "e2e.sqlite3"
    conn = connect(db_path)
    init_db(conn)
    import_files(conn, manage.DEMO_FILE, None, mode="seed")
    conn.execute(
        "INSERT INTO products (id, title, description, category, price_retail, sizes, status, "
        "created_at, updated_at) VALUES (?, ?, '', 'Одяг', 1500, '[\"M\"]', 'draft', ?, ?)",
        (DRAFT_ID, DRAFT_TITLE, "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "ENV": "dev",
        "BOT_TOKEN": "",
        "DEV_ADMIN_ID": "1",
        "ADMIN_TELEGRAM_IDS": "1",
        "ALLOWED_ORIGIN": base,
        "DATABASE_PATH": str(db_path),
        "UPLOADS_DIR": str(tmp_path / "uploads"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=BACKEND_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/api/products", timeout=1)
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError(proc.stdout.read().decode(errors="replace"))
                time.sleep(0.1)
        yield base, db_path
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def card_text(page, needle: str) -> str:
    page.wait_for_selector(".card")
    for card in page.query_selector_all(".card"):
        text = card.text_content()
        if needle in text:
            return text
    return ""


def db_price(db_path: Path, pid: str) -> int:
    with sqlite3.connect(db_path) as c:
        return c.execute("SELECT price_retail FROM products WHERE id = ?", (pid,)).fetchone()[0]


def test_admin_edit_shows_on_storefront_after_reload(server):
    base, db_path = server
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(channel=CHANNEL, headless=True)
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        shop = ctx.new_page()
        admin = ctx.new_page()

        # 1. storefront before
        shop.goto(f"{base}/catalog.html")
        before = card_text(shop, "модель 1")
        assert f"600{NBSP}грн" in before, before

        # 2a. admin: price 777 on "Футболка, модель 1"
        admin.goto(f"{base}/admin/")
        admin.wait_for_selector(".product-row")
        with admin.expect_response(lambda r: "/api/admin/products?" in r.url and "status=published" in r.url):
            admin.select_option("#filter-status", "published")
        admin.wait_for_function("document.querySelector('#summary').textContent.startsWith('Знайдено: 7')")
        admin.locator(".product-row", has_text="Футболка, модель 1").click()
        admin.wait_for_selector("#edit-view:not([hidden])")
        admin.fill("input[name=price_retail]", "777")
        with admin.expect_response(lambda r: r.request.method == "PATCH") as patch:
            admin.click("#save")
        assert patch.value.status == 200, patch.value.text()
        assert db_price(db_path, "tshirt-01") == 777

        # 2b. admin: publish the clean draft
        admin.click("#back")
        with admin.expect_response(lambda r: "status=draft" in r.url):
            admin.select_option("#filter-status", "draft")
        admin.locator(".product-row", has_text=DRAFT_TITLE).click()
        admin.wait_for_selector("#edit-view:not([hidden])")
        admin.select_option("select[name=status]", "published")
        with admin.expect_response(lambda r: r.request.method == "PATCH") as patch:
            admin.click("#save")
        assert patch.value.status == 200, patch.value.text()

        # 3. storefront after reload
        with shop.expect_response(lambda r: r.url.endswith("/api/products")) as api:
            shop.reload()
        api_price = next(p["price"] for p in api.value.json() if p["id"] == "tshirt-01")
        raw = card_text(shop, "модель 1")
        normalized = " ".join(raw.split())
        new_card = card_text(shop, DRAFT_TITLE)
        print(f"\n  API price={api_price} | raw card={raw!r} | normalized={normalized!r}")
        print(f"  NBSP check={f'777{NBSP}грн' in raw} | plain-space check={'777 грн' in raw} "
              f"| new product visible={bool(new_card)}")

        assert api_price == 777
        assert f"777{NBSP}грн" in raw, raw  # exact rendered text
        assert "777 грн" not in raw  # documents why the plain-space check failed once
        assert new_card and f"1{NBSP}500{NBSP}грн" in new_card, new_card
        browser.close()
