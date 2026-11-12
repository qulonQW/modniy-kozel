"""Serves the storefront files with versioned asset URLs.

Every HTML page is sent with its local JS/CSS references rewritten to
"./app.js?v=<content hash>". A request that carries the current hash gets a
one-year immutable Cache-Control, so moving between pages inside Telegram
does not download or even re-check app.js/style.css again; any edit to a
file changes its hash, so updates still reach the phone on the next page
load. HTML itself stays "no-cache" (with an ETag, so an unchanged page is a
tiny 304). Requests without (or with an old) ?v= get "no-cache".

Files are re-read only when their mtime/size changes.
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response

ASSETS = ("app.js", "cart.js", "products.js", "texts.js", "style.css")
_ASSET_REF = re.compile(r'(src|href)="\./(' + "|".join(re.escape(a) for a in ASSETS) + r')"')
IMMUTABLE = "public, max-age=31536000, immutable"
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class Storefront:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, tuple[tuple[int, int], bytes, str]] = {}
        self._lock = threading.Lock()

    def _read(self, name: str) -> tuple[bytes, str]:
        """(content, short sha256), re-read only when the file changed."""
        path = self.root / name
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

    def render_html(self, name: str) -> bytes:
        text = self._read(name)[0].decode("utf-8")
        return _ASSET_REF.sub(
            lambda m: f'{m[1]}="./{m[2]}?v={self.version(m[2])}"', text
        ).encode("utf-8")

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
