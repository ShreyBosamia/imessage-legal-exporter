#!/usr/bin/env python3
"""Fail when tracked files contain common private-data or secret patterns."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_PATH = Path("scripts/privacy_scan.py")
TEXT_EXCLUSIONS: set[str] = set()
FORBIDDEN_PATH_PARTS = {
    "attachments",
    "derived_media",
    "exports",
    "private_input",
    "source",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".storedata",
    ".csv",
    ".html",
    ".jpeg",
    ".jpg",
    ".jsonl",
    ".pdf",
    ".png",
}

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.I)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[ .-]?)?\(?([2-9]\d{2})\)?[ .-](\d{3})[ .-](\d{4})(?!\d)"
)
PHONE_LITERAL_RE = re.compile(r'''["']([+()0-9 .-]{10,})["']''')
USER_PATH_RE = re.compile(r"(?:/Users/|/home/|[A-Z]:\\Users\\)[^/\\\s]+", re.I)
DATA_IMAGE_RE = re.compile(r"data:image/[^;,]+;base64,[A-Za-z0-9+/=]{256,}")
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "OpenAI key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
}
ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
    "users.noreply.github.com",
}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / value.decode() for value in result.stdout.split(b"\0") if value]


def is_reserved_example_phone(match: re.Match[str]) -> bool:
    return match.group(2) == "555" and 100 <= int(match.group(3)) <= 199


def has_non_reserved_phone_literal(text: str) -> bool:
    for match in PHONE_LITERAL_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10 or digits[0] in "01":
            continue
        if digits[3:6] != "555" or not 100 <= int(digits[6:]) <= 199:
            return True
    return False


def main() -> int:
    findings: list[tuple[str, str]] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        lowered_parts = {part.casefold() for part in relative.parts[:-1]}
        lowered_name = relative.name.casefold()
        if lowered_parts & FORBIDDEN_PATH_PARTS or any(
            lowered_name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES
        ):
            findings.append((str(relative), "forbidden evidence/output path"))
            continue
        if str(relative) in TEXT_EXCLUSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        if relative != SCAN_PATH and USER_PATH_RE.search(text):
            findings.append((str(relative), "machine-specific user path"))
        if DATA_IMAGE_RE.search(text):
            findings.append((str(relative), "embedded base64 image"))
        if any(
            match.group(1).casefold() not in ALLOWED_EMAIL_DOMAINS
            for match in EMAIL_RE.finditer(text)
        ):
            findings.append((str(relative), "non-example email address"))
        if any(
            not is_reserved_example_phone(match) for match in PHONE_RE.finditer(text)
        ) or has_non_reserved_phone_literal(text):
            findings.append((str(relative), "non-reserved phone number"))
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append((str(relative), label))

    if findings:
        print("Privacy scan failed. Review these tracked files:", file=sys.stderr)
        for path, category in sorted(set(findings)):
            print(f"- {path}: {category}", file=sys.stderr)
        return 1
    print("Privacy scan passed for tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
