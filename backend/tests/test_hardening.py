"""Error format, privacy of logs and responses, limits, CORS."""

from __future__ import annotations

import logging
import uuid

import pytest
from conftest import ORIGIN, TEST_BOT_TOKEN, insert_product
from test_auth import init_data

from app import auth
from app.auth import HEADER

ALICE = 5001
PHONE = "50 765 43 21"


def as_user(user_id: int = ALICE) -> dict[str, str]:
    return {HEADER: init_data(user_id)}


def order(**overrides) -> dict:
    body = {
        "items": [{"product_id": "h-1", "size": "M", "qty": 1}],
        "customer_name": "Марія Шевченко", "phone": PHONE, "city": "Львів",
        "delivery_point": "Поштомат 123", "comment": "секретний коментар",
        "idempotency_key": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


@pytest.fixture
def shop(db):
    insert_product(db, "h-1", sizes='["M"]', price_supplier=271828)
    db.commit()


def test_auth_lifetimes_are_constants():
    assert auth.ADMIN_MAX_AGE_SECONDS == 3600
    assert auth.USER_MAX_AGE_SECONDS == 24 * 3600


@pytest.mark.parametrize(
    "method, url, headers, status, code",
    [
        ("get", "/api/favorites", {}, 401, "unauthorized"),
        ("get", "/api/admin/products", "user", 403, "forbidden"),
        ("get", "/api/products/nope", {}, 404, "not_found"),
        ("get", "/api/catalog?limit=999", {}, 422, "validation_error"),
        ("get", "/api/orders/mine", {}, 401, "unauthorized"),
    ],
)
def test_error_format_is_consistent(client, shop, method, url, headers, status, code):
    res = getattr(client, method)(url, headers=as_user() if headers == "user" else headers)
    assert res.status_code == status
    body = res.json()
    assert isinstance(body["detail"], str) and body["code"] == code


def test_validation_errors_do_not_echo_values(client, shop):
    res = client.post("/api/orders", json=order(phone="+48 123 456 789 secret"), headers=as_user())
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "validation_error"
    assert ["body", "phone"] in [e["loc"] for e in body["errors"]]
    assert "secret" not in res.text and "+48" not in res.text


def test_coded_conflicts_keep_their_code(client, shop):
    res = client.post("/api/orders", json=order(items=[{"product_id": "h-1", "size": "XL", "qty": 1}]),
                      headers=as_user())
    assert res.status_code == 409 and res.json()["code"] == "cart_changed"


def test_rate_limit_is_429_with_code(client, shop):
    client.app.state.limiters["orders"].limit = 2
    codes = [client.post("/api/orders", json=order(), headers=as_user()).status_code for _ in range(3)]
    assert codes == [201, 201, 429]
    assert client.post("/api/orders", json=order(), headers=as_user()).json()["code"] == "rate_limited"


@pytest.mark.parametrize("name", ["catalog", "favorites", "orders", "orders_read", "me", "upload", "admin"])
def test_every_limiter_exists(client, name):
    assert name in client.app.state.limiters


def test_oversized_json_body_is_rejected_before_parsing(client, shop):
    body = order(comment="x" * 70_000)
    res = client.post("/api/orders", json=body, headers=as_user())
    assert res.status_code == 413 and res.json()["code"] == "too_large"


def test_nothing_sensitive_is_logged(client, shop, notifier, caplog):
    caplog.set_level(logging.DEBUG)
    notifier.fail_for = {111: "http_403", 222: "network"}
    data = init_data(ALICE)
    res = client.post("/api/orders", json=order(), headers={HEADER: data})
    assert res.status_code == 201
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "order notification failed" in logged  # something useful is logged ...
    for secret in ("765", "Марія", "секретний", "Поштомат", data[:40], TEST_BOT_TOKEN, "AAFake"):
        assert secret not in logged, secret  # ... but no personal data, initData or token


def test_public_responses_never_expose_admin_fields(client, shop, db):
    insert_product(db, "h-draft", status="draft")
    db.commit()
    client.put("/api/favorites/h-1", headers=as_user())
    bodies = [
        client.get("/api/products").text,
        client.get("/api/products", params={"ids": "h-1,h-draft"}).text,
        client.get("/api/catalog").text,
        client.get("/api/products/h-1").text,
        client.get("/api/favorites", headers=as_user()).text,
    ]
    for text in bodies:
        for marker in ("price_supplier", "271828", "needs_review", "review_reasons", "edited_by_admin",
                       "h-draft", "source_channel", "duplicate_group"):
            assert marker not in text, marker


def test_cors_allows_only_the_configured_origin(client):
    ok = client.options("/api/orders", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-telegram-init-data",
    })
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    evil = client.options("/api/orders", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
    })
    assert "access-control-allow-origin" not in evil.headers
    assert "access-control-allow-origin" not in client.get(
        "/api/products", headers={"Origin": "https://evil.example"}).headers


def test_admin_age_is_enforced_but_user_age_is_longer(client, shop):
    two_hours_old = {HEADER: init_data(111, age=2 * 3600)}
    assert client.get("/api/admin/orders", headers=two_hours_old).status_code == 401
    assert client.get("/api/orders/mine", headers=two_hours_old).status_code == 200
    assert client.get("/api/me", headers=two_hours_old).json() == {"is_admin": False}
