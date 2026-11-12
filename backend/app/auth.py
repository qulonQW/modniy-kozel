"""Admin authentication via Telegram Mini App initData.

The admin page sends Telegram.WebApp.initData unchanged in the
X-Telegram-Init-Data header on every request. Verification follows
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    data_check_string = "\\n".join(f"{k}={v}" for every field except hash, sorted by key)
    secret_key        = HMAC_SHA256(key="WebAppData", msg=bot_token)
    valid             = hex(HMAC_SHA256(key=secret_key, msg=data_check_string)) == hash

"signature" (Ed25519, for third parties) is a normal field here and stays in
the data-check-string. initData and the token are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request

from .config import Settings

HEADER = "X-Telegram-Init-Data"
MAX_AGE_SECONDS = 3600
MAX_CLOCK_SKEW_SECONDS = 60
MAX_INIT_DATA_LENGTH = 4096


class InvalidInitData(Exception):
    """initData is malformed, forged or expired. The message is for tests and
    is never sent to the client or logged."""


def verify_init_data(init_data: str, bot_token: str, now: float | None = None) -> int:
    """Returns the Telegram user ID from valid initData, else raises InvalidInitData."""
    if not bot_token:
        raise InvalidInitData("bot token not configured")
    if not init_data or len(init_data) > MAX_INIT_DATA_LENGTH:
        raise InvalidInitData("missing or too long")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise InvalidInitData("not a query string") from None
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise InvalidInitData("duplicate keys")

    received_hash = fields.pop("hash", "")
    if len(received_hash) != 64:
        raise InvalidInitData("bad hash")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash.lower()):
        raise InvalidInitData("hash mismatch")

    # Only trust the contents after the signature has been checked.
    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError):
        raise InvalidInitData("bad auth_date") from None
    now = time.time() if now is None else now
    if now - auth_date > MAX_AGE_SECONDS:
        raise InvalidInitData("expired")
    if auth_date - now > MAX_CLOCK_SKEW_SECONDS:
        raise InvalidInitData("auth_date in the future")

    try:
        user = json.loads(fields["user"])
        user_id = user["id"]
    except (KeyError, ValueError, TypeError):
        raise InvalidInitData("no user") from None
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise InvalidInitData("bad user id")
    return user_id


def is_admin(request: Request) -> bool:
    """Same rules as require_admin, but answers yes/no instead of raising.
    Used by GET /api/me so the storefront can show the admin link."""
    try:
        require_admin(request)
    except HTTPException:
        return False
    return True


def require_admin(request: Request) -> int:
    """FastAPI dependency: returns the admin's Telegram user ID.

    401 for missing/invalid/expired initData, 403 for a valid Telegram user
    who is not in ADMIN_TELEGRAM_IDS.
    """
    settings: Settings = request.app.state.settings
    if settings.env == "dev" and settings.dev_admin_id is not None:
        # Dev bypass. create_app refuses to start with it outside ENV=dev.
        return settings.dev_admin_id

    try:
        user_id = verify_init_data(request.headers.get(HEADER, ""), settings.bot_token)
    except InvalidInitData:
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    if user_id not in settings.admin_telegram_ids:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user_id
