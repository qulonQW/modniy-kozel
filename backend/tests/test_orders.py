"""Orders: server-side prices, validation, idempotency, privacy, notifications."""

from __future__ import annotations

import uuid

import pytest
from conftest import insert_product
from test_auth import init_data

from app import notify
from app.auth import HEADER
from app.db import connect
from app.schemas import normalize_phone

ALICE, BOB = 5001, 5002
ADMINS = (111, 222)


def as_user(user_id: int) -> dict[str, str]:
    return {HEADER: init_data(user_id)}


def order_body(*items, key: str | None = None, **overrides) -> dict:
    body = {
        "items": list(items) or [{"product_id": "shoe-1", "size": "42", "qty": 1}],
        "customer_name": "Іван Петренко",
        "phone": "50 123 45 67",
        "city": "Київ",
        "delivery_point": "Відділення №12",
        "comment": "",
        "idempotency_key": key or str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


def post(client, body, user=ALICE):
    return client.post("/api/orders", json=body, headers=as_user(user))


@pytest.fixture
def shop(db):
    insert_product(db, "shoe-1", title="Кросівки", price_retail=2500, sizes='["41", "42"]')
    insert_product(db, "hood-1", title="Худі", price_retail=1300, sizes='["M", "L"]')
    insert_product(db, "nosize-1", title="Сумка", price_retail=900, sizes="[]")
    insert_product(db, "draft-1", status="draft", sizes='["42"]')
    insert_product(db, "hidden-1", status="hidden", sizes='["42"]')
    insert_product(db, "oos-1", in_stock=0, sizes='["42"]')
    db.commit()
    return db


# --- creation and pricing -------------------------------------------------------


def test_order_is_priced_on_the_server(client, shop):
    body = order_body(
        {"product_id": "shoe-1", "size": "42", "qty": 2, "price": 1},
        {"product_id": "hood-1", "size": "M", "qty": 1, "price": 1},
        total=3,
    )
    res = post(client, body)
    assert res.status_code == 201, res.text
    order = res.json()
    assert order["total"] == 2 * 2500 + 1300
    assert [(i["product_id"], i["qty"], i["price"]) for i in order["items"]] == [
        ("shoe-1", 2, 2500), ("hood-1", 1, 1300),
    ]
    assert order["status"] == "new" and order["phone"] == "+380501234567"
    stored = shop.execute("SELECT total, user_telegram_id FROM orders").fetchone()
    assert tuple(stored) == (6300, ALICE)


def test_same_line_twice_is_merged(client, shop):
    res = post(client, order_body({"product_id": "shoe-1", "size": "42", "qty": 1},
                                  {"product_id": "shoe-1", "size": "42", "qty": 2}))
    assert res.status_code == 201
    assert [(i["qty"]) for i in res.json()["items"]] == [3]


def test_product_without_sizes_needs_empty_size(client, shop):
    assert post(client, order_body({"product_id": "nosize-1", "size": "", "qty": 1})).status_code == 201
    res = post(client, order_body({"product_id": "nosize-1", "size": "M", "qty": 1}))
    assert res.status_code == 409


@pytest.mark.parametrize(
    "item, reason",
    [
        ({"product_id": "draft-1", "size": "42", "qty": 1}, "unavailable"),
        ({"product_id": "hidden-1", "size": "42", "qty": 1}, "unavailable"),
        ({"product_id": "missing-1", "size": "42", "qty": 1}, "unavailable"),
        ({"product_id": "oos-1", "size": "42", "qty": 1}, "out_of_stock"),
        ({"product_id": "shoe-1", "size": "48", "qty": 1}, "invalid_size"),
    ],
)
def test_unorderable_lines_are_rejected(client, shop, item, reason):
    res = post(client, order_body({"product_id": "shoe-1", "size": "41", "qty": 1}, item))
    assert res.status_code == 409
    data = res.json()
    assert data["code"] == "cart_changed"
    assert data["problems"] == [{"product_id": item["product_id"], "size": item["size"], "reason": reason}]
    assert shop.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_merged_quantity_over_limit_is_rejected(client, shop):
    res = post(client, order_body({"product_id": "shoe-1", "size": "42", "qty": 6},
                                  {"product_id": "shoe-1", "size": "42", "qty": 5}))
    assert res.status_code == 409 and res.json()["problems"][0]["reason"] == "too_many"


@pytest.mark.parametrize("qty", [0, -1, 11, "2.5", None])
def test_qty_validation(client, shop, qty):
    assert post(client, order_body({"product_id": "shoe-1", "size": "42", "qty": qty})).status_code == 422


def test_line_count_limit(client, shop):
    items = [{"product_id": "shoe-1", "size": "42", "qty": 1}] * 21
    assert post(client, order_body(*items)).status_code == 422
    assert post(client, order_body(items=[])).status_code == 422


def test_expected_total_mismatch_creates_nothing(client, shop):
    res = post(client, order_body(expected_total=2000))
    assert res.status_code == 409
    assert res.json()["code"] == "price_changed" and res.json()["total"] == 2500
    assert shop.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert post(client, order_body(expected_total=2500)).status_code == 201


# --- field validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+380 50 123 45 67", "+380501234567"),
        ("380501234567", "+380501234567"),
        ("050-123-45-67", "+380501234567"),
        ("(050) 123 45 67", "+380501234567"),
        ("501234567", "+380501234567"),
    ],
)
def test_phone_normalization(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "phone", ["", "12345", "+48 501 234 567", "0012345678", "50123456789", "50 123 45 6x", "+380 05 123 45 67"]
)
def test_bad_phones_are_rejected(client, shop, phone):
    assert post(client, order_body(phone=phone)).status_code == 422


