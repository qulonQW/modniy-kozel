from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from urllib.parse import quote, urlencode

import pytest
from fastapi.testclient import TestClient

from app.application import create_app
from app.auth import HEADER, InvalidInitData, verify_init_data
from app.config import BACKEND_DIR, ConfigError
from conftest import TEST_BOT_TOKEN

ADMIN_ID = 111  # in settings.admin_telegram_ids
STRANGER_ID = 999
URL = "/api/admin/products"


def sign(fields: dict[str, str], token: str = TEST_BOT_TOKEN) -> str:
    """Builds initData exactly as Telegram does (independent of app code)."""
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest}, quote_via=quote)


def init_data(user_id: int = ADMIN_ID, age: int = 0, token: str = TEST_BOT_TOKEN, **extra) -> str:
    fields = {
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(
            {"id": user_id, "first_name": "Брат", "username": "brother", "language_code": "uk"},
            ensure_ascii=False, separators=(",", ":"),
        ),
        "auth_date": str(int(time.time()) - age),
        "signature": "c2lnbmF0dXJlLWZvci10aGlyZC1wYXJ0aWVz",
        **extra,
    }
    return sign(fields, token)


def get(client, data: str | None):
    headers = {HEADER: data} if data is not None else {}
    return client.get(URL, headers=headers)


# --- HTTP level ---------------------------------------------------------------


def test_valid_init_data_is_accepted(client):
    assert get(client, init_data()).status_code == 200


def test_tampered_hash_is_rejected(client):
    data = init_data()
    head, _, digest = data.rpartition("hash=")
    flipped = digest[:-1] + ("0" if digest[-1] != "0" else "1")
    assert get(client, head + "hash=" + flipped).status_code == 401


def test_tampered_user_is_rejected(client):
    """Signed for a stranger, then the user id is edited to the admin's."""
    data = init_data(user_id=STRANGER_ID)
    forged = data.replace(quote(f'"id":{STRANGER_ID}'), quote(f'"id":{ADMIN_ID}'))
    assert forged != data
    assert get(client, forged).status_code == 401


def test_expired_auth_date_is_rejected(client):
    assert get(client, init_data(age=3601)).status_code == 401
    assert get(client, init_data(age=3500)).status_code == 200


def test_future_auth_date_is_rejected(client):
    assert get(client, init_data(age=-600)).status_code == 401


def test_non_admin_is_forbidden(client):
    res = get(client, init_data(user_id=STRANGER_ID))
    assert res.status_code == 403
    assert "price_supplier" not in res.text


def test_wrong_bot_token_is_rejected(client):
    other = "987654321:AAAnotherBotTokenAbcdefghijklmnopqrstu"
    assert get(client, init_data(token=other)).status_code == 401


@pytest.mark.parametrize(
    "data", [None, "", "garbage", "hash=abc", "user=%7B%7D&auth_date=1&hash=" + "0" * 64, "x" * 5000]
)
def test_missing_or_malformed_init_data(client, data):
    assert get(client, data).status_code == 401


def test_every_admin_route_requires_init_data(client):
    """Mutations with a stranger's valid initData are refused too."""
    stranger = {HEADER: init_data(user_id=STRANGER_ID)}
    assert client.post(URL, json={"title": "x", "category": "Одяг", "price_retail": 1}, headers=stranger).status_code == 403
    assert client.post(f"{URL}/bulk-status", json={"ids": ["a"], "status": "hidden"}, headers=stranger).status_code == 403
    assert client.delete(f"{URL}/a-1").status_code == 401


def test_cors_preflight_allows_init_data_header(client):
    from conftest import ORIGIN

    res = client.options(
        URL,
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "x-telegram-init-data,content-type",
        },
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == ORIGIN


def test_rate_limit_applies_before_auth(client):
    client.app.state.limiters["admin"].limit = 2
    codes = [get(client, "garbage").status_code for _ in range(3)]
    assert codes == [401, 401, 429]


# --- verify_init_data unit level -------------------------------------------------


