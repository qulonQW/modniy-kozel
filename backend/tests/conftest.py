from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.application import create_app
from app.config import Settings
from app.db import connect, init_db, search_key

ORIGIN = "https://shop.example"
TEST_BOT_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmn"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="dev",
        bot_token=TEST_BOT_TOKEN,
        admin_telegram_ids=frozenset({111, 222}),
        allowed_origin=ORIGIN,
        database_path=tmp_path / "app.sqlite3",
        uploads_dir=tmp_path / "uploads",
    )


class FakeNotifier:
    """Stands in for the Bot API: records messages, fails for chosen chats."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.calls = 0
        self.fail_for: dict[int, str] = {}  # chat_id -> error code

    def send(self, chat_id: int, text: str) -> str | None:
        self.calls += 1
        if chat_id in self.fail_for:
            return self.fail_for[chat_id]
        self.sent.append((chat_id, text))
        return None


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def client(settings: Settings, notifier: FakeNotifier):
    app = create_app(settings)
    app.state.notifier = notifier  # never call the real Telegram API in tests
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    """The same client with auth replaced by a fixed admin ID (111)."""
    from app.auth import require_admin

    client.app.dependency_overrides[require_admin] = lambda: 111
    yield client
    client.app.dependency_overrides.clear()


@pytest.fixture
def db(settings: Settings, client):
    """Direct connection to the app's database (tables already created)."""
    c = connect(settings.database_path)
    yield c
    c.close()


def insert_product(conn, pid: str, status: str = "published", **fields) -> None:
    row = {
        "title": f"Товар {pid}",
        "description": "Опис",
        "category": "Одяг",
        "price_retail": 600,
        "price_supplier": 271828,
        "sizes": '["S", "M"]',
        "in_stock": 1,
        "popular": 0,
        "edited_by_admin": 0,
        "source_channel": "secret-channel",
        "source_message_id": 31337,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        **fields,
    }
    row.setdefault("search_text", search_key(row["title"]))
    cols = ", ".join(["id", "status", *row])
    marks = ", ".join("?" * (len(row) + 2))
    conn.execute(f"INSERT INTO products ({cols}) VALUES ({marks})", (pid, status, *row.values()))
    conn.commit()


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "test.sqlite3")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def write_json(tmp_path: Path):
    def _write(name: str, data) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    return _write
