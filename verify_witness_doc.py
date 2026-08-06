#!/usr/bin/env python3
"""
Load and audit machine-readable message/call timelines without printing bodies.

The document audit is intentionally conservative.  It reports which source
timelines contain a referenced sequence, whether the event date matches the
nearest dated heading, and whether a quoted phrase occurs in the event text.
It does not decide that a date mismatch is an error because witness documents
often cite older background events from a later journal entry.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set


class ExportTimeline:
    def __init__(self, export_dir: Path):
        self.export_dir = Path(export_dir)
        self.timeline_path = self.export_dir / "timeline.jsonl"
        self.items: List[Dict[str, Any]] = []
        self.messages_by_seq: Dict[int, Dict[str, Any]] = {}
        self.calls_by_seq: Dict[int, Dict[str, Any]] = {}
        self._load_timeline()

    def _load_timeline(self) -> None:
        if not self.timeline_path.exists():
            raise FileNotFoundError(
                f"Could not find timeline.jsonl in {self.export_dir}. "
                "Please run an export first."
            )
        with self.timeline_path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                self.items.append(item)
                seq = item.get("sequence")
                if seq is None:
                    continue
                if item.get("type") == "message":
                    self.messages_by_seq[int(seq)] = item
                elif item.get("type") == "call":
                    self.calls_by_seq[int(seq)] = item

    def get_message(self, seq: int) -> Optional[Dict[str, Any]]:
        """Retrieve a message by its sequence number (e.g. ``#14``)."""
        return self.messages_by_seq.get(seq)

    def get_call(self, seq: int) -> Optional[Dict[str, Any]]:
        """Retrieve a call event by its sequence number (e.g. ``call 3``)."""
        return self.calls_by_seq.get(seq)

    def get_event(self, kind: str, seq: int) -> Optional[Dict[str, Any]]:
        if kind == "message":
            return self.get_message(seq)
        if kind == "call":
            return self.get_call(seq)
        raise ValueError(f"Unsupported event kind: {kind}")

    def search_text(self, query: str, case_sensitive: bool = False) -> List[Dict[str, Any]]:
        """Search message text for a query string.

        This importable API retains the original behavior.  Callers should
        avoid printing the returned records because they contain private text.
        """
        results = []
        for item in self.items:
            if item.get("type") != "message":
                continue
            text = item.get("text") or ""
            if (query in text if case_sensitive else query.lower() in text.lower()):
                results.append(item)
        return results

    def get_slice(self, start_idx: int, end_idx: int) -> List[Dict[str, Any]]:
        """Get a slice of chronologically sorted timeline items."""
        return self.items[start_idx:end_idx]


_MONTH_NAMES = (
    ("jan", "january"),
    ("feb", "february"),
    ("mar", "march"),
    ("apr", "april"),
    ("may", "may"),
    ("jun", "june"),
    ("jul", "july"),
    ("aug", "august"),
    ("sep", "september"),
    ("oct", "october"),
    ("nov", "november"),
    ("dec", "december"),
)
_MONTHS = {
    alias: number
    for number, aliases in enumerate(_MONTH_NAMES, start=1)
    for alias in aliases
}
_MONTH_PATTERN = "|".join(
    full if short == full else f"{short}|{full}"
    for short, full in _MONTH_NAMES
)
_HEADER_DATE_RE = re.compile(
    r"(?ix)\b"
    rf"(?P<m1>{_MONTH_PATTERN})\s+"
    r"(?P<d1>\d{1,2})"
    rf"(?:\s*-\s*(?:(?P<m2>{_MONTH_PATTERN})\s+)?"
    r"(?P<d2>\d{1,2}))?"
    r",?\s+(?P<year>\d{4})"
)
_CALL_REF_RE = re.compile(
    r"(?i)\bcall(?:\s+events?)?\s*`?\s*(?:\\#|#)\s*\d+"
    r"(?:"
    r"\s*`?\s*(?:-|,)\s*(?:and\s*)?`?\s*(?:\\#|#)\s*\d+"
    r"|\s*`?\s+and\s+`?\s*(?:\\#|#)\s*\d+"
    r")*"
)
_MESSAGE_REF_RE = re.compile(r"#\s*(\d+)")
_QUOTE_RE = re.compile(r'“([^”]{1,10000})”|"([^"\n]{1,10000})"')
_MAX_TEXT_SCAN_LINE = 10_000


def _header_dates(line: str) -> Set[dt.date]:
    """Parse dated Markdown headings, including cross-month ranges."""
    if not line.lstrip().startswith("#"):
        return set()
    dates: Set[dt.date] = set()
    parsed_endpoints: List[dt.date] = []
    for match in _HEADER_DATE_RE.finditer(line):
        year = int(match.group("year"))
        month = _MONTHS[match.group("m1").lower()]
        first = int(match.group("d1"))
        second = int(match.group("d2") or first)
        end_month = _MONTHS[(match.group("m2") or match.group("m1")).lower()]
        start = dt.date(year, month, first)
        end = dt.date(year, end_month, second)
        if end < start:
            # A cross-year range is unusual, but preserving both endpoints is
            # less misleading than failing the complete document audit.
            dates.update((start, end))
            continue
        cursor = start
        while cursor <= end:
            dates.add(cursor)
            cursor += dt.timedelta(days=1)
        parsed_endpoints.extend((start, end))
    if len(parsed_endpoints) >= 2 and re.search(r"(?i)\b(?:through|into|to)\b|[-]", line):
        start, end = min(parsed_endpoints), max(parsed_endpoints)
        cursor = start
        while cursor <= end:
            dates.add(cursor)
            cursor += dt.timedelta(days=1)
    return dates


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def _quoted_phrases(line: str) -> List[str]:
    return [match.group(1) or match.group(2) for match in _QUOTE_RE.finditer(line)]


def _source_hints(line: str, labels: Iterable[str]) -> List[str]:
    """Return export labels explicitly mentioned in a document line."""
    normalized_line = _normalize_text(line)
    hints = []
    for label in labels:
        normalized_label = _normalize_text(label)
        if not normalized_label:
            continue
        if re.search(
            rf"(?<!\w){re.escape(normalized_label)}(?!\w)", normalized_line
        ):
            hints.append(label)
    return sorted(set(hints), key=str.casefold)


def _reference_matches(line: str) -> List[tuple[str, int]]:
    """Return explicit message/call refs once each, avoiding overlap."""
    matches: List[tuple[str, int, int, int]] = []
    for match in _CALL_REF_RE.finditer(line):
        for sequence in re.findall(r"(?:\\#|#)\s*(\d+)", match.group(0)):
            matches.append(("call", int(sequence), match.start(), match.end()))
    for match in _MESSAGE_REF_RE.finditer(line):
        if any(start <= match.start() < end for _, _, start, end in matches):
            continue
        matches.append(("message", int(match.group(1)), match.start(), match.end()))
    matches.sort(key=lambda item: item[2])
    return [(kind, seq) for kind, seq, _, _ in matches]


def _candidate_metadata(
    source: str,
    kind: str,
    sequence: int,
    item: Optional[Mapping[str, Any]],
    section_dates: Set[dt.date],
    phrases: Sequence[str],
) -> Dict[str, Any]:
    candidate: Dict[str, Any] = {"source": source, "exists": item is not None}
    if item is None:
        return candidate

    timestamp = item.get("timestamp_local")
    event_date = None
    if timestamp:
        try:
            event_date = dt.datetime.fromisoformat(timestamp).date()
        except ValueError:
            pass
    candidate.update(
        {
            "kind": kind,
            "sequence": sequence,
            "timestamp_local": timestamp,
            "event_date": event_date.isoformat() if event_date else None,
            "date_matches_section": bool(event_date and event_date in section_dates),
            "direction": item.get("direction"),
            "source_rowid": item.get("source_rowid"),
        }
    )
    if kind == "message":
        body = _normalize_text(str(item.get("text") or ""))
        candidate["quoted_text_matches"] = any(
            len(_normalize_text(phrase)) >= 8
            and _normalize_text(phrase) in body
            for phrase in phrases
        )
    else:
        candidate.update(
            {
                "answered_label": item.get("answered_label"),
                "call_type_label": item.get("call_type_label"),
                "duration_seconds": item.get("duration_seconds"),
            }
        )
    return candidate


def audit_witness_document(
    document_path: Path,
    exports: Mapping[str, ExportTimeline],
) -> Dict[str, Any]:
    """Audit explicit document references without returning message bodies."""
    document_path = Path(document_path)
    lines = document_path.read_text(encoding="utf-8", errors="replace").splitlines()
    section_dates: Set[dt.date] = set()
    references: List[Dict[str, Any]] = []
    embedded_image_lines = 0

    for line_number, line in enumerate(lines, start=1):
        heading_dates = _header_dates(line)
        if heading_dates:
            section_dates = heading_dates
        if "data:" in line and len(line) > _MAX_TEXT_SCAN_LINE:
            embedded_image_lines += 1
        if len(line) > _MAX_TEXT_SCAN_LINE:
            continue

        refs = _reference_matches(line)
        if not refs:
            continue
        phrases = _quoted_phrases(line)
        line_hints = _source_hints(line, exports)
        for kind, sequence in refs:
            candidates = [
                _candidate_metadata(
                    source,
                    kind,
                    sequence,
                    timeline.get_event(kind, sequence),
                    section_dates,
                    phrases,
                )
                for source, timeline in exports.items()
            ]
            existing = [candidate for candidate in candidates if candidate["exists"]]
            hinted_sources = {hint for hint in line_hints if hint in exports}
            hinted_existing = [
                candidate for candidate in existing if candidate["source"] in hinted_sources
            ]
            if hinted_existing:
                existing = hinted_existing
            date_matches = [candidate for candidate in existing if candidate.get("date_matches_section")]
            quote_matches = [candidate for candidate in existing if candidate.get("quoted_text_matches")]
            if quote_matches:
                status = "verified_by_quote"
            elif len(date_matches) == 1:
                status = "verified_by_date"
            elif not existing:
                status = "missing"
            elif not section_dates:
                status = "exists_only"
            elif not date_matches:
                status = "historical_or_date_mismatch"
            else:
                status = "ambiguous"
            references.append(
                {
                    "line": line_number,
                    "kind": kind,
                    "sequence": sequence,
                    "line_hints": line_hints,
                    "section_dates": sorted(str(date) for date in section_dates),
                    "quote_count": len(phrases),
                    "status": status,
                    "candidates": candidates,
                }
            )

    unique_refs = {(ref["kind"], ref["sequence"]) for ref in references}
    status_counts: Dict[str, int] = {}
    for ref in references:
        status_counts[ref["status"]] = status_counts.get(ref["status"], 0) + 1
    return {
        "document": str(document_path),
        "line_count": len(lines),
        "embedded_image_lines": embedded_image_lines,
        "reference_occurrences": len(references),
        "unique_references": len(unique_refs),
        "status_counts": status_counts,
        "references": references,
    }


def _parse_export_specs(specs: Iterable[str]) -> Dict[str, ExportTimeline]:
    exports: Dict[str, ExportTimeline] = {}
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"Expected LABEL=EXPORT_DIR, got: {spec}")
        label, path = spec.split("=", 1)
        if not label or not path:
            raise ValueError(f"Expected LABEL=EXPORT_DIR, got: {spec}")
        exports[label] = ExportTimeline(Path(path))
    if not exports:
        raise ValueError("At least one --export LABEL=EXPORT_DIR is required")
    return exports


