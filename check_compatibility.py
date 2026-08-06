#!/usr/bin/env python3
"""Privacy-safe preflight checks for iMessage Legal Exporter inputs."""

from __future__ import annotations

import argparse
import importlib.util
import platform
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


MINIMUM_PYTHON = (3, 10)
CHAT_TABLES = {"chat", "message", "handle", "chat_message_join", "chat_handle_join"}
CHAT_COLUMNS = {
    "chat": {"ROWID"},
    "message": {"ROWID", "date", "is_from_me"},
    "handle": {"ROWID", "id"},
}
CALL_TABLES = {"ZCALLRECORD"}
CALL_COLUMNS = {
    "ZCALLRECORD": {"Z_PK", "ZDATE", "ZDURATION", "ZADDRESS", "ZORIGINATED"}
}


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str


def expand_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def sqlite_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
    }


def check_database(
    path: Path,
    *,
    label: str,
    required_tables: set[str],
    required_columns: dict[str, set[str]],
) -> Check:
    if not path.exists():
        return Check("FAIL", label, "file does not exist")
    if not path.is_file():
        return Check("FAIL", label, "path is not a regular file")
    try:
        with connect_read_only(path) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            tables = sqlite_tables(connection)
            missing_tables = sorted(required_tables - tables)
            missing_columns = {
                table: sorted(columns - sqlite_columns(connection, table))
                for table, columns in required_columns.items()
                if table in tables and columns - sqlite_columns(connection, table)
            }
    except (OSError, sqlite3.Error) as exc:
        return Check("FAIL", label, f"not a readable SQLite database ({exc})")
    if quick_check != "ok":
        return Check("FAIL", label, f"SQLite quick_check returned {quick_check!r}")
    if missing_tables:
        return Check("FAIL", label, f"missing tables: {', '.join(missing_tables)}")
    if missing_columns:
        details = "; ".join(
            f"{table}: {', '.join(columns)}"
            for table, columns in sorted(missing_columns.items())
        )
        return Check("FAIL", label, f"missing columns: {details}")
    return Check("PASS", label, "required SQLite structure is present")


def runtime_checks() -> list[Check]:
    checks = [
        Check(
            "PASS" if sys.version_info >= MINIMUM_PYTHON else "FAIL",
            "Python",
            f"{platform.python_version()} (requires 3.10 or newer)",
        ),
        Check(
            "PASS" if platform.system() == "Darwin" else "WARN",
            "Operating system",
            f"{platform.system()} (real Apple database acquisition is supported on macOS)",
        ),
        Check(
            "PASS" if importlib.util.find_spec("typedstream") else "WARN",
            "pytypedstream",
            "available" if importlib.util.find_spec("typedstream") else "missing; some attributed bodies may be undecoded",
        ),
        Check(
            "PASS" if shutil.which("node") else "WARN",
            "Node.js",
            "available" if shutil.which("node") else "missing; PDF rendering will be unavailable",
        ),
        Check(
            "PASS" if (Path(__file__).resolve().parent / "node_modules" / "playwright").exists() else "WARN",
            "Playwright package",
            "installed" if (Path(__file__).resolve().parent / "node_modules" / "playwright").exists() else "missing; run npm install and npm run install-browsers",
        ),
    ]
    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check runtime dependencies and copied Apple database structure without "
            "printing message bodies, handles, or call participants."
        )
    )
    parser.add_argument("--chat-db", help="Optional path to a copied chat.db.")
    parser.add_argument("--call-db", help="Optional path to a copied CallHistory.storedata.")
    parser.add_argument(
        "--attachments-root",
        help="Optional Messages Attachments directory to check for readability.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    checks = runtime_checks()
    if args.chat_db:
        checks.append(
            check_database(
                expand_path(args.chat_db),
                label="Messages database",
                required_tables=CHAT_TABLES,
                required_columns=CHAT_COLUMNS,
            )
        )
    if args.call_db:
        checks.append(
            check_database(
                expand_path(args.call_db),
                label="Call-history database",
                required_tables=CALL_TABLES,
                required_columns=CALL_COLUMNS,
            )
        )
    if args.attachments_root:
        attachment_path = expand_path(args.attachments_root)
        checks.append(
            Check(
                "PASS" if attachment_path.is_dir() else "WARN",
                "Attachments directory",
                "readable directory found" if attachment_path.is_dir() else "not found; attachment files may be missing from the export",
            )
        )

    for check in checks:
        print(f"[{check.status}] {check.name}: {check.detail}")
    return 1 if any(check.status == "FAIL" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