def test_signature_field_is_part_of_the_check():
    data = init_data()
    assert verify_init_data(data, TEST_BOT_TOKEN) == ADMIN_ID
    with pytest.raises(InvalidInitData):
        verify_init_data(data.replace("signature=c2ln", "signature=X2ln"), TEST_BOT_TOKEN)


def test_duplicate_keys_are_rejected():
    data = init_data()
    with pytest.raises(InvalidInitData):
        verify_init_data(data + "&auth_date=1", TEST_BOT_TOKEN)


def test_uppercase_hash_is_accepted():
    head, _, digest = init_data().rpartition("hash=")
    assert verify_init_data(head + "hash=" + digest.upper(), TEST_BOT_TOKEN) == ADMIN_ID


@pytest.mark.parametrize("user", ['{"id":"111"}', '{"id":true}', '{"id":-5}', "[]", "not json"])
def test_bad_user_payload(user):
    fields = {"user": user, "auth_date": str(int(time.time()))}
    with pytest.raises(InvalidInitData):
        verify_init_data(sign(fields), TEST_BOT_TOKEN)


def test_empty_token_never_verifies():
    with pytest.raises(InvalidInitData):
        verify_init_data(init_data(token=""), "")


# --- dev bypass and startup checks ---------------------------------------------


def test_dev_bypass_without_init_data(settings):
    app = create_app(dataclasses.replace(settings, bot_token="", dev_admin_id=42))
    with TestClient(app) as c:
        assert c.get(URL).status_code == 200


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"env": "prod", "dev_admin_id": 42}, "DEV_ADMIN_ID"),
        ({"env": "prod", "bot_token": ""}, "BOT_TOKEN"),
        ({"env": "dev", "bot_token": ""}, "BOT_TOKEN"),
        ({"bot_token": "not-a-token"}, "BOT_TOKEN"),
        ({"admin_telegram_ids": frozenset()}, "ADMIN_TELEGRAM_IDS"),
        ({"allowed_origin": "*"}, "ALLOWED_ORIGIN"),
        ({"allowed_origin": "https://a.example,https://b.example"}, "ALLOWED_ORIGIN"),
        ({"allowed_origin": "https://a.example/path"}, "ALLOWED_ORIGIN"),
        ({"env": "prod", "allowed_origin": "http://shop.example"}, "ALLOWED_ORIGIN"),
        ({"env": "staging"}, "ENV"),
    ],
)
def test_invalid_config_refuses_to_start(settings, changes, message):
    with pytest.raises(ConfigError, match=message):
        create_app(dataclasses.replace(settings, **changes))


def test_valid_prod_config_starts(settings):
    create_app(dataclasses.replace(settings, env="prod"))


def test_config_error_never_contains_the_token(settings):
    secret = "123:short"
    with pytest.raises(ConfigError) as exc:
        create_app(dataclasses.replace(settings, bot_token=secret))
    assert secret not in str(exc.value)


def run_main(**env) -> subprocess.CompletedProcess:
    """Imports app.main in a fresh process with the given environment (overrides .env)."""
    full = {**os.environ, "BOT_TOKEN": "", "DEV_ADMIN_ID": "", "ENV": "dev",
            "ALLOWED_ORIGIN": "http://localhost:8000", **env}
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=BACKEND_DIR, env=full, capture_output=True, text=True, timeout=60,
    )


def test_main_exits_with_prod_and_dev_admin():
    res = run_main(ENV="prod", DEV_ADMIN_ID="42", BOT_TOKEN=TEST_BOT_TOKEN,
                   ALLOWED_ORIGIN="https://shop.example")
    assert res.returncode != 0
    assert "Config error: DEV_ADMIN_ID" in res.stderr
    assert "Traceback" not in res.stderr
    assert TEST_BOT_TOKEN not in res.stderr


def test_main_exits_with_empty_token():
    res = run_main()
    assert res.returncode != 0 and "BOT_TOKEN is empty" in res.stderr


def test_main_starts_in_dev_with_bypass():
    assert run_main(DEV_ADMIN_ID="42").returncode == 0