def print_help() -> None:
    print("Usage: python3 verify_witness_doc.py <path_to_export_dir>")
    print("       python3 verify_witness_doc.py --audit-document DOC --export LABEL=DIR [--export LABEL=DIR ...]")
    print("Example: python3 verify_witness_doc.py exports/example-contact")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("export_dir", nargs="?")
    parser.add_argument("--audit-document", type=Path)
    parser.add_argument("--export", action="append", default=[])
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    try:
        if args.audit_document:
            specs = list(args.export)
            if not specs and args.export_dir:
                specs = [f"export={args.export_dir}"]
            report = audit_witness_document(args.audit_document, _parse_export_specs(specs))
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0

        if not args.export_dir:
            print_help()
            return 1
        timeline = ExportTimeline(Path(args.export_dir))
        if args.as_json:
            print(
                json.dumps(
                    {
                        "export_dir": str(timeline.export_dir),
                        "total_events": len(timeline.items),
                        "messages": len(timeline.messages_by_seq),
                        "call_events": len(timeline.calls_by_seq),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"Successfully loaded timeline from {timeline.export_dir}")
            print(f"Total timeline events: {len(timeline.items)}")
            print(f"Messages: {len(timeline.messages_by_seq)}")
            print(f"Call Events: {len(timeline.calls_by_seq)}")
            print("\nTimeline Helper is ready to import as ExportTimeline.")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
