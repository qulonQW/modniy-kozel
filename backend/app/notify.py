"""New-order notifications to admins via the Bot API (sendMessage).

The order is committed first; sending happens afterwards (right after the
response, then from a background loop), so a Telegram failure never loses an
order. Each admin has a row in order_notifications: one admin who blocked the
bot or never pressed Start does not block the others. Up to MAX_ATTEMPTS per
admin with BACKOFF_SECONDS between them; the admin panel can reset it.

Never logged: the bot token (it is part of the API URL), phone numbers,
message text, initData. Errors are stored as short codes (http_403, network).
"""

from __future__ import annotations

import asyncio
import html
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from .db import connect, utc_now

log = logging.getLogger("tovarka.notify")
# httpx logs every request URL at INFO; the URL contains the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MAX_ATTEMPTS = 5
# Wait before attempt n+1 after n failed attempts.
BACKOFF_SECONDS = (0, 30, 120, 600, 1800)
CLAIM_SECONDS = 120  # a claimed row is not picked again while being sent
LOOP_INTERVAL_SECONDS = 30


class Notifier(Protocol):
    def send(self, chat_id: int, text: str) -> str | None:
        """Returns None on success, else a short error code."""


class BotNotifier:
    def __init__(self, token: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"

    def __repr__(self) -> str:  # never show the token
        return "BotNotifier()"

    def send(self, chat_id: int, text: str) -> str | None:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            res = httpx.post(self._url, json=payload, timeout=10.0)
        except httpx.HTTPError:
            return "network"
        if res.status_code == 200:
            try:
                return None if res.json().get("ok") else "not_ok"
            except ValueError:
                return "bad_json"
        return f"http_{res.status_code}"


def _money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " грн"


# Telegram rejects messages over 4096 characters (after entity parsing; we
# count the raw HTML, which is longer, so this is on the safe side).
MAX_MESSAGE_CHARS = 4096
# Progressively shorter limits for user text: (title, comment, other fields).
_SHORTENING = ((None, None, None), (60, 300, 80), (30, 120, 40), (16, 60, 24))


def _cut(value, limit: int | None) -> str:
    text = str(value)
    return text if limit is None or len(text) <= limit else text[: limit - 1] + "…"


def order_message(conn: sqlite3.Connection, order_id: int) -> str:
    """HTML text for Telegram. Every user-supplied value goes through
    html.escape; only our own tags are left unescaped. Raw values are
    shortened BEFORE escaping when the message would be too long, so the
    markup is never cut in the middle."""
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    items = conn.execute(
        "SELECT title_snapshot, size, qty, price_snapshot FROM order_items WHERE order_id = ? ORDER BY id",
        (order_id,),
    ).fetchall()
    for limits in _SHORTENING:
        text = _render(o, items, *limits)
        if len(text) <= MAX_MESSAGE_CHARS:
            return text
    return text[:MAX_MESSAGE_CHARS]  # unreachable with the schema's length limits


def _render(o, items, title_limit, comment_limit, field_limit) -> str:
    e = lambda value, limit=field_limit: html.escape(_cut(value, limit), quote=True)  # noqa: E731

    buyer = f'<a href="tg://user?id={int(o["user_telegram_id"])}">{e(o["first_name"] or "Покупець")}</a>'
    if o["username"]:
        buyer += f" @{e(o['username'])}"
    lines = [
        f"🛒 <b>Нове замовлення № {int(o['id'])}</b>",
        "",
        f"<b>Покупець:</b> {buyer}",
        f"<b>Ім'я:</b> {e(o['customer_name'])}",
        f"<b>Телефон:</b> {e(o['phone'])}",
        f"<b>Місто:</b> {e(o['city'])}",
        f"<b>Відділення / поштомат:</b> {e(o['delivery_point'])}",
    ]
    if o["comment"]:
        lines.append(f"<b>Коментар:</b> {e(o['comment'], comment_limit)}")
    lines += ["", "<b>Товари:</b>"]
    for it in items:
        size = f", розмір {e(it['size'])}" if it["size"] else ""
        lines.append(
            f"• {e(it['title_snapshot'], title_limit)}{size} × {int(it['qty'])} — "
            f"{_money(it['qty'] * it['price_snapshot'])}"
        )
    lines += ["", f"<b>Разом: {_money(o['total'])}</b>"]
    return "\n".join(lines)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def process_due(conn: sqlite3.Connection, notifier: Notifier, order_id: int | None = None) -> int:
    """Sends every due notification (optionally of one order). Returns how
    many were sent successfully. Safe to run concurrently: a row is claimed
    with a conditional UPDATE before it is sent."""
    now = datetime.now(timezone.utc)
    where = "status = 'pending' AND attempts < ? AND next_attempt_at <= ?"
    params: list = [MAX_ATTEMPTS, _iso(now)]
    if order_id is not None:
        where += " AND order_id = ?"
        params.append(order_id)
    due = conn.execute(
        f"SELECT order_id, admin_id, attempts, next_attempt_at FROM order_notifications "
        f"WHERE {where} ORDER BY order_id, admin_id",
        params,
    ).fetchall()

    sent = 0
    for row in due:
        with conn:
            claimed = conn.execute(
                "UPDATE order_notifications SET next_attempt_at = ? "
                "WHERE order_id = ? AND admin_id = ? AND status = 'pending' AND next_attempt_at = ?",
                (_iso(now + timedelta(seconds=CLAIM_SECONDS)), row["order_id"], row["admin_id"],
                 row["next_attempt_at"]),
            ).rowcount
        if not claimed:
            continue  # another worker took it

        try:
            error = notifier.send(row["admin_id"], order_message(conn, row["order_id"]))
        except Exception as exc:  # a bug in a notifier must not kill the loop
            error = f"error_{type(exc).__name__}"[:40]
        attempts = row["attempts"] + 1
        with conn:
            if error is None:
                sent += 1
                conn.execute(
                    "UPDATE order_notifications SET status = 'sent', attempts = ?, last_error = NULL, "
                    "sent_at = ? WHERE order_id = ? AND admin_id = ?",
                    (attempts, utc_now(), row["order_id"], row["admin_id"]),
                )
            else:
                done = attempts >= MAX_ATTEMPTS
                wait = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]
                conn.execute(
                    "UPDATE order_notifications SET status = ?, attempts = ?, last_error = ?, "
                    "next_attempt_at = ? WHERE order_id = ? AND admin_id = ?",
                    ("failed" if done else "pending", attempts, error,
                     _iso(datetime.now(timezone.utc) + timedelta(seconds=wait)),
                     row["order_id"], row["admin_id"]),
                )
                log.warning(
                    "order notification failed: order=%s attempt=%s error=%s",
                    row["order_id"], attempts, error,
                )
            conn.execute(
                "UPDATE orders SET notify_attempts = notify_attempts + 1, "
                "notified = NOT EXISTS (SELECT 1 FROM order_notifications n "
                "WHERE n.order_id = orders.id AND n.status != 'sent') WHERE id = ?",
                (row["order_id"],),
            )
    return sent


