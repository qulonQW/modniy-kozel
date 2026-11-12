"""API-level smoke test of the whole shopper -> admin flow."""

from __future__ import annotations

import uuid

from conftest import insert_product
from test_auth import init_data

from app.auth import HEADER

SHOPPER = 7007
ADMIN = 111


def test_browse_favorite_cart_order_admin(client, db, notifier):
    insert_product(db, "smoke-1", title="Кросівки Smoke", price_retail=3100, sizes='["42", "43"]', popular=1)
    insert_product(db, "smoke-2", title="Худі Smoke", price_retail=1500, sizes='["L"]')
    insert_product(db, "smoke-draft", status="draft", sizes='["42"]')
    db.commit()
    shopper = {HEADER: init_data(SHOPPER)}
    admin = {HEADER: init_data(ADMIN)}

    # 1. browse: home (popular), catalog search, product page
    assert client.get("/api/me", headers=shopper).json() == {"is_admin": False}
    home = client.get("/api/catalog", params={"popular": "true", "limit": 12}).json()["items"]
    assert [p["id"] for p in home] == ["smoke-1"]
    found = client.get("/api/catalog", params={"q": "худі"}).json()["items"]
    assert [p["id"] for p in found] == ["smoke-2"]
    product = client.get("/api/products/smoke-1").json()
    assert product["sizes"] == ["42", "43"] and product["price"] == 3100

    # 2. favorite
    assert client.put("/api/favorites/smoke-2", headers=shopper).status_code == 204
    assert [p["id"] for p in client.get("/api/favorites", headers=shopper).json()] == ["smoke-2"]

    # 3. cart (client-side): the cart page looks its products up by id
    cart = client.get("/api/products", params={"ids": "smoke-1,smoke-2"}).json()
    total = 2 * 3100 + 1500
    assert sum(p["price"] * q for p, q in zip(sorted(cart, key=lambda p: p["id"]), (2, 1))) == total

    # 4. order (twice with the same key: one order)
    body = {
        "items": [{"product_id": "smoke-1", "size": "43", "qty": 2},
                  {"product_id": "smoke-2", "size": "L", "qty": 1, "price": 1}],
        "customer_name": "Олена Коваль", "phone": "+380 67 111 22 33", "city": "Одеса",
        "delivery_point": "Відділення №5", "comment": "", "idempotency_key": str(uuid.uuid4()),
        "expected_total": total, "total": 1,
    }
    created = client.post("/api/orders", json=body, headers=shopper)
    assert created.status_code == 201, created.text
    again = client.post("/api/orders", json=body, headers=shopper)
    assert again.status_code == 200 and again.json() == created.json()
    assert created.json()["total"] == total and "id" not in created.json()  # no order number for shoppers
    order_id = db.execute("SELECT id FROM orders").fetchone()[0]

    mine = client.get("/api/orders/mine", headers=shopper).json()
    assert [(o["created_at"], o["status"]) for o in mine] == [(created.json()["created_at"], "new")]

    # 5. admins were notified (both), once
    assert sorted(chat for chat, _ in notifier.sent) == [111, 222]
    assert all(f"№ {order_id}" in text for _, text in notifier.sent)

    # 6. admin sees it, confirms it; the shopper sees the new status
    assert client.get("/api/me", headers=admin).json() == {"is_admin": True}
    listed = client.get("/api/admin/orders", params={"status": "new"}, headers=admin).json()
    assert [o["id"] for o in listed["items"]] == [order_id]
    detail = client.get(f"/api/admin/orders/{order_id}", headers=admin).json()
    assert detail["user_telegram_id"] == SHOPPER and detail["phone"] == "+380671112233"
    assert detail["total"] == total and detail["notified"] is True
    assert client.patch(f"/api/admin/orders/{order_id}", json={"status": "confirmed"},
                        headers=admin).status_code == 200
    assert client.get("/api/orders/mine", headers=shopper).json()[0]["status"] == "confirmed"

    # the shopper cannot use the admin API
    assert client.get("/api/admin/orders", headers=shopper).status_code == 403
