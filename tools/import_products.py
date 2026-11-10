#!/usr/bin/env python3
"""Convert a Telegram Desktop HTML export of a supplier channel into the
files that `python -m app.manage import-json` expects.

    python tools/import_products.py --supplier casual_gladky_shoes --dry-run
    python tools/import_products.py --supplier casual_gladky_shoes
    cd backend && python -m app.manage import-json \
        --public ../import/products.casual_gladky_shoes.public.json \
        --costs ../import/private/costs.casual_gladky_shoes.json

Each supplier has an entry in tools/import_config.json (channel key = entry
name, export folder, markup and optional template rules). The channel key is
part of every product id, so it must never change after an import.

Outputs per supplier (not written with --dry-run, except the report):
  import/products.<key>.public.json   products (no supplier prices)
  import/private/costs.<key>.json     {"<id>": {"price_supplier": .., "price_reference": ..}}
  backend/uploads/products/*          photo copies, name = sha256(id + bytes)[:32].<ext>
  import/import_report.<key>.md       counts, message ids and reason codes only

Duplicates: every product photo is hashed (sha256) and compared with the
other products of this import and with the products already in the DB (opened
read-only). Matches are only flagged (possible_duplicate + duplicate_group),
never deleted or merged.

Stdlib only, no network access. All channel text is untrusted: it is only
ever matched with regexes, never evaluated, and nothing from it is logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULTS = {
    "config": REPO / "tools" / "import_config.json",
    "uploads": REPO / "backend" / "uploads",
    "db": REPO / "backend" / "data" / "shop.sqlite3",
}


def default_outputs(channel: str) -> dict[str, Path]:
    return {
        "out_public": REPO / "import" / f"products.{channel}.public.json",
        "out_costs": REPO / "import" / "private" / f"costs.{channel}.json",
        "report": REPO / "import" / f"import_report.{channel}.md",
    }


TITLE_MAX = 100
DESCRIPTION_MAX = 1000
MAX_IMAGES = 10
MAX_SIZES = 20
UNTITLED = "Без назви"

# --------------------------------------------------------------------------
# HTML export parsing
# --------------------------------------------------------------------------


@dataclass
class Block:
    """One <div class="message ..."> of the export."""

    msg_id: int
    service: bool
    joined: bool
    date: str | None = None
    text: str = ""
    photos: list[str] = field(default_factory=list)
    video: bool = False


class ExportParser(HTMLParser):
    VOID = {"br", "img", "hr", "meta", "link", "input", "source", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._stack: list[tuple[str, str | None]] = []
        self._cur: Block | None = None
        self._in_text = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.VOID:
            if tag == "br" and self._in_text and self._cur is not None:
                self._cur.text += "\n"
            return
        classes = set((a.get("class") or "").split())
        role = None
        cur = self._cur
        if tag == "div" and "message" in classes:
            match = re.fullmatch(r"message(-?\d+)", a.get("id") or "")
            self._cur = Block(
                msg_id=int(match.group(1)) if match else 0,
                service="service" in classes,
                joined="joined" in classes,
            )
            self.blocks.append(self._cur)
            role = "message"
        elif cur is not None:
            if tag == "div" and {"date", "details"} <= classes:
                cur.date = a.get("title")
            elif tag == "div" and "text" in classes and not self._in_text:
                self._in_text = True
                role = "text"
            elif tag == "a" and "photo_wrap" in classes:
                cur.photos.append(a.get("href") or "")
            elif classes & {"media_video", "video_file_wrap"}:
                cur.video = True
        self._stack.append((tag, role))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        while self._stack:
            open_tag, role = self._stack.pop()
            if role == "text":
                self._in_text = False
            elif role == "message":
                self._cur = None
            if open_tag == tag:
                break

    def handle_data(self, data):
        if self._in_text and self._cur is not None:
            self._cur.text += data


def export_files(input_dir: Path) -> list[Path]:
    def page_no(p: Path) -> int:
        digits = re.sub(r"\D", "", p.stem)
        return int(digits) if digits else 1

    return sorted(input_dir.glob("messages*.html"), key=page_no)


def parse_export(input_dir: Path) -> list[Block]:
    parser = ExportParser()
    for path in export_files(input_dir):
        parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    for b in parser.blocks:
        b.text = "\n".join(line.strip() for line in b.text.splitlines()).strip()
    return parser.blocks


def group_albums(blocks: list[Block]) -> list[list[Block]]:
    """HTML exports have no media_group_id. An album shows up as consecutive
    "joined" photo blocks with the same timestamp (to the second). A second
    caption always starts a new group."""
    groups: list[list[Block]] = []
    cur: list[Block] = []
    for b in blocks:
        if b.service:
            if cur:
                groups.append(cur)
            cur = []
            continue
        continues = (
            cur
            and b.joined
            and b.photos
            and cur[-1].photos
            and b.date is not None
            and b.date == cur[-1].date
            and not (b.text and any(x.text for x in cur))
        )
        if continues:
            cur.append(b)
        else:
            if cur:
                groups.append(cur)
            cur = [b]
    if cur:
        groups.append(cur)
    return groups


def _timestamp(date: str | None) -> datetime | None:
    try:
        return datetime.strptime((date or "")[:19], "%d.%m.%Y %H:%M:%S")
    except ValueError:
        return None


def merge_split_albums(groups: list[list[Block]], max_gap_seconds: int) -> list[list[Block]]:
    """Some exports split one album when its photos straddle a second boundary:
    the tail becomes a caption-less group of "joined" photo blocks a second
    later. Re-attach such a tail to the captioned album right before it, if the
    gap is within max_gap_seconds and the album stays within MAX_IMAGES."""
    if max_gap_seconds <= 0:
        return groups
    merged: list[list[Block]] = []
    for g in groups:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and not any(b.text for b in g)
            and all(b.joined and b.photos and not b.service for b in g)
            and any(b.text for b in prev)
            and all(b.photos for b in prev)
            and sum(len(b.photos) for b in prev + g) <= MAX_IMAGES
        ):
            start, end = _timestamp(prev[-1].date), _timestamp(g[0].date)
            if start and end and 0 <= (end - start).total_seconds() <= max_gap_seconds:
                prev.extend(g)
                continue
        merged.append(g)
    return merged


# --------------------------------------------------------------------------
# Text rules
# --------------------------------------------------------------------------

PRICE_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[  ]\d{3})+|\d+)\s*(?:грн\.?|uah|₴|гр\.?)(?![a-zа-яіїєґ])",
    re.IGNORECASE,
)
LINK_RE = re.compile(r"(?:https?://|www\.)\S+|\b(?:t|telegram)\.me/\S*", re.IGNORECASE)
USERNAME_RE = re.compile(r"@[A-Za-z0-9_]{3,}")
PHONE_RE = re.compile(
    r"\+\d[\d\s()\-]{8,}\d|(?<!\d)(?:38)?\(?0\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
)
CONTACT_WORDS = re.compile(
    r"instagram|інстаграм|инстаграм|\binst\b|viber|вайбер|whatsapp|telegram|телеграм|tiktok|"
    r"менеджер|пишіть|пишите|звертайтесь|дропшип|реквізит|реквизит",
    re.IGNORECASE,
)
ORDER_WORDS = re.compile(r"замовлен|заказ|наявн|наличи|доп\.?\s*фото|замір|замер|опт\b|\bопт", re.I)
WHOLESALE_RE = re.compile(r"\bопт|від\s*\d+\s*(?:шт|пар)|от\s*\d+\s*(?:шт|пар)", re.IGNORECASE)
SIZE_LABEL_RE = re.compile(r"^(?:розмір\w*|размер\w*|р-р|розміри)\s*:?\s*(.*)$", re.IGNORECASE)
OUT_OF_STOCK_RE = re.compile(r"нема\w*\s+в\s+наявн|немає\s+в\s+наявн|нет\s+в\s+наличии", re.I)
HAS_LETTER = re.compile(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]")
LATIN = re.compile(r"[A-Za-z]{2,}")
CONTROL_RE = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f​-‏‪-‮⁦-⁩]")
TAG_RE = re.compile(r"<[^>]{0,200}>")

DESCRIPTION_KEEP = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(?:колір|колор|цвет|color)\b",
        r"^(?:виробник|производитель|made in|країна)\b",
        r"^(?:матеріал|материал|склад|состав|тканина|ткань)\b",
        r"маломір|великомір|маломер|большемер|розмір в розмір|размер в размер|в розмір|сіда|садит",
        r"коробк|комплект|упаковк|пильовик|пыльник|бирк",
        r"^(?:\d{2}(?:[.,]5)?\s*\(\s*\d{1,2}(?:[.,]\d+)?\s*(?:cm|см)?\s*\)\s*)+$",
    )
]


def clean_text(value: str) -> str:
    """Removes markup remnants, control characters and every contact."""
    value = TAG_RE.sub(" ", value)
    value = CONTROL_RE.sub("", value)
    value = LINK_RE.sub(" ", value)
    value = USERNAME_RE.sub(" ", value)
    value = PHONE_RE.sub(" ", value)
    return re.sub(r"[ \t ]+", " ", value).strip()


def lines_of(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def is_sensitive(line: str) -> bool:
    """Lines that must never reach customers (prices, contacts, ordering)."""
    return bool(
        PRICE_RE.search(line)
        or LINK_RE.search(line)
        or USERNAME_RE.search(line)
        or PHONE_RE.search(line)
        or CONTACT_WORDS.search(line)
        or ORDER_WORDS.search(line)
        or "дроп" in line.casefold()
        or "купити собі" in line.casefold()
    )


def looks_like_product(text: str) -> bool:
    return any(
        PRICE_RE.search(line) or SIZE_LABEL_RE.match(line) or "наявн" in line.casefold()
        for line in lines_of(text)
    )


def extract_title(text: str) -> str | None:
    for line in lines_of(text):
        if is_sensitive(line) or SIZE_LABEL_RE.match(line):
            continue
        title = clean_text(line).rstrip(" :;,-—")
        if HAS_LETTER.search(title):
            return title[:TITLE_MAX].rstrip()
    return None


def extract_description(text: str, title: str | None) -> str:
    kept: list[str] = []
    for line in lines_of(text):
        if is_sensitive(line):
            continue
        cleaned = clean_text(line)
        if not cleaned or cleaned == title:
            continue
        if any(p.search(cleaned) for p in DESCRIPTION_KEEP):
            # "Колір : Чорний;Зелений;" -> "Колір: Чорний, Зелений"
            cleaned = re.sub(r"\s*;\s*", ", ", cleaned).strip(" ,")
            kept.append(re.sub(r"\s+:\s*", ": ", cleaned))
    result = ""
    for line in kept:
        candidate = f"{result}\n{line}" if result else line
        if len(candidate) > DESCRIPTION_MAX:
            break
        result = candidate
    return result


def parse_int(raw: str) -> int:
    return int(re.sub(r"\D", "", raw))


LABEL_ONLY_RE = re.compile(r"(?:дроп|купити собі)[^:]*:\s*$")


@dataclass
class Prices:
    drop: set[int] = field(default_factory=set)
    reference: set[int] = field(default_factory=set)
    unlabeled: bool = False


def extract_prices(text: str) -> Prices:
    prices = Prices()
    lines = lines_of(text)
    for i, line in enumerate(lines):
        low = line.casefold()
        found = [parse_int(m.group(1)) for m in PRICE_RE.finditer(line)]
        # "Дроп ціна :" with the amount on the next line
        if not found and LABEL_ONLY_RE.search(low) and i + 1 < len(lines):
            nxt = lines[i + 1]
            if re.fullmatch(r"\s*" + PRICE_RE.pattern + r"\s*", nxt, re.IGNORECASE):
                found = [parse_int(m.group(1)) for m in PRICE_RE.finditer(nxt)]
        if not found:
            continue
        if "дроп" in low:
            prices.drop.update(found)
        elif "купити собі" in low or "купить себе" in low:
            prices.reference.update(found)
        elif WHOLESALE_RE.search(low):
            continue
        elif i and LABEL_ONLY_RE.search(lines[i - 1].casefold()):
            continue  # amount under a "Дроп ціна :" label, already handled above
        else:
            prices.unlabeled = True
    return prices


def retail_price(supplier: int, markup: Decimal, round_to: int, add: int = 0) -> int:
    """supplier * markup + add, rounded half up to a multiple of round_to."""
    value = (Decimal(supplier) * markup + add) / round_to
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP)) * round_to


LETTER_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "4XL", "5XL"]
LETTER_ALIASES = {"2XL": "XXL", "3XL": "XXXL"}
LETTER_TOKEN = r"(?:XXS|XS|XXXL|XXL|XL|2XL|3XL|4XL|5XL|S|M|L)"
CYRILLIC_LOOKALIKES = str.maketrans({"М": "M", "Х": "X", "Ѕ": "S", "І": "I"})


def _letter(token: str) -> str:
    return LETTER_ALIASES.get(token, token)


def parse_sizes(value: str) -> list[str] | None:
    """Returns sizes, [] for bag dimensions ("30х20х10"), None if nothing found."""
    if re.search(r"ONE\s*SIZE|ONESIZE|УНІВЕРСАЛ|УНИВЕРСАЛ", value.upper()):
        return ["One size"]
    v = value.upper().translate(CYRILLIC_LOOKALIKES)
    if re.search(r"\d+\s*[XХ×*]\s*\d+", v):
        return []
    v = re.sub(r"\([^)]*\)", " ", v)  # "42(27cm)" -> "42"
    numbers: set[float] = set()
    letters: set[str] = set()

    def take_range(m: re.Match) -> str:
        a, b = int(m.group(1)), int(m.group(2))
        if 30 <= a <= b <= 50 and b - a <= 15:
            numbers.update(range(a, b + 1))
        return " "

    v = re.sub(r"(?<!\d)(\d{2})\s*[-–—]\s*(\d{2})(?!\d)", take_range, v)

    def take_letter_range(m: re.Match) -> str:
        a, b = _letter(m.group(1)), _letter(m.group(2))
        if a in LETTER_ORDER and b in LETTER_ORDER and LETTER_ORDER.index(a) <= LETTER_ORDER.index(b):
            letters.update(LETTER_ORDER[LETTER_ORDER.index(a) : LETTER_ORDER.index(b) + 1])
        return " "

    v = re.sub(
        rf"(?<!\w)({LETTER_TOKEN})\s*[-–—]\s*({LETTER_TOKEN})(?!\w)", take_letter_range, v
    )
    for n in re.findall(r"(?<![\d.,])(\d{2}(?:[.,]5)?)(?![\d.,]*\d)", v):
        num = float(n.replace(",", "."))
        if 30 <= num <= 50:
            numbers.add(num)
    for token in re.findall(rf"(?<!\w)({LETTER_TOKEN})(?!\w)", v):
        letters.add(_letter(token))

    if not numbers and not letters:
        return None
    result = [s for s in LETTER_ORDER if s in letters]
    result += [str(int(n)) if n == int(n) else str(n) for n in sorted(numbers)]
    return result[:MAX_SIZES]


def extract_sizes(text: str) -> tuple[list[str], str | None]:
    """Returns (sizes, reason or None)."""
    lines = lines_of(text)
    for i, line in enumerate(lines):
        m = SIZE_LABEL_RE.match(line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest:
            sizes = parse_sizes(rest)
            return (sizes, None) if sizes is not None else ([], "sizes_unparsed")
        collected: list[str] = []
        for nxt in lines[i + 1 :]:
            found = parse_sizes(nxt)
            if not found:
                break
            collected += [s for s in found if s not in collected]
        return (collected[:MAX_SIZES], None) if collected else ([], "sizes_unparsed")
    # No label: only unambiguous ranges ("40-45", "S-XL") anywhere in the text.
    for line in lines:
        if PRICE_RE.search(line):
            continue
        if re.search(rf"(?<!\d)(?:3\d|4\d)\s*[-–]\s*(?:3\d|4\d)(?!\d)|(?<!\w){LETTER_TOKEN}\s*[-–]\s*{LETTER_TOKEN}(?!\w)", line.upper()):
            sizes = parse_sizes(line)
            if sizes:
                return sizes, None
    return [], "sizes_missing"


# --- optional per-supplier template rules ---------------------------------

EDGE_SYMBOLS_RE = re.compile(r"^[^\w(\"«]+|[^\w)\"».!?]+$")
SIZE_LABEL_SYMBOLS_RE = re.compile(r"^(розмір\w*|размер\w*|р-р)[^\w:]+$", re.IGNORECASE)
INSOLE_RE = re.compile(
    r"^(\d{2}(?:[.,]5)?)\s*[-–—]\s*(\d{2}(?:[.,]\d)?)\s*(?:см|cm)\.?$", re.IGNORECASE
)


def normalize_title(title: str, words: dict[str, str]) -> str:
    """Strips emoji and symbols at both ends, then replaces the first word
    when it is in `words` (casefolded key -> replacement)."""
    title = EDGE_SYMBOLS_RE.sub("", title).strip()
    first, _, rest = title.partition(" ")
    replacement = words.get(first.casefold())
    if replacement:
        title = f"{replacement} {rest}".strip()
    return title[:TITLE_MAX].rstrip()


def extract_insole_table(text: str) -> list[tuple[str, str]]:
    """Lines like "40-25 см" (EU size - insole length) -> [("40", "25"), ...]."""
    rows: list[tuple[str, str]] = []
    for line in lines_of(text):
        m = INSOLE_RE.match(line)
        if m:
            size = m.group(1).replace(",", ".")
            if 30 <= float(size) <= 50 and size not in {s for s, _ in rows}:
                rows.append((size, m.group(2).replace(",", ".")))
    return rows[:MAX_SIZES]


def strip_size_label_symbols(text: str) -> str:
    """"Размеры👇" -> "Размеры", so the generic size parser reads the lines below."""
    return "\n".join(SIZE_LABEL_SYMBOLS_RE.sub(r"\1", line) for line in text.splitlines())


def detect_category(title: str, text: str, keywords: dict[str, list[str]]) -> str | None:
    def classify(s: str) -> str | None | bool:
        s = s.casefold()
        if any(k in s for k in keywords["accessories"]):
            return None
        shoes = any(k in s for k in keywords["shoes"])
        clothes = any(k in s for k in keywords["clothes"])
        if shoes and clothes:
            return None
        if shoes:
            return "Взуття"
        if clothes:
            return "Одяг"
        return False  # no signal

    by_title = classify(title)
    if by_title is not False:
        return by_title
    body = "\n".join(l for l in lines_of(text) if not is_sensitive(l))
    by_body = classify(body)
    return by_body or None


# --------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------

PHOTO_HREF_RE = re.compile(r"^photos/[A-Za-z0-9_@.\-]{1,200}\.(?:jpe?g|png|webp)$", re.IGNORECASE)


def detect_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def load_photo(input_dir: Path, href: str, max_bytes: int) -> tuple[bytes, str] | str:
    """Returns (data, ext) or a reason code."""
    if not PHOTO_HREF_RE.match(href) or ".." in href:
        return "photo_bad_path"
    photos_dir = (input_dir / "photos").resolve()
    path = (input_dir / href).resolve()
    if path.parent != photos_dir:
        return "photo_bad_path"
    if not path.is_file():
        return "photo_missing"
    if path.stat().st_size > max_bytes:
        return "photo_too_large"
    data = path.read_bytes()
    ext = detect_image_type(data)
    if ext is None:
        return "photo_bad_type"
    return data, ext


def stored_name(product_id: str, data: bytes, ext: str) -> str:
    digest = hashlib.sha256(product_id.encode() + b"\0" + data).hexdigest()[:32]
    return f"{digest}.{ext}"


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


@dataclass
class Product:
    record: dict
    cost: dict
    photos: list[tuple[str, bytes]]  # (stored file name, data)


@dataclass
class Result:
    products: list[Product] = field(default_factory=list)
    skipped: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    photo_issues: dict[str, list[tuple[int, str]]] = field(default_factory=lambda: defaultdict(list))
    groups: int = 0


def product_id(channel: str, message_id: int) -> str:
    return hashlib.sha1(f"{channel}:{message_id}".encode()).hexdigest()[:12]


def build_products(input_dir: Path, config: dict) -> Result:
    channel = config["channel"]
    markup = Decimal(str(config.get("markup", 1)))
    markup_add = int(config.get("markup_add", 0))
    round_to = int(config["round_to"])
    keywords = config["keywords"]
    fixed_category = config.get("category")
    title_words = config.get("title_words")
    size_table = bool(config.get("size_table"))
    result = Result()
    groups = merge_split_albums(
        group_albums(parse_export(input_dir)), int(config.get("album_gap_seconds", 0))
    )
    result.groups = len(groups)

    for group in groups:
        msg_id = group[0].msg_id
        photos = [href for b in group for href in b.photos]
        caption_at = [i for i, b in enumerate(group) if b.text]
        text = group[caption_at[0]].text if caption_at else ""
        lines = lines_of(text)
        first_line = lines[0].casefold() if lines else ""

        if not photos:
            reason = "no_photo" if text and looks_like_product(text) else "text_announcement"
            result.skipped[reason if text else "no_photo"].append(msg_id)
            continue
        if any(k in first_line for k in keywords["restock"]):
            result.skipped["restock_announcement"].append(msg_id)
            continue
        if not text:
            result.skipped["no_caption"].append(msg_id)
            continue
        if not looks_like_product(text):
            result.skipped["ad_or_announcement"].append(msg_id)
            continue

        pid = product_id(channel, msg_id)
        reasons: list[str] = []
        if caption_at[0] != 0:
            reasons.append("caption_not_first")

        title = extract_title(text)
        if title is not None and title_words is not None:
            title = normalize_title(title, title_words) or None
        if title is None:
            reasons.append("title_missing")

        prices = extract_prices(text)
        supplier = None
        if len(prices.drop) == 1:
            value = next(iter(prices.drop))
            if config["min_supplier_price"] <= value <= config["max_supplier_price"]:
                supplier = value
            else:
                reasons.append("price_out_of_range")
        elif len(prices.drop) > 1:
            reasons.append("multiple_drop_prices")
        else:
            reasons.append("no_drop_price")
        if prices.unlabeled and supplier is None:
            reasons.append("price_unlabeled")
        reference = next(iter(prices.reference)) if len(prices.reference) == 1 else None

        category = fixed_category or detect_category(title or "", text, keywords)
        if category is None:
            reasons.append("category_unknown")

        insole = extract_insole_table(text) if size_table else []
        if insole:
            sizes, size_reason = [size for size, _ in insole], None
        elif size_table:
            sizes, size_reason = extract_sizes(strip_size_label_symbols(text))
        else:
            sizes, size_reason = extract_sizes(text)
        if size_reason == "sizes_unparsed" or (size_reason == "sizes_missing" and category):
            reasons.append(size_reason)

        stored: list[tuple[str, bytes]] = []
        for href in photos[:MAX_IMAGES]:
            loaded = load_photo(input_dir, href, int(config["max_photo_bytes"]))
            if isinstance(loaded, str):
                result.photo_issues[loaded].append((msg_id, Path(href).name))
                continue
            data, ext = loaded
            name = stored_name(pid, data, ext)
            if name not in {n for n, _ in stored}:
                stored.append((name, data))
        if not stored:
            result.skipped["no_valid_photo"].append(msg_id)
            continue
        if len(stored) < len(photos[:MAX_IMAGES]):
            reasons.append("photo_rejected")

        description = extract_description(text, title)
        if insole:
            table = "\n".join(f"{size} — {cm} см" for size, cm in insole)
            description = "\n".join(x for x in (f"Довжина устілки:\n{table}", description) if x)
            description = description[:DESCRIPTION_MAX]

        record = {
            "id": pid,
            "title": title or UNTITLED,
            "description": description,
            "category": category or "Одяг",
            "price_retail": retail_price(supplier, markup, round_to, markup_add) if supplier else 0,
            "sizes": sizes,
            "in_stock": not OUT_OF_STOCK_RE.search(text),
            "popular": False,
            "images": [f"uploads/products/{name}" for name, _ in stored],
            "source_channel": channel,
            "source_message_id": msg_id,
            "needs_review": False,
            "review_reasons": reasons,
        }
        cost = {"price_supplier": supplier, "price_reference": reference}
        result.products.append(Product(record, cost, stored))

    for p in result.products:
        # "Найпопулярніше худі", "ТОП 🔝": no brand/model, the admin must rename it.
        # Repeated branded titles are usually colourways and are not flagged.
        if p.record["title"] != UNTITLED and not LATIN.search(p.record["title"]):
            p.record["review_reasons"].append("generic_title")
        p.record["review_reasons"] = p.record["review_reasons"][:10]
        p.record["needs_review"] = bool(p.record["review_reasons"])

    result.products.sort(key=lambda p: p.record["source_message_id"], reverse=True)
    return result


# --------------------------------------------------------------------------
# Duplicate detection (exact photo match). Never deletes or merges anything:
# duplicates get the review reason possible_duplicate and duplicate_group =
# id of the group's main product. Existing DB rows are only read.
# --------------------------------------------------------------------------

DUPLICATE_REASON = "possible_duplicate"
# A photo shared by more products than this is treated as a common image
# (banner, size chart) and does not link products into a group.
COMMON_PHOTO_LIMIT = 5
DB_IMAGE_RE = re.compile(
    r"^(public/images/products|images/products|uploads/products)/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:jpg|jpeg|png|webp))$"
)


@dataclass
class Known:
    """A product that takes part in duplicate detection."""

    id: str
    title: str
    channel: str | None
    message_id: int | None
    status: str  # "new" for products of this import
    created_at: str
    photos: int
    hashes: set[str]


@dataclass
class DuplicateGroup:
    main: Known
    members: list[Known]  # without the main product


@dataclass
class Duplicates:
    groups: list[DuplicateGroup] = field(default_factory=list)
    db_used: bool = False
    db_products: int = 0
    common_photos: int = 0


def photo_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_known(db_path: Path, uploads_dir: Path) -> list[Known] | None:
    """Products already in the DB with the sha256 of each image file. The DB is
    opened read-only; returns None when there is no DB file."""
    if not db_path.is_file():
        return None
    roots = {
        "uploads/products": uploads_dir / "products",
        "public/images/products": REPO / "public" / "images" / "products",
        "images/products": REPO / "images" / "products",
    }
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        products = conn.execute(
            "SELECT id, title, source_channel, source_message_id, status, created_at FROM products"
        ).fetchall()
        images = conn.execute("SELECT product_id, path FROM product_images").fetchall()
    finally:
        conn.close()

    known = {
        pid: Known(pid, title, channel, msg_id, status, created_at, 0, set())
        for pid, title, channel, msg_id, status, created_at in products
    }
    for pid, path in images:
        m = DB_IMAGE_RE.match(path or "")
        if pid not in known or not m:
            continue
        file = roots[m.group(1)] / m.group(2)  # file name only: no traversal
        known[pid].photos += 1
        if file.is_file():
            known[pid].hashes.add(photo_hash(file.read_bytes()))
    return list(known.values())


def find_duplicates(result: Result, existing: list[Known] | None) -> Duplicates:
    """Flags products of `result` that share a photo file (sha256) with another
    product of this import or of the DB. Deterministic, so re-runs give the
    same flags: the main product of a group is the oldest DB row that is not
    part of this import, else the earliest message of this import."""
    dups = Duplicates(db_used=existing is not None, db_products=len(existing or []))
    new_ids = {p.record["id"] for p in result.products}
    nodes: dict[str, Known] = {
        k.id: k for k in existing or [] if k.id not in new_ids  # a re-imported id is the new record
    }
    for p in result.products:
        r = p.record
        nodes[r["id"]] = Known(
            r["id"], r["title"], r["source_channel"], r["source_message_id"], "new", "",
            len(p.photos), {photo_hash(data) for _, data in p.photos},
        )

    by_hash: dict[str, set[str]] = defaultdict(set)
    for node in nodes.values():
        for h in node.hashes:
            by_hash[h].add(node.id)

    parent = {pid: pid for pid in nodes}

    def root(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for ids in by_hash.values():
        if len(ids) > COMMON_PHOTO_LIMIT:
            dups.common_photos += 1
            continue
        first, *rest = sorted(ids)
        for other in rest:
            parent[root(other)] = root(first)

    components: dict[str, list[Known]] = defaultdict(list)
    for pid, node in nodes.items():
        components[root(pid)].append(node)

    def age(k: Known) -> tuple:
        if k.status != "new":
            return (0, k.created_at, k.id)
        return (1, "", k.message_id or 0, k.id)

    records = {p.record["id"]: p.record for p in result.products}
    for members in components.values():
        if len(members) < 2 or not any(m.id in records for m in members):
            continue
        members.sort(key=age)
        main, others = members[0], members[1:]
        for m in others:
            record = records.get(m.id)
            if record is None:
                continue  # an existing row: never changed by the importer
            record["duplicate_group"] = main.id
            reasons = record["review_reasons"]
            if DUPLICATE_REASON not in reasons:
                reasons[:] = (reasons[:9] + [DUPLICATE_REASON])
            record["needs_review"] = True
        dups.groups.append(DuplicateGroup(main, others))
    dups.groups.sort(key=lambda g: age(g.main))
    return dups


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_outputs(result: Result, out_public: Path, out_costs: Path, uploads: Path) -> int:
    """Writes JSON files and copies photos. Returns the number of new photo files."""
    target = uploads / "products"
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for p in result.products:
        for name, data in p.photos:
            dest = target / name
            if not dest.exists():
                write_atomic(dest, data)
                copied += 1
    public = [p.record for p in result.products]
    costs = {
        p.record["id"]: p.cost
        for p in result.products
        if any(v is not None for v in p.cost.values())
    }
    dump = lambda obj: (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    write_atomic(out_public, dump(public))
    write_atomic(out_costs, dump(costs))
    return copied


def summary(result: Result) -> dict[str, int]:
    review = sum(1 for p in result.products if p.record["needs_review"])
    return {
        "groups": result.groups,
        "products": len(result.products),
        "ready": len(result.products) - review,
        "needs_review": review,
        "skipped": sum(len(v) for v in result.skipped.values()),
        "photos": sum(len(p.photos) for p in result.products),
        "photo_issues": sum(len(v) for v in result.photo_issues.values()),
    }


def _ids(values: list, limit: int = 400) -> str:
    shown = ", ".join(str(v) for v in values[:limit])
    return shown + (f", … (+{len(values) - limit})" if len(values) > limit else "")


def _pricing(config: dict) -> str:
    if "markup_add" in config:
        return f"markup +{config['markup_add']} UAH"
    return f"markup {config['markup']}"


def _cell(text: str) -> str:
    return re.sub(r"[|`\r\n]+", " ", text).strip()


def _describe(k: Known) -> str:
    where = f"{k.channel or 'demo'}" + (f" msg {k.message_id}" if k.message_id is not None else "")
    state = "new in this import" if k.status == "new" else f"in DB, {k.status}"
    return f"`{k.id}` · {where} · {state} · {k.photos} photos · {_cell(k.title)}"


def render_duplicates(dups: Duplicates) -> list[str]:
    flagged = sum(1 for g in dups.groups for m in g.members if m.status == "new")
    out = [
        "## Possible duplicates (exact photo match, sha256)",
        "",
        (f"Compared with {dups.db_products} products in the DB (read-only) and with each other."
         if dups.db_used else "DB not found: compared only within this import."),
        f"Groups: {len(dups.groups)} · flagged new products: {flagged} · "
        f"photos ignored as common images (> {COMMON_PHOTO_LIMIT} products): {dups.common_photos}",
        "",
        "Flagged products stay draft with `possible_duplicate`; nothing is deleted or merged.",
        "The main product is not flagged; DB rows are never changed by the importer.",
        "",
    ]
    if not dups.groups:
        out.append("None.")
    for n, g in enumerate(dups.groups, 1):
        out += [f"### Group {n} — main {_describe(g.main)}", ""]
        out += [f"- {_describe(m)}" for m in g.members]
        out.append("")
    return out


def render_report(result: Result, config: dict, dry_run: bool, dups: Duplicates | None = None) -> str:
    s = summary(result)
    review_by_reason: dict[str, list[int]] = defaultdict(list)
    for p in result.products:
        for r in p.record["review_reasons"]:
            review_by_reason[r].append(p.record["source_message_id"])
    no_cost = sum(1 for p in result.products if p.cost["price_supplier"] is None)

    out = [
        "# Import report",
        "",
        f"Mode: {'dry run (nothing written except this report)' if dry_run else 'write'}  ",
        f"Channel: `{config['channel']}` · {_pricing(config)} · rounding {config['round_to']} UAH",
        "",
        "Message ids are Telegram message ids of the first block of each album.",
        "No message text is included in this report (duplicate groups show product titles).",
        "",
        "## Counts",
        "",
        "| | count |",
        "|---|---:|",
        f"| message groups (albums / single posts) | {s['groups']} |",
        f"| **products to import (all as draft)** | **{s['products']}** |",
        f"| … ready (no review flags) | {s['ready']} |",
        f"| … needs_review | {s['needs_review']} |",
        f"| … without supplier price (price_retail = 0) | {no_cost} |",
        f"| skipped | {s['skipped']} |",
        f"| photos to copy | {s['photos']} |",
        f"| photo files rejected | {s['photo_issues']} |",
        "",
        "## Skipped (by reason)",
        "",
    ]
    if not result.skipped:
        out.append("None.")
    for reason, ids in sorted(result.skipped.items(), key=lambda kv: -len(kv[1])):
        out += [f"### `{reason}` — {len(ids)}", "", _ids(sorted(ids)), ""]
    out += ["## needs_review (by reason)", "", "A product can have several reasons.", ""]
    if not review_by_reason:
        out.append("None.")
    for reason, ids in sorted(review_by_reason.items(), key=lambda kv: -len(kv[1])):
        out += [f"### `{reason}` — {len(ids)}", "", _ids(sorted(ids)), ""]
    if dups is not None:
        out += ["", *render_duplicates(dups)]
    out += ["", "## Photo files rejected", ""]
    if not result.photo_issues:
        out.append("None.")
    for reason, items in sorted(result.photo_issues.items()):
        out += [f"### `{reason}` — {len(items)}", ""]
        out += [f"- message {mid}: `{name}`" for mid, name in items[:200]]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


SHARED_KEYS = {
    "round_to": int, "max_photo_bytes": int, "min_supplier_price": int,
    "max_supplier_price": int, "keywords": dict,
}
# Per-supplier keys. Exactly one of markup / markup_add is required.
SUPPLIER_KEYS = {
    "input": str,               # export folder, relative to the repo, inside import/raw/
    "markup": (int, float),     # retail = supplier * markup
    "markup_add": int,          # retail = supplier + markup_add (UAH)
    "album_gap_seconds": int,   # re-attach album tails split across seconds (0 = off)
    "category": str,            # fixed category instead of keyword detection
    "title_words": dict,        # strip emoji at title ends, replace the first word
    "size_table": bool,         # "40-25 см" insole lines -> sizes + description
}
CATEGORIES = ("Взуття", "Одяг")


def _fail(message: str) -> None:
    raise SystemExit(f"config: {message}")


def load_config(path: Path, supplier: str) -> dict:
    """Shared settings merged with one supplier entry; "channel" = the entry key."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, kind in SHARED_KEYS.items():
        if not isinstance(data.get(key), kind) or isinstance(data.get(key), bool):
            _fail(f"'{key}' is missing or has the wrong type")
    if data["round_to"] < 1:
        _fail("round_to must be >= 1")
    for group in ("shoes", "clothes", "accessories", "restock"):
        words = data["keywords"].get(group)
        if not isinstance(words, list) or not all(isinstance(w, str) and w for w in words):
            _fail(f"keywords.{group} must be a list of strings")
        data["keywords"][group] = [w.casefold() for w in words]

    suppliers = data.get("suppliers")
    if not isinstance(suppliers, dict) or not suppliers:
        _fail("'suppliers' must be a non-empty object")
    for name in suppliers:
        if not re.fullmatch(r"[a-z0-9_]{1,64}", name):
            _fail(f"supplier key {name!r} must match [a-z0-9_]{{1,64}}")
    if supplier not in suppliers:
        _fail(f"unknown supplier {supplier!r}; known: {', '.join(sorted(suppliers))}")
    entry = suppliers[supplier]
    if not isinstance(entry, dict):
        _fail(f"suppliers.{supplier} must be an object")
    for key, value in entry.items():
        kind = SUPPLIER_KEYS.get(key)
        if kind is None:
            _fail(f"suppliers.{supplier}.{key} is not a known setting")
        if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
            _fail(f"suppliers.{supplier}.{key} has the wrong type")

    if ("markup" in entry) == ("markup_add" in entry):
        _fail(f"suppliers.{supplier} needs exactly one of markup / markup_add")
    if "markup" in entry and not 1 <= entry["markup"] <= 10:
        _fail("markup must be within 1..10")
    if "markup_add" in entry and not 0 <= entry["markup_add"] <= 100000:
        _fail("markup_add must be within 0..100000")
    if not 0 <= entry.get("album_gap_seconds", 0) <= 10:
        _fail("album_gap_seconds must be within 0..10")
    if "category" in entry and entry["category"] not in CATEGORIES:
        _fail(f"category must be one of {', '.join(CATEGORIES)}")
    if "title_words" in entry:
        words = entry["title_words"]
        if not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in words.items()):
            _fail("title_words must map non-empty strings to non-empty strings")
        entry = {**entry, "title_words": {k.casefold(): v for k, v in words.items()}}

    config = {k: v for k, v in data.items() if k != "suppliers"}
    config.update(entry)
    config["channel"] = supplier
    if "input" in entry:
        raw_root = (REPO / "import" / "raw").resolve()
        folder = (REPO / entry["input"]).resolve()
        if raw_root not in folder.parents:
            _fail(f"suppliers.{supplier}.input must be a folder inside import/raw/")
        config["input"] = folder
    return config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--supplier", required=True, help="supplier key in the config (= channel key)")
    ap.add_argument("--input", type=Path, help="export folder (default: the supplier's 'input')")
    ap.add_argument("--config", type=Path, default=DEFAULTS["config"])
    ap.add_argument("--dry-run", action="store_true", help="only write the report")
    ap.add_argument("--out-public", type=Path)
    ap.add_argument("--out-costs", type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--uploads-dir", type=Path, default=DEFAULTS["uploads"])
    ap.add_argument("--db", type=Path, default=DEFAULTS["db"],
                    help="existing catalogue for duplicate detection (read-only)")
    args = ap.parse_args(argv)

    config = load_config(args.config, args.supplier)
    input_dir = args.input or config.get("input")
    if input_dir is None:
        ap.error(f"supplier {args.supplier!r} has no 'input' in the config; pass --input")
    if not export_files(input_dir):
        ap.error(f"no messages*.html in {input_dir}")
    outputs = default_outputs(config["channel"])
    out_public = args.out_public or outputs["out_public"]
    out_costs = args.out_costs or outputs["out_costs"]
    report = args.report or outputs["report"]

    result = build_products(input_dir, config)
    dups = find_duplicates(result, load_known(args.db, args.uploads_dir))

    copied = 0
    if not args.dry_run:
        copied = write_outputs(result, out_public, out_costs, args.uploads_dir)
    write_atomic(report, render_report(result, config, args.dry_run, dups).encode("utf-8"))

    s = summary(result)
    print(
        f"{'DRY RUN: ' if args.dry_run else ''}supplier={config['channel']} "
        f"groups={s['groups']} products={s['products']} "
        f"ready={s['ready']} needs_review={s['needs_review']} skipped={s['skipped']} "
        f"photos={s['photos']} photo_issues={s['photo_issues']} duplicate_groups={len(dups.groups)}"
        + ("" if args.dry_run else f" new_photo_files={copied}")
    )
    if not args.dry_run:
        print(f"public: {out_public}\ncosts: {out_costs}")
    print(f"report: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