@pytest.mark.parametrize(
    "field, value",
    [
        ("customer_name", "І"),
        ("customer_name", "x" * 101),
        ("customer_name", "Іван\nПетренко"),
        ("city", ""),
        ("city", "x" * 101),
        ("delivery_point", ""),
        ("delivery_point", "x" * 201),
        ("comment", "x" * 501),
        ("comment", "bad\x00char"),
        ("idempotency_key", "short"),
        ("idempotency_key", "bad key with spaces!!"),
    ],
)
def test_length_and_character_limits(client, shop, field, value):
    assert post(client, order_body(**{field: value})).status_code == 422


# --- idempotency and privacy --------------------------------------------------------


def test_same_idempotency_key_returns_the_existing_order(client, shop, notifier):
    body = order_body(key="a" * 32)
    first = post(client, body)
    second = post(client, {**body, "comment": "changed"})
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert shop.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert len(notifier.sent) == len(ADMINS)  # no second notification


def test_idempotency_key_is_per_user(client, shop):
    body = order_body(key="b" * 32)
    assert post(client, body, ALICE).json()["id"] != post(client, body, BOB).json()["id"]


def test_users_only_see_their_own_orders(client, shop):
    post(client, order_body())
    post(client, order_body(), BOB)
    alice = client.get("/api/orders/mine", headers=as_user(ALICE))
    assert alice.status_code == 200 and alice.headers["cache-control"] == "no-store"
    assert len(alice.json()) == 1
    assert {o["id"] for o in alice.json()}.isdisjoint(
        {o["id"] for o in client.get("/api/orders/mine", headers=as_user(BOB)).json()}
    )


def test_user_id_in_body_is_ignored(client, shop):
    res = post(client, order_body(user_telegram_id=BOB, user_id=BOB))
    assert res.status_code == 201
    assert shop.execute("SELECT user_telegram_id FROM orders").fetchone()[0] == ALICE


def test_orders_need_init_data(client, shop):
    assert client.post("/api/orders", json=order_body()).status_code == 401
    assert client.get("/api/orders/mine").status_code == 401


def test_non_admin_cannot_use_admin_order_api(client, shop):
    post(client, order_body())
    for method, url in [("get", "/api/admin/orders"), ("get", "/api/admin/orders/1"),
                        ("post", "/api/admin/orders/1/resend")]:
        assert getattr(client, method)(url, headers=as_user(ALICE)).status_code == 403
    assert client.patch("/api/admin/orders/1", json={"status": "confirmed"},
                        headers=as_user(ALICE)).status_code == 403


# --- admin -----------------------------------------------------------------------


def test_admin_lists_filters_and_updates(admin, shop):
    oid = post(admin, order_body()).json()["id"]
    data = admin.get("/api/admin/orders").json()
    assert data["total"] == 1
    order = data["items"][0]
    assert order["user_telegram_id"] == ALICE and order["notified"] is True
    assert "price_supplier" not in order["items"][0]

    res = admin.patch(f"/api/admin/orders/{oid}", json={"status": "confirmed"})
    assert res.status_code == 200 and res.json()["status"] == "confirmed"
    assert admin.get("/api/admin/orders", params={"status": "new"}).json()["total"] == 0
    assert admin.get("/api/admin/orders", params={"status": "confirmed"}).json()["total"] == 1
    assert admin.patch(f"/api/admin/orders/{oid}", json={"status": "paid"}).status_code == 422
    assert admin.patch("/api/admin/orders/999", json={"status": "new"}).status_code == 404
    # the shopper sees the new status
    mine = admin.get("/api/orders/mine", headers=as_user(ALICE)).json()
    assert mine[0]["status"] == "confirmed"


# --- notifications ------------------------------------------------------------------


def test_every_admin_is_notified_after_commit(client, shop, notifier):
    oid = post(client, order_body()).json()["id"]
    assert sorted(chat for chat, _ in notifier.sent) == list(ADMINS)
    text = notifier.sent[0][1]
    assert f"№ {oid}" in text and "+380501234567" in text and "Разом: 2 500 грн" in text
    assert f'href="tg://user?id={ALICE}"' in text
    assert shop.execute("SELECT notified, notify_attempts FROM orders").fetchone()[0] == 1