def reset_for_resend(conn: sqlite3.Connection, order_id: int) -> int:
    """Admin "resend": admins who have not received the order get it again
    with a fresh attempt budget; if everyone already has it, all get it
    again. Returns the number of rows queued."""
    now = utc_now()
    queued = conn.execute(
        "UPDATE order_notifications SET status = 'pending', attempts = 0, next_attempt_at = ? "
        "WHERE order_id = ? AND status != 'sent'",
        (now, order_id),
    ).rowcount
    if not queued:
        queued = conn.execute(
            "UPDATE order_notifications SET status = 'pending', attempts = 0, next_attempt_at = ?, "
            "sent_at = NULL WHERE order_id = ?",
            (now, order_id),
        ).rowcount
    conn.execute("UPDATE orders SET notified = 0 WHERE id = ? AND ? > 0", (order_id, queued))
    return queued


def run_due(app, order_id: int | None = None) -> None:
    """Entry point for background tasks: own connection, app's notifier."""
    notifier = getattr(app.state, "notifier", None)
    if notifier is None:
        return  # no BOT_TOKEN (dev): notifications stay pending
    conn = connect(app.state.settings.database_path, check_same_thread=False)
    try:
        process_due(conn, notifier, order_id)
    finally:
        conn.close()


async def retry_loop(app) -> None:
    """Lightweight retry loop started in the app lifespan."""
    from starlette.concurrency import run_in_threadpool

    while True:
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)
        try:
            await run_in_threadpool(run_due, app)
        except Exception as exc:  # keep looping; never log message contents
            log.warning("notification loop error: %s", type(exc).__name__)
