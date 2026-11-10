"""Command line tools. Run from backend/:

    python -m app.manage init-db
    python -m app.manage import-json --public products.public.json --costs import/private/costs.json
    python -m app.manage seed-demo

Relative --public/--costs paths are resolved against the current directory.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import BACKEND_DIR, load_settings
from .db import connect, init_db
from .importer import ImportReport, import_files

DEMO_FILE = BACKEND_DIR / "seed" / "demo_products.json"


def _print_report(report: ImportReport) -> None:
    print(
        f"inserted={report.inserted} updated={report.updated} "
        f"unchanged={report.unchanged} protected={report.protected} "
        f"errors={len(report.errors)}"
    )
    for line in report.errors:
        print(f"  ! {line}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.manage")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create tables if missing")

    imp = sub.add_parser("import-json", help="upsert products from importer output (as drafts)")
    imp.add_argument("--public", required=True, type=Path, help="products.public.json")
    imp.add_argument("--costs", type=Path, default=None, help="costs.json (optional)")

    sub.add_parser("seed-demo", help="insert the 7 demo products as published (missing ones only)")

    args = parser.parse_args(argv)
    settings = load_settings()
    conn = connect(settings.database_path)
    try:
        init_db(conn)
        if args.command == "init-db":
            print(f"database ready: {settings.database_path}")
        elif args.command == "import-json":
            for path in (args.public, args.costs):
                if path is not None and not path.is_file():
                    parser.error(f"file not found: {path}")
            _print_report(import_files(conn, args.public, args.costs, mode="import"))
        elif args.command == "seed-demo":
            _print_report(import_files(conn, DEMO_FILE, None, mode="seed"))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
