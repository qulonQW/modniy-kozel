"""Uploaded image validation and storage.

The type is detected from magic bytes only; the client's filename and
Content-Type are ignored. Files get random names and live under
UPLOADS_DIR/products, which is served as static files (never executed).
"""

from __future__ import annotations

import secrets
from pathlib import Path

MAX_IMAGE_BYTES = 5 * 1024 * 1024
UPLOAD_SUBDIR = "products"
STORED_PREFIX = "uploads/products/"


def detect_image_type(data: bytes) -> str | None:
    """Returns the file extension for JPEG/PNG/WebP data, else None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save_image(uploads_dir: Path, data: bytes, ext: str) -> str:
    """Writes the file under a random name; returns the stored relative path."""
    target_dir = uploads_dir / UPLOAD_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}.{ext}"
    with open(target_dir / name, "xb") as fh:  # "x": never overwrite
        fh.write(data)
    return STORED_PREFIX + name


def delete_stored_image(uploads_dir: Path, stored_path: str) -> None:
    """Deletes an uploaded file. Paths outside uploads (demo or imported
    images) are left alone, as is anything that resolves outside the dir."""
    if not stored_path.startswith(STORED_PREFIX):
        return
    base = (uploads_dir / UPLOAD_SUBDIR).resolve()
    target = (base / stored_path[len(STORED_PREFIX):]).resolve()
    if target.parent != base:
        return
    target.unlink(missing_ok=True)
