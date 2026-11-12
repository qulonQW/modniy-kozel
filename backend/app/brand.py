"""The shop's brand: everything that differs between two shops built on this
code lives in ONE folder, brands/<slug>/, chosen by BRAND in backend/.env.

    brands/<slug>/
      brand.json   name, short name, hero lines, tagline, language, font URL
      theme.css    colours, fonts, radii (CSS variables only)
      texts.js     shopper texts for cart / checkout / orders
      logo.png     square logo (header + browser icon)

The storefront HTML refers to these as ./brand/<file> and to the values of
brand.json as {{brand.<key>}}; app/storefront.py fills them in. Nothing
outside brands/ may name a shop (tests/test_brand.py checks this), so a new
shop is "copy a brand folder, edit it, set BRAND" without touching the core.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import REPO_DIR, ConfigError

BRANDS_DIR = REPO_DIR / "brands"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

# Files every brand folder must have; these are the only ones served
# (at /brand/<file>). brand.json is read by the server and never served.
SERVED_FILES = ("theme.css", "texts.js", "logo.png")
REQUIRED_KEYS = ("name", "short_name", "hero_lines", "tagline", "lang", "fonts_url")
FONTS_PREFIX = "https://fonts.googleapis.com/css2?"


@dataclass(frozen=True)
class Brand:
    slug: str
    directory: Path
    name: str
    short_name: str
    hero_lines: tuple[str, ...]
    tagline: str
    lang: str
    fonts_url: str

    def files(self) -> dict[str, Path]:
        """Public URL path -> file, for the storefront allowlist."""
        return {f"brand/{name}": self.directory / name for name in SERVED_FILES}

    def html_values(self) -> dict[str, str]:
        """{{brand.<key>}} -> text that is already safe inside HTML."""
        esc = lambda value: html.escape(value, quote=True)  # noqa: E731
        return {
            "name": esc(self.name),
            "short_name": esc(self.short_name),
            "hero": "<br>".join(esc(line) for line in self.hero_lines),
            "tagline": esc(self.tagline),
            "lang": esc(self.lang),
            "fonts_url": esc(self.fonts_url),
        }


def _text(data: dict, key: str, max_len: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ConfigError(f"brand.json: '{key}' must be a non-empty string up to {max_len} characters")
    return value.strip()


def load_brand(slug: str, brands_dir: Path | None = None) -> Brand:
    if not SLUG_RE.match(slug):
        raise ConfigError("BRAND must be a folder name like 'modniy-kozel' (a-z, 0-9, -)")
    directory = (brands_dir or BRANDS_DIR) / slug
    if not (directory / "brand.json").is_file():
        raise ConfigError(f"BRAND: brands/{slug}/brand.json not found")
    for name in SERVED_FILES:
        if not (directory / name).is_file():
            raise ConfigError(f"brand '{slug}' has no {name}")
    try:
        data = json.loads((directory / "brand.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"brands/{slug}/brand.json is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError("brand.json must be a JSON object")
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ConfigError(f"brand.json is missing: {', '.join(missing)}")

    lines = data["hero_lines"]
    if not isinstance(lines, list) or not 1 <= len(lines) <= 3:
        raise ConfigError("brand.json: 'hero_lines' must be a list of 1 to 3 strings")
    hero = tuple(_text({"line": line}, "line", 40) for line in lines)

    lang = _text(data, "lang", 10)
    if not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", lang):
        raise ConfigError("brand.json: 'lang' must look like 'uk' or 'en-US'")
    fonts_url = _text(data, "fonts_url", 500)
    if not fonts_url.startswith(FONTS_PREFIX):
        # The admin page's Content-Security-Policy allows only Google Fonts.
        raise ConfigError(f"brand.json: 'fonts_url' must start with {FONTS_PREFIX}")

    return Brand(
        slug=slug,
        directory=directory,
        name=_text(data, "name", 60),
        short_name=_text(data, "short_name", 4),
        hero_lines=hero,
        tagline=_text(data, "tagline", 120),
        lang=lang,
        fonts_url=fonts_url,
    )
