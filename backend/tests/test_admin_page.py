from __future__ import annotations

import re

from app.config import REPO_DIR

ADMIN_DIR = REPO_DIR / "admin"


def test_admin_page_is_served_with_csp(client):
    res = client.get("/admin/")
    assert res.status_code == 200
    assert "Товари" in res.text
    csp = res.headers["content-security-policy"]
    assert "script-src 'self' https://telegram.org" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert client.get("/admin/admin.js").status_code == 200


def test_admin_page_needs_no_auth_but_data_does(client):
    assert client.get("/admin/").status_code == 200
    assert client.get("/api/admin/products").status_code == 401


def test_admin_sources_never_use_unsafe_html_apis():
    js = (ADMIN_DIR / "admin.js").read_text(encoding="utf-8")
    for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "localStorage", "sessionStorage"):
        assert api not in js, api


def test_admin_html_has_no_inline_scripts_or_handlers():
    html = (ADMIN_DIR / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S)
    assert all("src=" in attrs and not body.strip() for attrs, body in scripts)
    assert not re.search(r"\son[a-z]+=", html)
