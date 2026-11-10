"""Entry point: uvicorn app.main:app (run from backend/).

A bad configuration stops the process with a one-line message (no traceback,
no secret values) instead of starting a server."""

import sys

from .application import create_app
from .config import ConfigError, load_settings

try:
    app = create_app(load_settings())
except ConfigError as exc:
    sys.exit(f"Config error: {exc}")