def test_order_is_saved_when_telegram_fails(client, shop, notifier):
    notifier.fail_for = {111: "http_403", 222: "network"}
    res = post(client, order_body())
    assert res.status_code == 201
    row = shop.execute("SELECT notified, notify_attempts FROM orders").fetchone()
    assert tuple(row) == (0, 2)
    rows = shop.execute("SELECT admin_id, status, attempts, last_error FROM order_notifications ORDER BY admin_id")
    assert [tuple(r) for r in rows] == [(111, "pending", 1, "http_403"), (222, "pending", 1, "network")]


def test_one_blocked_admin_does_not_block_the_other(client, shop, notifier):
    notifier.fail_for = {111: "http_403"}
    post(client, order_body())
    assert [chat for chat, _ in notifier.sent] == [222]
    statuses = dict(shop.execute("SELECT admin_id, status FROM order_notifications").fetchall())
    assert statuses == {111: "pending", 222: "sent"}


def _make_due(db):
    db.execute("UPDATE order_notifications SET next_attempt_at = '2000-01-01T00:00:00+00:00' "
               "WHERE status = 'pending'")
    db.commit()


def test_retry_marks_notified(client, shop, notifier, settings):
    notifier.fail_for = {111: "http_403"}
    post(client, order_body())
    conn = connect(settings.database_path)
    assert notify.process_due(conn, notifier) == 0  # not due yet (backoff)
    notifier.fail_for = {}
    _make_due(shop)
    assert notify.process_due(conn, notifier) == 1
    assert shop.execute("SELECT notified FROM orders").fetchone()[0] == 1
    assert [chat for chat, _ in notifier.sent] == [222, 111]
    assert notify.process_due(conn, notifier) == 0  # nothing left, nothing sent twice
    conn.close()


def test_retries_stop_after_max_attempts(client, shop, notifier, settings):
    notifier.fail_for = {111: "http_403", 222: "http_403"}
    post(client, order_body())
    conn = connect(settings.database_path)
    for _ in range(notify.MAX_ATTEMPTS + 2):
        _make_due(shop)
        notify.process_due(conn, notifier)
    rows = shop.execute("SELECT status, attempts FROM order_notifications").fetchall()
    assert {tuple(r) for r in rows} == {("failed", notify.MAX_ATTEMPTS)}
    assert notifier.calls == 2 * notify.MAX_ATTEMPTS
    conn.close()


def test_admin_resend(admin, shop, notifier):
    notifier.fail_for = {111: "http_403"}
    oid = post(admin, order_body()).json()["id"]
    notifier.fail_for = {}
    res = admin.post(f"/api/admin/orders/{oid}/resend")
    assert res.status_code == 200
    assert admin.get(f"/api/admin/orders/{oid}").json()["notified"] is True
    assert [chat for chat, _ in notifier.sent] == [222, 111]  # 222 did not get it twice
    # everyone has it: resend goes to all admins again
    admin.post(f"/api/admin/orders/{oid}/resend")
    assert [chat for chat, _ in notifier.sent] == [222, 111, 111, 222]
    assert admin.post("/api/admin/orders/999/resend").status_code == 404


def test_claimed_rows_are_not_sent_twice(client, shop, notifier, settings):
    notifier.fail_for = {111: "network", 222: "network"}
    post(client, order_body())
    _make_due(shop)
    shop.execute("UPDATE order_notifications SET next_attempt_at = '2999-01-01T00:00:00+00:00' WHERE admin_id = 111")
    shop.commit()
    notifier.fail_for = {}
    conn = connect(settings.database_path)
    notify.process_due(conn, notifier)
    assert [chat for chat, _ in notifier.sent] == [222]
    conn.close()


def test_admin_message_escapes_all_user_text(client, shop, notifier):
    shop.execute("UPDATE products SET title = '<b>Кросівки</b> & co' WHERE id = 'shoe-1'")
    shop.commit()
    evil = '<a href="https://evil.example">клік</a>'
    client.post(
        "/api/orders",
        json=order_body(customer_name=f"Іван {evil}"[:100], city="Київ <i>", delivery_point="<script>x</script>",
                        comment=f"{evil}\nдругий рядок"),
        headers={HEADER: init_data(ALICE, user='{"id":5001,"first_name":"<b>Bad</b>","username":"a_b"}')},
    )
    text = notifier.sent[0][1]
    assert "evil.example" in text  # the text is there ...
    assert '<a href="https://evil.example">' not in text  # ... but not as markup
    assert "&lt;a href=&quot;https://evil.example&quot;&gt;" in text
    assert "&lt;b&gt;Кросівки&lt;/b&gt; &amp; co" in text
    assert "&lt;script&gt;" in text and "<script>" not in text
    assert "&lt;b&gt;Bad&lt;/b&gt;" in text and "<b>Bad</b>" not in text
    assert "Київ &lt;i&gt;" in text


def test_bot_notifier_never_exposes_the_token():
    bot = notify.BotNotifier("123456789:AAFakeTokenForTestsOnly_abcdefghijklmn")
    assert "AAFake" not in repr(bot) and "AAFake" not in str(bot)
