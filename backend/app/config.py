"""Settings loaded from backend/.env plus the process environment.

Real environment variables win over .env, so tests and hosting platforms can
override values without editing the file. Values are never logged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
ENV_FILE = BACKEND_DIR / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser: blank lines and # comments are skipped,
    surrounding single/double quotes are stripped. No interpolation."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


class ConfigError(Exception):
    """Invalid configuration. Messages name the variable, never its value."""


def _parse_int(name: str, raw: str) -> int:
    if not raw.isdigit() or int(raw) <= 0:
        raise ConfigError(f"{name} must be a positive integer Telegram user ID")
    return int(raw)


def _parse_ids(raw: str) -> frozenset[int]:
    return frozenset(
        _parse_int("ADMIN_TELEGRAM_IDS", part.strip()) for part in raw.split(",") if part.strip()
    )


BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def validate_settings(s: "Settings") -> None:
    """Startup checks; create_app calls this so a bad config never serves."""
    if s.env not in ("dev", "prod"):
        raise ConfigError("ENV must be 'dev' or 'prod'")
    dev_bypass = s.dev_admin_id is not None
    if dev_bypass and s.env != "dev":
        raise ConfigError("DEV_ADMIN_ID is set but ENV is not 'dev'; remove it in production")
    if not s.bot_token and not dev_bypass:
        raise ConfigError("BOT_TOKEN is empty (allowed only with ENV=dev and DEV_ADMIN_ID)")
    if s.bot_token and not BOT_TOKEN_RE.match(s.bot_token):
        raise ConfigError("BOT_TOKEN does not look like a token from @BotFather")
    if not s.admin_telegram_ids and not dev_bypass:
        raise ConfigError("ADMIN_TELEGRAM_IDS is empty")
    origin = s.allowed_origin
    if "*" in origin or "," in origin or (origin and not re.fullmatch(r"https?://[^/\s]+", origin)):
        raise ConfigError("ALLOWED_ORIGIN must be one origin like https://shop.example (no path, no wildcard)")
    if s.env == "prod" and not origin.startswith("https://"):
        raise ConfigError("ALLOWED_ORIGIN must be an https:// origin in production")


@dataclass(frozen=True)
class Settings:
    env: str = "dev"
    bot_token: str = field(default="", repr=False)
    admin_telegram_ids: frozenset[int] = frozenset()
    dev_admin_id: int | None = None
    allowed_origin: str = ""
    database_path: Path = BACKEND_DIR / "data" / "shop.sqlite3"
    uploads_dir: Path = BACKEND_DIR / "uploads"
    brand: str = "modniy-kozel"  # folder in brands/, see app/brand.py


def load_settings(env_file: Path = ENV_FILE) -> Settings:
    merged = {**read_env_file(env_file), **os.environ}

    def get(key: str, default: str = "") -> str:
        return merged.get(key, default).strip()

    dev_admin = get("DEV_ADMIN_ID")
    db_path = get("DATABASE_PATH")
    uploads = get("UPLOADS_DIR")
    return Settings(
        env=get("ENV", "dev").lower(),
        bot_token=get("BOT_TOKEN"),
        admin_telegram_ids=_parse_ids(get("ADMIN_TELEGRAM_IDS")),
        dev_admin_id=_parse_int("DEV_ADMIN_ID", dev_admin) if dev_admin else None,
        allowed_origin=get("ALLOWED_ORIGIN"),
        database_path=Path(db_path) if db_path else Settings.database_path,
        uploads_dir=Path(uploads) if uploads else Settings.uploads_dir,
        brand=get("BRAND") or Settings.brand,
    )
