"""Serves an explicit allowlist of page files with versioned asset URLs and
the brand's values filled in.

Every HTML page is sent with:
  * its local asset references rewritten to "./app.js?v=<content hash>"
    (also "./brand/theme.css", "./brand/logo.png", ...). A request that
    carries the current hash gets a one-year immutable Cache-Control, so
    moving between pages inside Telegram does not download or even re-check
    the JS/CSS again; any edit to a file changes its hash, so updates still
    reach the phone on the next page load;
  * every {{brand.<key>}} replaced with the active brand's HTML-escaped value
    (app/brand.py). An unknown key is an error, never shown to shoppers.
HTML itself stays "no-cache" (with an ETag, so an unchanged page is a tiny
304). Requests without (or with an old) ?v= get "no-cache".

Files are re-read only when their mtime/size changes.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response

IMMUTABLE = "public, max-age=31536000, immutable"
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
}
PLACEHOLDER = re.compile(r"\{\{brand\.([a-z_]+)\}\}")


class Storefront:
    def __init__(self, files: Mapping[str, Path], values: Mapping[str, str]) -> None:
        """files: URL path ("app.js", "brand/theme.css") -> file on disk.
        values: {{brand.<key>}} -> HTML-safe text."""
        self.files = dict(files)
        self.values = dict(values)
        assets = sorted((n for n in self.files if not n.endswith(".html")), key=len, reverse=True)
        self._asset_ref = re.compile(
            r'(src|href)="\./(' + "|".join(re.escape(a) for a in assets) + r')"'
        ) if assets else None
        self._cache: dict[str, tuple[tuple[int, int], bytes, str]] = {}
        self._lock = threading.Lock()

    def __contains__(self, name: str) -> bool:
        return name in self.files

    def _read(self, name: str) -> tuple[bytes, str]:
        """(content, short sha256), re-read only when the file changed."""
        path = self.files[name]
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(name)
        if hit and hit[0] == key:
            return hit[1], hit[2]
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:12]
        with self._lock:
            self._cache[name] = (key, data, digest)
        return data, digest

    def version(self, name: str) -> str:
        return self._read(name)[1]

    def _fill(self, match: re.Match) -> str:
        key = match[1]
        if key not in self.values:
            raise KeyError(f"unknown placeholder {{{{brand.{key}}}}}")
        return self.values[key]

    def render_html(self, name: str) -> bytes:
        text = PLACEHOLDER.sub(self._fill, self._read(name)[0].decode("utf-8"))
        if self._asset_ref:
            text = self._asset_ref.sub(lambda m: f'{m[1]}="./{m[2]}?v={self.version(m[2])}"', text)
        return text.encode("utf-8")

    def response(self, request: Request, name: str) -> Response:
        if name.endswith(".html"):
            body = self.render_html(name)
            etag = f'"{hashlib.sha256(body).hexdigest()[:16]}"'
            cache = "no-cache"
        else:
            body, digest = self._read(name)
            etag = f'"{digest}"'
            cache = IMMUTABLE if request.query_params.get("v") == digest else "no-cache"
        headers = {"Cache-Control": cache, "ETag": etag}
        if etag in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type=MEDIA_TYPES[Path(name).suffix], headers=headers)
