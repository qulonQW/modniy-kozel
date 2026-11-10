from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request

from .config import Settings
from .db import connect


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection per request. FastAPI may run the dependency and the
    endpoint on different worker threads, hence check_same_thread=False;
    the connection is never shared between requests."""
    conn = connect(request.app.state.settings.database_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()
