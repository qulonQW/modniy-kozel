"""In-memory sliding-window rate limiter.

Good enough for a single uvicorn process (the planned deployment). With
several workers each one keeps its own counters. Keyed by client IP; behind a
reverse proxy run uvicorn with --proxy-headers so the real IP is used.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

LIMITS = {
    "admin": (120, 60.0),  # requests per window (seconds)
    "upload": (20, 60.0),
    "catalog": (240, 60.0),  # public search: typing + "show more"
    "favorites": (120, 60.0),
}


class RateLimiter:
    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # drop idle keys so memory stays bounded
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True


def build_limiters() -> dict[str, RateLimiter]:
    return {name: RateLimiter(*cfg) for name, cfg in LIMITS.items()}


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def limit(name: str):
    def dependency(request: Request) -> None:
        limiter: RateLimiter = request.app.state.limiters[name]
        if not limiter.hit(_client_key(request)):
            raise HTTPException(status_code=429, detail="Too many requests")

    return dependency
