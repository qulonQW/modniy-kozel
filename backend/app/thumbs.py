"""WebP thumbnails (~400 px) for product cards and lists.

A thumbnail is named by the SHA-256 of the ORIGINAL file's bytes, so the same
name always means the same picture: it can be cached forever (immutable), and
products that share a photo share one thumbnail file. Originals are only ever
read, never modified or deleted here.

Stored as product_images.thumb_path = "uploads/thumbs/<hash>-400.webp"
(served from UPLOADS_DIR/thumbs). A missing thumbnail is not an error: the
API falls back to the original image.
"""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from .config import REPO_DIR
from .db import backup_database

THUMB_WIDTH = 400
THUMB_HEIGHT = 533  # cards are 3:4; the thumbnail covers this box
THUMB_SUBDIR = "thumbs"
THUMB_PREFIX = "uploads/thumbs/"
WEBP_QUALITY = 72
# Refuse absurd pixel counts (decompression bombs) instead of warning.
Image.MAX_IMAGE_PIXELS = 50_000_000


class ThumbnailError(Exception):
    """The image could not be decoded."""


def render_thumbnail(data: bytes) -> bytes:
    """Original bytes -> WebP bytes covering THUMB_WIDTH x THUMB_HEIGHT
    (aspect kept, never upscaled)."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.draft("RGB", (THUMB_WIDTH, THUMB_WIDTH))  # fast JPEG decode at reduced size
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            scale = min(1.0, max(THUMB_WIDTH / im.width, THUMB_HEIGHT / im.height))
            size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
            if size != im.size:
                im = im.resize(size, Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, "WEBP", quality=WEBP_QUALITY, method=4)
    except (OSError, ValueError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ThumbnailError(type(exc).__name__) from None
    return out.getvalue()


def thumb_name(original: bytes) -> str:
    return f"{hashlib.sha256(original).hexdigest()[:32]}-{THUMB_WIDTH}.webp"


def ensure_thumbnail(uploads_dir: Path, original: bytes) -> str:
    """Creates the thumbnail file if missing; returns its stored path."""
    name = thumb_name(original)
    target_dir = uploads_dir / THUMB_SUBDIR
    target = target_dir / name
    if not target.is_file():
        webp = render_thumbnail(original)
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(webp)
            os.replace(tmp, target)  # atomic: never a half-written thumbnail
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return THUMB_PREFIX + name


def original_file(stored_path: str, uploads_dir: Path) -> Path | None:
    """Filesystem location of a stored image path, or None if it points
    anywhere unexpected."""
    roots = {
        "uploads/products/": uploads_dir / "products",
        "public/images/products/": REPO_DIR / "public" / "images" / "products",
        "images/products/": REPO_DIR / "images" / "products",
    }
    for prefix, base in roots.items():
        if stored_path.startswith(prefix):
            base = base.resolve()
            target = (base / stored_path[len(prefix):]).resolve()
            return target if target.parent == base else None
    return None


def thumb_file(stored_thumb: str, uploads_dir: Path) -> Path | None:
    if not stored_thumb.startswith(THUMB_PREFIX):
        return None
    base = (uploads_dir / THUMB_SUBDIR).resolve()
    target = (base / stored_thumb[len(THUMB_PREFIX):]).resolve()
    return target if target.parent == base else None


def delete_thumb_if_unused(conn: sqlite3.Connection, uploads_dir: Path, stored_thumb: str | None) -> None:
    """Thumbnails are shared by identical photos: delete only the last user."""
    if not stored_thumb:
        return
    used = conn.execute("SELECT 1 FROM product_images WHERE thumb_path = ?", (stored_thumb,)).fetchone()
    path = thumb_file(stored_thumb, uploads_dir)
    if used is None and path is not None:
        path.unlink(missing_ok=True)


def backfill(conn: sqlite3.Connection, uploads_dir: Path, progress=None) -> dict[str, int]:
    """Idempotent: creates thumbnails for images that have none (or whose
    file is gone) and records them. Backs the database up first if anything
    is going to change. Originals are only read."""
    rows = conn.execute("SELECT id, path, thumb_path FROM product_images ORDER BY id").fetchall()
    todo, missing = [], 0
    for r in rows:
        existing = thumb_file(r["thumb_path"], uploads_dir) if r["thumb_path"] else None
        if existing is not None and existing.is_file():
            continue
        source = original_file(r["path"], uploads_dir)
        if source is None or not source.is_file():
            missing += 1  # nothing to make a thumbnail from; the API uses the original URL
            continue
        todo.append((r, source))
    stats = {
        "images": len(rows), "already_done": len(rows) - len(todo) - missing,
        "created": 0, "missing": missing, "failed": 0,
    }
    if not todo:
        return stats
    stats["backup"] = str(backup_database(conn) or "")
    for i, (r, source) in enumerate(todo, 1):
        try:
            stored = ensure_thumbnail(uploads_dir, source.read_bytes())
        except ThumbnailError:
            stats["failed"] += 1
            continue
        conn.execute("UPDATE product_images SET thumb_path = ? WHERE id = ?", (stored, r["id"]))
        stats["created"] += 1
        if i % 200 == 0:
            conn.commit()
            if progress:
                progress(i, len(todo))
    conn.commit()
    return stats
