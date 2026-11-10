"""Idempotent product import (upsert by id).

Rules:
- New ids are inserted as status=draft (parsed data needs manual review).
- An existing row is refreshed only while it is still an untouched draft:
  rows with edited_by_admin=1 or a status other than draft are never changed.
- Unchanged rows are not rewritten, so updated_at stays stable on re-runs.
- Invalid records are skipped and reported; the rest is applied in one transaction.

seed mode is used only for the bundled demo catalogue: it inserts missing rows
as published and never touches existing rows.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from .db import utc_now
from .schemas import CostEntry, ImportRecord


@dataclass
class ImportReport:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    protected: int = 0
    errors: list[str] = field(default_factory=list)


def _error_text(exc: ValidationError) -> str:
    # Only field locations and messages; never echo input values.
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
    )


def load_records(path: Path, report: ImportReport) -> list[ImportRecord]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: top level must be a JSON array")
    records: list[ImportRecord] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        try:
            record = ImportRecord.model_validate(item)
        except ValidationError as exc:
            report.errors.append(f"item #{index}: {_error_text(exc)}")
            continue
        if record.id in seen:
            report.errors.append(f"item #{index}: duplicate id {record.id!r}, skipped")
            continue
        seen.add(record.id)
        records.append(record)
    return records


def load_costs(path: Path | None, report: ImportReport) -> dict[str, CostEntry]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level must be a JSON object")
    costs: dict[str, CostEntry] = {}
    for key, value in data.items():
        try:
            costs[str(key)] = CostEntry.model_validate(value)
        except ValidationError as exc:
            report.errors.append(f"costs[{key!r}]: {_error_text(exc)}")
    return costs


def _values(record: ImportRecord, cost: CostEntry | None) -> tuple:
    return (
        record.title,
        record.description,
        record.category,
        record.price_retail,
        cost.price_supplier if cost else None,
        cost.price_reference if cost else None,
        json.dumps(record.sizes, ensure_ascii=False),
        int(record.in_stock),
        int(record.popular),
        record.source_channel,
        record.source_message_id,
        int(record.needs_review or bool(record.review_reasons)),
        ",".join(record.review_reasons),
        record.duplicate_group,
    )


_COLUMNS = (
    "title, description, category, price_retail, price_supplier, price_reference, sizes, "
    "in_stock, popular, source_channel, source_message_id, needs_review, review_reasons, "
    "duplicate_group"
)


def _replace_images(conn: sqlite3.Connection, product_id: str, paths: list[str]) -> None:
    conn.execute("DELETE FROM product_images WHERE product_id = ?", (product_id,))
    conn.executemany(
        "INSERT INTO product_images (product_id, path, position) VALUES (?, ?, ?)",
        [(product_id, p, i) for i, p in enumerate(paths)],
    )


def apply_records(
    conn: sqlite3.Connection,
    records: list[ImportRecord],
    costs: dict[str, CostEntry],
    report: ImportReport,
    mode: Literal["import", "seed"] = "import",
) -> ImportReport:
    status = "published" if mode == "seed" else "draft"
    with conn:  # one transaction; rolls back on any exception
        for record in records:
            values = _values(record, costs.get(record.id))
            row = conn.execute(
                f"SELECT {_COLUMNS}, status, edited_by_admin FROM products WHERE id = ?",
                (record.id,),
            ).fetchone()
            now = utc_now()

            if row is None:
                conn.execute(
                    f"INSERT INTO products (id, {_COLUMNS}, status, created_at, updated_at) "
                    f"VALUES ({', '.join('?' * (len(values) + 4))})",
                    (record.id, *values, status, now, now),
                )
                _replace_images(conn, record.id, record.images)
                report.inserted += 1
                continue

            if mode == "seed" or row["edited_by_admin"] or row["status"] != "draft":
                report.protected += 1
                continue

            current_images = [
                r["path"]
                for r in conn.execute(
                    "SELECT path FROM product_images WHERE product_id = ? ORDER BY position, id",
                    (record.id,),
                )
            ]
            if tuple(row)[: len(values)] == values and current_images == record.images:
                report.unchanged += 1
                continue

            assignments = ", ".join(f"{c.strip()} = ?" for c in _COLUMNS.split(","))
            conn.execute(
                f"UPDATE products SET {assignments}, updated_at = ? WHERE id = ?",
                (*values, now, record.id),
            )
            _replace_images(conn, record.id, record.images)
            report.updated += 1
    return report


def import_files(
    conn: sqlite3.Connection,
    public_path: Path,
    costs_path: Path | None,
    mode: Literal["import", "seed"] = "import",
) -> ImportReport:
    report = ImportReport()
    records = load_records(public_path, report)
    costs = load_costs(costs_path, report)
    return apply_records(conn, records, costs, report, mode)
