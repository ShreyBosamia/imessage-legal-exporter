#!/usr/bin/env python3
"""Export a macOS Messages chat.db thread as a legal-style evidence package."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import mimetypes
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

try:
    import typedstream
except ImportError:  # pragma: no cover - exercised when dependencies are not installed.
    typedstream = None


APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
ROOT = Path(__file__).resolve().parent


def detect_system_timezone() -> dt.tzinfo:
    configured = os.environ.get("TZ")
    if configured:
        try:
            return ZoneInfo(configured)
        except Exception:
            pass
    try:
        resolved = Path("/etc/localtime").resolve()
        marker = "zoneinfo/"
        if marker in str(resolved):
            return ZoneInfo(str(resolved).split(marker, 1)[1])
    except (OSError, ValueError):
        pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


LOCAL_TZ = detect_system_timezone()


def configure_timezone(name: str | None) -> None:
    global LOCAL_TZ
    LOCAL_TZ = ZoneInfo(name) if name else detect_system_timezone()


@dataclass
class AttachmentRecord:
    source_rowid: int
    guid: str
    original_filename: str | None
    resolved_path: str | None
    export_filename: str | None
    mime_type: str | None
    uti: str | None
    transfer_name: str | None
    total_bytes: int | None
    sha256: str | None = None
    status: str = "missing"
    error: str | None = None
    detected_mime_type: str | None = None
    render_filename: str | None = None
    render_kind: str = "metadata"
    width: int | None = None
    height: int | None = None
    preview_filename: str | None = None
    preview_mime_type: str | None = None
    preview_sha256: str | None = None
    preview_status: str | None = None


@dataclass
class DerivedMediaRecord:
    source: str
    export_filename: str
    mime_type: str
    sha256: str
    bytes: int
    width: int | None = None
    height: int | None = None
    render_kind: str = "image"
    status: str = "extracted"


@dataclass
class MessageRecord:
    sequence: int
    source_rowid: int
    guid: str
    text: str
    body_source: str
    body_status: str
    timestamp_local: str | None
    timestamp_utc: str | None
    timestamp_raw: int | None
    date_edited_local: str | None
    date_edited_utc: str | None
    date_edited_raw: int | None
    direction: str
    service: str | None
    handle: str | None
    is_from_me: int
    is_sent: int
    is_delivered: int
    is_read: int
    error: int
    item_type: int
    balloon_bundle_id: str | None
    payload_data_bytes: int
    payload_data_sha256: str | None
    payload_metadata: list[str]
    share_direction: int | None
    render_kind: str
    associated_message_guid: str | None
    associated_message_type: int
    reply_to_guid: str | None
    thread_originator_guid: str | None = None
    thread_originator_part: str | None = None
    reply_target_sequence: int | None = None
    reply_target_rowid: int | None = None
    reply_target_guid: str | None = None
    reply_target_timestamp_local: str | None = None
    reply_target_direction: str | None = None
    reply_target_excerpt: str | None = None
    reply_context_source: str | None = None
    reaction_target_sequence: int | None = None
    reaction_target_rowid: int | None = None
    reaction_target_guid: str | None = None
    reaction_target_timestamp_local: str | None = None
    reaction_target_direction: str | None = None
    reaction_target_excerpt: str | None = None
    chat_join_status: str = "joined"
    sent_with_siri: bool = False
    reactions: list[MessageRecord] = field(default_factory=list)
    is_nested: bool = False
    attachments: list[AttachmentRecord] = field(default_factory=list)
    derived_media: list[DerivedMediaRecord] = field(default_factory=list)
    edit_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CallEvent:
    sequence: int
    source_rowid: int | None
    unique_id: str | None
    timestamp_local: str | None
    timestamp_utc: str | None
    timestamp_raw: Any
    direction: str
    answered_label: str
    duration_seconds: float | None
    call_type_label: str | None
    source_label: str = "Supplied CallHistory.storedata database"


@dataclass
class CallExportContext:
    source_path: Path
    copied_path: Path
    sha256: str
    events: list[CallEvent]
    source_description: str


def expand_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def sqlite_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path), safe='/')}?mode=ro&immutable=1"


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(sqlite_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


IMAGE_RENDER_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
PREVIEW_RENDER_MIME_TYPES = {"image/png", "image/jpeg"}
FINDMY_MIN_FULL_PREVIEW_WIDTH = 240
FINDMY_MIN_FULL_PREVIEW_HEIGHT = 160


def detect_media_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {
        b"heic",
        b"heix",
        b"hevc",
        b"hevx",
        b"mif1",
        b"msf1",
    }:
        return "image/heic", ".heic"
    return None


def detect_media_file(path: Path) -> tuple[str, str] | None:
    try:
        with path.open("rb") as fh:
            return detect_media_type(fh.read(64))
    except OSError:
        return None


def detect_image_dimensions(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                return None
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(data):
                return None
            segment_length = int.from_bytes(data[index : index + 2], "big")
            if segment_length < 2 or index + segment_length > len(data):
                return None
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if segment_length < 7:
                    return None
                height = int.from_bytes(data[index + 3 : index + 5], "big")
                width = int.from_bytes(data[index + 5 : index + 7], "big")
                return width, height
            index += segment_length
    if len(data) >= 30 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        chunk_type = data[12:16]
        if chunk_type == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        if chunk_type == b"VP8 " and len(data) >= 30:
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
        if chunk_type == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
    return None


def detect_image_dimensions_file(path: Path, read_limit: int = 2 * 1024 * 1024) -> tuple[int, int] | None:
    try:
        with path.open("rb") as fh:
            return detect_image_dimensions(fh.read(read_limit))
    except OSError:
        return None


def findmy_payload_media_kind(width: int | None, height: int | None) -> str:
    if (
        width is not None
        and height is not None
        and (width < FINDMY_MIN_FULL_PREVIEW_WIDTH or height < FINDMY_MIN_FULL_PREVIEW_HEIGHT)
    ):
        return "thumbnail"
    return "image"


def image_extension_for_export(
    current_name: str,
    detected_media: tuple[str, str] | None,
) -> str:
    if not detected_media:
        return current_name
    mime_type, suffix = detected_media
    if mime_type not in IMAGE_RENDER_MIME_TYPES:
        return current_name
    if Path(current_name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return current_name
    return f"{current_name}{suffix}"


def attachment_preview_name(sequence: int, source_rowid: int, suffix: str) -> str:
    return f"msg_{sequence:06d}_att_{source_rowid}_preview{suffix}"


def create_heic_preview(
    source_path: Path,
    sequence: int,
    source_rowid: int,
    derived_media_dir: Path,
) -> tuple[str, str, str, str | None]:
    export_filename = attachment_preview_name(sequence, source_rowid, ".jpg")
    destination = derived_media_dir / export_filename
    try:
        result = subprocess.run(
            [
                "sips",
                "-s",
                "format",
                "jpeg",
                "--resampleHeightWidthMax",
                "1024",
                str(source_path),
                "--out",
                str(destination),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return export_filename, "image/jpeg", "preview_tool_missing", str(exc)
    if result.returncode != 0 or not destination.exists():
        error = (result.stderr or result.stdout or "sips failed").strip()
        return export_filename, "image/jpeg", "preview_error", error
    return export_filename, "image/jpeg", "preview_generated", None


def extract_embedded_media(data: bytes) -> tuple[bytes, str, str] | None:
    candidates = [
        (position, kind)
        for kind, markers in (
            ("png", [b"\x89PNG\r\n\x1a\n"]),
            ("jpeg", [b"\xff\xd8\xff"]),
            ("gif", [b"GIF87a", b"GIF89a"]),
            ("webp", [b"RIFF"]),
            ("heic", [b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1"]),
        )
        for marker in markers
        if (position := data.find(marker)) >= 0
    ]
    if not candidates:
        return None
    start, kind = min(candidates, key=lambda item: item[0])
    if kind == "png":
        end = data.find(b"IEND", start)
        if end < 0:
            return None
        end += len(b"IEND") + 4
        blob = data[start:end]
    elif kind == "jpeg":
        end = data.find(b"\xff\xd9", start + 2)
        if end < 0:
            return None
        blob = data[start : end + 2]
    elif kind == "gif":
        end = data.find(b"\x3b", start + 6)
        if end < 0:
            return None
        blob = data[start : end + 1]
    elif kind == "webp":
        if start + 8 > len(data):
            return None
        size = int.from_bytes(data[start + 4 : start + 8], "little") + 8
        blob = data[start : start + size]
    else:
        box_start = start - 4
        if box_start < 0:
            return None
        size = int.from_bytes(data[box_start:start], "big")
        if size <= 0:
            return None
        blob = data[box_start : box_start + size]

    detected = detect_media_type(blob)
    if not detected:
        return None
    mime_type, suffix = detected
    return blob, mime_type, suffix


def collect_plist_strings(value: Any, limit: int = 12) -> list[str]:
    strings: list[str] = []

    def visit(node: Any) -> None:
        if len(strings) >= limit:
            return
        if isinstance(node, str):
            cleaned = clean_archived_text(node)
            if cleaned and cleaned not in strings:
                strings.append(cleaned)
        elif isinstance(node, dict):
            for key, child in node.items():
                visit(key)
                visit(child)
        elif isinstance(node, list | tuple):
            for child in node:
                visit(child)

    visit(value)
    return strings


def payload_metadata_strings(payload_data: bytes | None) -> list[str]:
    if not payload_data:
        return []
    try:
        parsed = plistlib.loads(payload_data)
    except Exception:
        return []
    rejected = (
        "$",
        "NS",
        "NSMutable",
        "NSDictionary",
        "NSObject",
        "com.apple.messages",
        "com.apple.findmy",
    )
    rejected_exact = {
        "root",
        "ai",
        "ldtext",
        "url",
        "userInfo",
        "pluginPayload",
        "richLinkMetadata",
    }
    return [
        value
        for value in collect_plist_strings(parsed)
        if len(value) > 3
        and value not in rejected_exact
        and not value.startswith(rejected)
        and "bplist00" not in value
    ][:8]


def apple_time_to_datetimes(raw: int | None) -> tuple[str | None, str | None]:
    if raw is None or raw == 0:
        return None, None
    # Modern chat.db stores nanoseconds since 2001-01-01 UTC. Older stores may
    # use seconds; support both so the exporter remains portable.
    seconds = raw / 1_000_000_000 if abs(raw) > 10_000_000_000 else raw
    utc_dt = APPLE_EPOCH + dt.timedelta(seconds=seconds)
    local_dt = utc_dt.astimezone(LOCAL_TZ)
    return local_dt.isoformat(timespec="seconds"), utc_dt.isoformat(timespec="seconds")


def format_human_timestamp(iso_str: str | None) -> str:
    if not iso_str:
        return "unknown time"
    try:
        parsed = dt.datetime.fromisoformat(iso_str)
        day = parsed.day
        hour = parsed.hour
        hour_12 = hour % 12
        if hour_12 == 0:
            hour_12 = 12
        minute_str = parsed.strftime("%M")
        am_pm = parsed.strftime("%p")
        day_of_week = parsed.strftime("%a")
        month = parsed.strftime("%b")
        return f"{day_of_week}, {month} {day} at {hour_12}:{minute_str} {am_pm}"
    except Exception:
        return iso_str


def parse_iso_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TZ)
    return parsed


def call_event_from_dict(row: dict[str, Any], fallback_sequence: int) -> CallEvent:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    source_rowid = row.get("z_pk", raw.get("Z_PK"))
    try:
        source_rowid = int(source_rowid) if source_rowid is not None else None
    except (TypeError, ValueError):
        source_rowid = None
    duration = row.get("duration_seconds", raw.get("ZDURATION"))
    try:
        duration_seconds = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_seconds = None
    sequence = row.get("sequence", fallback_sequence)
    try:
        sequence = int(sequence)
    except (TypeError, ValueError):
        sequence = fallback_sequence
    return CallEvent(
        sequence=sequence,
        source_rowid=source_rowid,
        unique_id=row.get("zunique_id") or raw.get("ZUNIQUE_ID"),
        timestamp_local=row.get("timestamp_local"),
        timestamp_utc=row.get("timestamp_utc"),
        timestamp_raw=row.get("zdate_raw", raw.get("ZDATE")),
        direction=str(row.get("direction") or "unknown"),
        answered_label=str(row.get("answered_label") or "unknown"),
        duration_seconds=duration_seconds,
        call_type_label=row.get("call_type_label"),
        source_label=str(
            row.get("source_label") or "Supplied CallHistory.storedata database"
        ),
    )


def load_call_events(path: Path) -> list[CallEvent]:
    events: list[CallEvent] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid calls JSONL on line {line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Invalid calls JSONL on line {line_number}: expected object")
            events.append(call_event_from_dict(row, len(events) + 1))
    return events


def prepare_call_export(
    calls_jsonl: str | None,
    call_db: str | None,
    chat_summary: dict[str, Any],
    output_dir: Path,
    source_description: str = "Supplied CallHistory.storedata database",
) -> CallExportContext | None:
    if not calls_jsonl and not call_db:
        return None

    if calls_jsonl:
        source_path = expand_path(calls_jsonl)
        events = load_call_events(source_path)
        for event in events:
            event.source_label = source_description
        copied_path = output_dir / "call_records.jsonl"
        shutil.copyfile(source_path, copied_path)
        return CallExportContext(
            source_path=source_path,
            copied_path=copied_path,
            sha256=sha256_file(copied_path),
            events=events,
            source_description=source_description,
        )

    # Process call_db directly
    source_db = expand_path(call_db)
    if not source_db.exists():
        raise FileNotFoundError(source_db)

    # Copy database into output directory's source folder
    source_dir = output_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    copied_db = source_dir / "call_history_copy.db"

    source_hash_before = sha256_file(source_db)
    shutil.copy2(source_db, copied_db)
    source_hash_after = sha256_file(copied_db)
    if source_hash_before != source_hash_after:
        raise RuntimeError("call history database hash changed during copy")

    import call_history_exporter
    call_history_exporter.LOCAL_TZ = LOCAL_TZ
    target_keys: set[str] = set()
    for handle in chat_summary.get("handles", []):
        target_keys.update(call_history_exporter.match_keys(handle))

    if not target_keys:
        return None

    from contextlib import closing
    with closing(call_history_exporter.connect_db(copied_db)) as conn:
        records = call_history_exporter.load_matching_calls(conn, target_keys)

    copied_jsonl = output_dir / "call_records.jsonl"
    with copied_jsonl.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(
                json.dumps(
                    call_history_exporter.record_to_dict(record),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    events = load_call_events(copied_jsonl)
    for event in events:
        event.source_label = source_description
    return CallExportContext(
        source_path=source_db,
        copied_path=copied_jsonl,
        sha256=sha256_file(copied_jsonl),
        events=events,
        source_description=source_description,
    )



def timeline_sort_key(item: MessageRecord | CallEvent) -> tuple[dt.datetime, int, int]:
    parsed = parse_iso_timestamp(item.timestamp_local) or parse_iso_timestamp(item.timestamp_utc)
    if parsed is None:
        parsed = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    item_type_order = 0 if isinstance(item, MessageRecord) else 1
    return parsed.astimezone(dt.timezone.utc), item_type_order, item.sequence


def merge_timeline(
    messages: list[MessageRecord], calls: list[CallEvent] | None = None
) -> list[MessageRecord | CallEvent]:
    return sorted([*messages, *(calls or [])], key=timeline_sort_key)


def mask_handle(value: str | None) -> str:
    if not value:
        return ""
    if "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:2]}***@{domain}"
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 7:
        return f"{value[:3]}***{value[-4:]}"
    return f"{value[:2]}***"


def mask_identifier(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("chat"):
        return value
    if "@" in value or re.sub(r"\D", "", value):
        return mask_handle(value)
    return f"{value[:8]}***" if len(value) > 12 else value


ARCHIVE_TEXT_MARKERS = (b"NSMutableString", b"NSString")
ARCHIVE_METADATA_MARKERS = (
    b"NSDictionary",
    b"__kIMMessagePartAttributeName",
    b"NSNumber",
    b"NSValue",
    b"bplist00",
    b"DDScannerResult",
    b"$archiver",
    b"$objects",
    b"$class",
)
ARCHIVE_REJECTED_STRINGS = {
    "streamtyped",
    "NSString",
    "NSMutableString",
    "NSAttributedString",
    "NSMutableAttributedString",
    "NSDictionary",
    "NSNumber",
    "NSObject",
    "NSColor",
    "NSFont",
    "NSValue",
    "__kIMMessagePartAttributeName",
    "DDScannerResult",
}
FALLBACK_CONTROL_PREFIX_CHARS = "+=()*@"


def clean_archived_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip("\x00\r\n\t ")
    if not cleaned or cleaned in ARCHIVE_REJECTED_STRINGS:
        return None
    if any(marker in cleaned for marker in ("__kIM", "bplist00", "DDScannerResult")):
        return None
    return cleaned


def clean_fallback_archive_fragment(value: str | None) -> str | None:
    cleaned = clean_archived_text(value)
    if cleaned is None:
        return None
    stripped = cleaned.lstrip(FALLBACK_CONTROL_PREFIX_CHARS)
    if stripped != cleaned:
        stripped = stripped.lstrip()
    return clean_archived_text(stripped)


def archived_class_name(value: Any) -> str:
    clazz = getattr(value, "clazz", None)
    name = getattr(clazz, "name", None)
    if isinstance(name, bytes):
        return name.decode("utf-8", errors="ignore")
    if isinstance(name, str):
        return name
    return type(value).__name__


def nsstring_value(value: Any) -> str | None:
    class_name = archived_class_name(value)
    if class_name not in {"NSString", "NSMutableString"}:
        return None
    raw_value = getattr(value, "value", None)
    if isinstance(raw_value, bytes):
        raw_value = raw_value.decode("utf-8", errors="ignore")
    if not isinstance(raw_value, str):
        return None
    return clean_archived_text(raw_value)


def child_values(value: Any) -> list[Any]:
    children: list[Any] = []
    for attr in ("values", "contents"):
        attr_value = getattr(value, attr, None)
        if isinstance(attr_value, list | tuple):
            children.extend(attr_value)
    return children


def first_nsstring_value(value: Any, seen: set[int] | None = None) -> str | None:
    if seen is None:
        seen = set()
    obj_id = id(value)
    if obj_id in seen:
        return None
    seen.add(obj_id)

    text = nsstring_value(value)
    if text is not None:
        return text
    if archived_class_name(value) == "NSDictionary":
        return None
    if isinstance(value, list | tuple):
        iterable = value
    else:
        iterable = child_values(value)
    for child in iterable:
        text = first_nsstring_value(child, seen)
        if text is not None:
            return text
    return None


def decode_attributed_body_with_typedstream(blob: bytes | None) -> str | None:
    if not blob or typedstream is None:
        return None
    try:
        unarchived = typedstream.unarchive_from_data(blob)
    except Exception:
        return None

    class_name = archived_class_name(unarchived)
    if "AttributedString" in class_name:
        text = first_nsstring_value(child_values(unarchived))
        if text is not None:
            return text
    return first_nsstring_value(unarchived)


def printable_archive_fragments(blob: bytes, start: int = 0, stop: int | None = None) -> list[str]:
    """Extract printable fragments without logging private data."""
    if stop is None:
        stop = len(blob)
    data = blob[start:stop]
    fragments: list[str] = []
    current = bytearray()
    for byte in data:
        if byte in (9, 10, 13) or 32 <= byte <= 126 or byte >= 128:
            current.append(byte)
        else:
            if len(current) >= 2:
                text = current.decode("utf-8", errors="ignore")
                cleaned = clean_fallback_archive_fragment(text)
                if cleaned:
                    fragments.append(cleaned)
            current.clear()
    if len(current) >= 2:
        text = current.decode("utf-8", errors="ignore")
        cleaned = clean_fallback_archive_fragment(text)
        if cleaned:
            fragments.append(cleaned)
    return fragments


def bounded_legacy_attributed_text(blob: bytes | None) -> str | None:
    """Last-resort attributedBody text extraction for malformed typedstreams.

    The real message text appears immediately after the archived NSString value.
    Attribute dictionaries and data-detector payloads follow it, so stop before
    known metadata markers instead of joining every printable archive string.
    """
    if not blob:
        return None
    marker_positions = [
        position
        for marker in ARCHIVE_TEXT_MARKERS
        if (position := blob.find(marker)) >= 0
    ]
    start = min(marker_positions) if marker_positions else 0
    metadata_positions = [
        position
        for marker in ARCHIVE_METADATA_MARKERS
        if (position := blob.find(marker, start + 1)) >= 0
    ]
    stop = min(metadata_positions) if metadata_positions else len(blob)
    fragments = printable_archive_fragments(blob, start=start, stop=stop)
    candidates = [
        fragment
        for fragment in fragments
        if not (fragment.startswith("NS") and len(fragment.split()) == 1)
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


def decode_body(text: str | None, attributed_body: bytes | None) -> tuple[str, str, str]:
    if text:
        return text, "message.text", "ok"
    if attributed_body and typedstream is None:
        return "", "message.attributedBody", "undecoded"
    attributed_text = decode_attributed_body_with_typedstream(attributed_body)
    if attributed_text:
        return attributed_text, "message.attributedBody", "attributed"
    legacy_text = bounded_legacy_attributed_text(attributed_body)
    if legacy_text:
        return legacy_text, "message.attributedBody", "fallback"
    if attributed_body:
        return "", "message.attributedBody", "undecoded"
    return "", "none", "empty"


def list_threads(args: argparse.Namespace) -> int:
    if hasattr(args, "timezone"):
        configure_timezone(args.timezone)
    db_path = expand_path(args.db)
    with connect_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
              c.ROWID AS chat_id,
              c.service_name,
              c.style,
              c.display_name,
              c.chat_identifier,
              COUNT(cmj.message_id) AS messages,
              MIN(m.date) AS first_raw,
              MAX(m.date) AS last_raw,
              GROUP_CONCAT(DISTINCT h.id) AS handles
            FROM chat c
            LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            LEFT JOIN message m ON m.ROWID = cmj.message_id
            LEFT JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
            LEFT JOIN handle h ON h.ROWID = chj.handle_id
            GROUP BY c.ROWID
            HAVING messages > 0
            ORDER BY messages DESC, last_raw DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()

    writer = csv.writer(sys.stdout)
    writer.writerow(
        [
            "chat_id",
            "service",
            "style",
            "messages",
            "first_local",
            "last_local",
            "masked_handles",
            "display_name",
            "chat_identifier",
        ]
    )
    for row in rows:
        first_local, _ = apple_time_to_datetimes(row["first_raw"])
        last_local, _ = apple_time_to_datetimes(row["last_raw"])
        handles = ", ".join(mask_handle(h) for h in (row["handles"] or "").split(",") if h)
        writer.writerow(
            [
                row["chat_id"],
                row["service_name"],
                row["style"],
                row["messages"],
                first_local or "",
                last_local or "",
                handles,
                row["display_name"] or "",
                mask_identifier(row["chat_identifier"]),
            ]
        )
    return 0


def load_chat_summary(conn: sqlite3.Connection, chat_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          c.ROWID AS chat_id,
          c.guid,
          c.service_name,
          c.style,
          c.display_name,
          c.chat_identifier,
          c.room_name,
          COUNT(cmj.message_id) AS messages,
          MIN(m.date) AS first_raw,
          MAX(m.date) AS last_raw,
          GROUP_CONCAT(DISTINCT h.id) AS handles
        FROM chat c
        LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        LEFT JOIN message m ON m.ROWID = cmj.message_id
        LEFT JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        LEFT JOIN handle h ON h.ROWID = chj.handle_id
        WHERE c.ROWID = ?
        GROUP BY c.ROWID
        """,
        (chat_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"chat id {chat_id} was not found")
    first_local, first_utc = apple_time_to_datetimes(row["first_raw"])
    last_local, last_utc = apple_time_to_datetimes(row["last_raw"])
    return {
        "chat_id": row["chat_id"],
        "guid": row["guid"],
        "service_name": row["service_name"],
        "style": row["style"],
        "display_name": row["display_name"],
        "chat_identifier": row["chat_identifier"],
        "room_name": row["room_name"],
        "messages": row["messages"],
        "first_local": first_local,
        "first_utc": first_utc,
        "last_local": last_local,
        "last_utc": last_utc,
        "handles": [h for h in (row["handles"] or "").split(",") if h],
    }


def resolve_attachment_path(filename: str | None, attachments_root: Path) -> Path | None:
    if not filename:
        return None
    expanded = Path(filename.replace("~/", f"{Path.home()}/", 1)).expanduser()
    candidates = [expanded]
    if filename.startswith("~/Library/Messages/Attachments/"):
        relative = filename.removeprefix("~/Library/Messages/Attachments/")
        candidates.append(attachments_root / relative)
    if not expanded.is_absolute():
        candidates.append(attachments_root / filename)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return expanded.resolve() if expanded.is_absolute() else None


def attachment_missing_status(path: Path | None) -> str:
    if path and (
        str(path).startswith("/var/folders/") or str(path).startswith("/private/var/folders/")
    ):
        return "missing_temp_cache"
    return "missing"


def safe_attachment_name(sequence: int, attachment: sqlite3.Row, source_path: Path | None) -> str:
    suffix = ""
    if source_path and source_path.suffix:
        suffix = source_path.suffix
    else:
        mime = attachment["mime_type"]
        suffix = mimetypes.guess_extension(mime or "") or ""
    guid_part = re.sub(r"[^A-Za-z0-9_-]+", "_", attachment["guid"] or str(attachment["attachment_id"]))
    return f"msg_{sequence:06d}_att_{attachment['attachment_id']}_{guid_part}{suffix}"


def load_attachments(
    conn: sqlite3.Connection,
    message_id: int,
    sequence: int,
    attachments_root: Path,
    export_attachments_dir: Path,
    derived_media_dir: Path,
) -> list[AttachmentRecord]:
    rows = conn.execute(
        """
        SELECT
          a.ROWID AS attachment_id,
          a.guid,
          a.filename,
          a.uti,
          a.mime_type,
          a.transfer_name,
          a.total_bytes
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE maj.message_id = ?
        ORDER BY a.ROWID
        """,
        (message_id,),
    ).fetchall()
    attachments: list[AttachmentRecord] = []
    for row in rows:
        source_path = resolve_attachment_path(row["filename"], attachments_root)
        detected_media = (
            detect_media_file(source_path)
            if source_path and source_path.exists() and source_path.is_file()
            else None
        )
        export_filename = image_extension_for_export(
            safe_attachment_name(sequence, row, source_path), detected_media
        )
        declared_mime = row["mime_type"]
        render_mime = detected_media[0] if detected_media else declared_mime
        can_render = bool(render_mime in IMAGE_RENDER_MIME_TYPES)
        dimensions = (
            detect_image_dimensions_file(source_path)
            if can_render and source_path and source_path.exists() and source_path.is_file()
            else None
        )
        record = AttachmentRecord(
            source_rowid=row["attachment_id"],
            guid=row["guid"],
            original_filename=row["filename"],
            resolved_path=str(source_path) if source_path else None,
            export_filename=export_filename,
            mime_type=declared_mime,
            uti=row["uti"],
            transfer_name=row["transfer_name"],
            total_bytes=row["total_bytes"],
            detected_mime_type=detected_media[0] if detected_media else None,
            render_filename=export_filename if can_render else None,
            render_kind="image" if can_render else "metadata",
            width=dimensions[0] if dimensions else None,
            height=dimensions[1] if dimensions else None,
        )
        if source_path and source_path.exists() and source_path.is_file():
            destination = export_attachments_dir / export_filename
            try:
                shutil.copy2(source_path, destination)
                record.sha256 = sha256_file(destination)
                record.status = "copied"
                if detected_media and detected_media[0] == "image/heic":
                    (
                        record.preview_filename,
                        record.preview_mime_type,
                        record.preview_status,
                        preview_error,
                    ) = create_heic_preview(
                        source_path, sequence, row["attachment_id"], derived_media_dir
                    )
                    if record.preview_status == "preview_generated" and record.preview_filename:
                        record.preview_sha256 = sha256_file(
                            derived_media_dir / record.preview_filename
                        )
                    elif preview_error:
                        record.error = preview_error
            except PermissionError as exc:
                record.status = "inaccessible"
                record.error = str(exc)
            except OSError as exc:
                record.status = "copy_error"
                record.error = str(exc)
        elif source_path and source_path.exists():
            record.status = "not_a_file"
        else:
            record.status = attachment_missing_status(source_path)
        attachments.append(record)
    return attachments


def message_columns(conn: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in conn.execute("PRAGMA table_info(message)").fetchall()}


def message_column_expr(columns: set[str], name: str, default: str = "NULL") -> str:
    if name in columns:
        return f"m.{name}"
    return default


def is_findmy_balloon(balloon_bundle_id: str | None) -> bool:
    return bool(balloon_bundle_id and "com.apple.findmy.FindMyMessagesApp" in balloon_bundle_id)


def is_location_share_alert(item_type: int | None) -> bool:
    return item_type == 4


def derive_render_kind(
    item_type: int | None,
    balloon_bundle_id: str | None,
    associated_message_guid: str | None = None,
    associated_message_type: int | None = None,
) -> str:
    if is_location_share_alert(item_type):
        return "location_share_alert"
    if is_findmy_balloon(balloon_bundle_id):
        return "findmy_location_card"
    if associated_message_guid and associated_message_type is not None:
        if 2000 <= associated_message_type <= 2006 or 3000 <= associated_message_type <= 3006:
            return "reaction"
    return "message"


def is_unsent_message(message: MessageRecord) -> bool:
    return (
        message.render_kind == "message"
        and message.item_type == 0
        and not message.text
        and message.body_status == "empty"
        and bool(message.date_edited_raw)
        and not message.attachments
        and not message.derived_media
    )


def extract_payload_preview(
    sequence: int,
    source_rowid: int,
    payload_data: bytes | None,
    derived_media_dir: Path,
) -> DerivedMediaRecord | None:
    if not payload_data:
        return None
    extracted = extract_embedded_media(payload_data)
    if not extracted:
        return None
    blob, mime_type, suffix = extracted
    dimensions = detect_image_dimensions(blob)
    width = dimensions[0] if dimensions else None
    height = dimensions[1] if dimensions else None
    export_filename = f"msg_{sequence:06d}_payload_{source_rowid}_preview{suffix}"
    destination = derived_media_dir / export_filename
    destination.write_bytes(blob)
    return DerivedMediaRecord(
        source="message.payload_data",
        export_filename=export_filename,
        mime_type=mime_type,
        sha256=sha256_file(destination),
        bytes=len(blob),
        width=width,
        height=height,
        render_kind=findmy_payload_media_kind(width, height),
    )


def load_messages(
    conn: sqlite3.Connection,
    chat_id: int,
    attachments_root: Path,
    export_attachments_dir: Path,
    derived_media_dir: Path,
) -> list[MessageRecord]:
    columns = message_columns(conn)
    chat_bounds = conn.execute(
        """
        SELECT MIN(m.date) AS first_raw, MAX(m.date) AS last_raw
        FROM chat_message_join cmj
        JOIN message m ON m.ROWID = cmj.message_id
        WHERE cmj.chat_id = ?
        """,
        (chat_id,),
    ).fetchone()
    first_raw = chat_bounds["first_raw"] if chat_bounds else None
    last_raw = chat_bounds["last_raw"] if chat_bounds else None
    rows = conn.execute(
        f"""
        SELECT
          m.ROWID AS message_id,
          m.guid,
          m.text,
          m.attributedBody,
          m.date,
          {message_column_expr(columns, "date_edited", "0")} AS date_edited,
          {message_column_expr(columns, "message_summary_info")} AS message_summary_info,
          m.is_from_me,
          m.is_sent,
          m.is_delivered,
          m.is_read,
          m.error,
          m.item_type,
          m.service,
          m.handle_id,
          {message_column_expr(columns, "balloon_bundle_id")} AS balloon_bundle_id,
          {message_column_expr(columns, "payload_data")} AS payload_data,
          {message_column_expr(columns, "share_direction", "0")} AS share_direction,
          m.associated_message_guid,
          m.associated_message_type,
          m.reply_to_guid,
          {message_column_expr(columns, "thread_originator_guid")} AS thread_originator_guid,
          {message_column_expr(columns, "thread_originator_part")} AS thread_originator_part,
          h.id AS handle,
          'joined' AS chat_join_status
        FROM chat_message_join cmj
        JOIN message m ON m.ROWID = cmj.message_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id = ?
        UNION ALL
        SELECT
          m.ROWID AS message_id,
          m.guid,
          m.text,
          m.attributedBody,
          m.date,
          {message_column_expr(columns, "date_edited", "0")} AS date_edited,
          {message_column_expr(columns, "message_summary_info")} AS message_summary_info,
          m.is_from_me,
          m.is_sent,
          m.is_delivered,
          m.is_read,
          m.error,
          m.item_type,
          m.service,
          m.handle_id,
          {message_column_expr(columns, "balloon_bundle_id")} AS balloon_bundle_id,
          {message_column_expr(columns, "payload_data")} AS payload_data,
          {message_column_expr(columns, "share_direction", "0")} AS share_direction,
          m.associated_message_guid,
          m.associated_message_type,
          m.reply_to_guid,
          {message_column_expr(columns, "thread_originator_guid")} AS thread_originator_guid,
          {message_column_expr(columns, "thread_originator_part")} AS thread_originator_part,
          h.id AS handle,
          'recovered_unjoined_sms' AS chat_join_status
        FROM message m
        LEFT JOIN chat_message_join any_cmj ON any_cmj.message_id = m.ROWID
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE any_cmj.message_id IS NULL
          AND ? IS NOT NULL
          AND ? IS NOT NULL
          AND m.date BETWEEN ? AND ?
          AND m.service = 'SMS'
          AND m.is_from_me = 1
          AND COALESCE(m.handle_id, 0) = 0
        ORDER BY date, message_id
        """,
        (chat_id, first_raw, last_raw, first_raw, last_raw),
    ).fetchall()
    messages: list[MessageRecord] = []
    for index, row in enumerate(rows, 1):
        payload_data = row["payload_data"]
        body, body_source, body_status = decode_body(row["text"], row["attributedBody"])
        local_ts, utc_ts = apple_time_to_datetimes(row["date"])
        date_edited_local, date_edited_utc = apple_time_to_datetimes(row["date_edited"])
        render_kind = derive_render_kind(
            row["item_type"],
            row["balloon_bundle_id"],
            row["associated_message_guid"],
            row["associated_message_type"],
        )
        
        sent_with_siri = False
        edit_history = []
        message_summary_info = row["message_summary_info"] if "message_summary_info" in row.keys() else None
        if message_summary_info:
            try:
                summary_data = plistlib.loads(message_summary_info)
                if summary_data.get("amsa") == "com.apple.siri":
                    sent_with_siri = True
                ec = summary_data.get("ec")
                if ec and "0" in ec:
                    edits = ec["0"]
                    for edit in edits[:-1]:
                        raw_text = edit.get("t")
                        apple_ts = edit.get("d")
                        decoded = decode_attributed_body_with_typedstream(raw_text) if raw_text else None
                        if decoded:
                            cleaned = decoded.replace("\ufffc", "").strip()
                            local_dt, _ = apple_time_to_datetimes(int(apple_ts)) if apple_ts else (None, None)
                            edit_history.append({
                                "text": cleaned,
                                "timestamp_local": local_dt,
                            })
            except Exception:
                pass

        message = MessageRecord(
            sequence=index,
            source_rowid=row["message_id"],
            guid=row["guid"],
            text=body,
            body_source=body_source,
            body_status=body_status,
            timestamp_local=local_ts,
            timestamp_utc=utc_ts,
            timestamp_raw=row["date"],
            date_edited_local=date_edited_local,
            date_edited_utc=date_edited_utc,
            date_edited_raw=row["date_edited"],
            direction="outgoing" if row["is_from_me"] else "incoming",
            service=row["service"],
            handle=row["handle"],
            is_from_me=row["is_from_me"],
            is_sent=row["is_sent"],
            is_delivered=row["is_delivered"],
            is_read=row["is_read"],
            error=row["error"],
            item_type=row["item_type"],
            balloon_bundle_id=row["balloon_bundle_id"],
            payload_data_bytes=len(payload_data) if payload_data else 0,
            payload_data_sha256=sha256_bytes(payload_data) if payload_data else None,
            payload_metadata=payload_metadata_strings(payload_data),
            share_direction=row["share_direction"],
            render_kind=render_kind,
            associated_message_guid=row["associated_message_guid"],
            associated_message_type=row["associated_message_type"],
            reply_to_guid=row["reply_to_guid"],
            thread_originator_guid=row["thread_originator_guid"],
            thread_originator_part=row["thread_originator_part"],
            chat_join_status=row["chat_join_status"],
            sent_with_siri=sent_with_siri,
            edit_history=edit_history,
        )
        message.attachments = load_attachments(
            conn,
            row["message_id"],
            index,
            attachments_root,
            export_attachments_dir,
            derived_media_dir,
        )
        if render_kind == "findmy_location_card":
            preview = extract_payload_preview(index, row["message_id"], payload_data, derived_media_dir)
            if preview:
                message.derived_media.append(preview)
        if is_unsent_message(message):
            message.render_kind = "unsent_message"
        messages.append(message)
    annotate_reply_context(messages)
    return messages


def reply_target_excerpt(message: MessageRecord, limit: int = 160) -> str:
    if message.render_kind == "unsent_message":
        return "[Unsent message]"
    if message.render_kind == "findmy_location_card":
        return "[Location Share via Find My]"
    text = " ".join(message.text.split())
    if text:
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."
    if message.attachments or message.derived_media:
        return "[Attachment message]"
    if message.render_kind == "location_share_alert":
        return "[Location Share via Find My]"
    return "[No text body]"


def set_reply_context(message: MessageRecord, target: MessageRecord, source: str) -> None:
    message.reply_target_sequence = target.sequence
    message.reply_target_rowid = target.source_rowid
    message.reply_target_guid = target.guid
    message.reply_target_timestamp_local = target.timestamp_local
    message.reply_target_direction = target.direction
    message.reply_target_excerpt = reply_target_excerpt(target)
    message.reply_context_source = source


def clean_associated_guid(guid: str | None) -> str | None:
    if not guid:
        return None
    if "/" in guid:
        guid = guid.split("/")[-1]
    if guid.startswith("bp:"):
        guid = guid[3:]
    elif guid.startswith("p:"):
        guid = guid[2:]
    return guid


def annotate_reply_context(messages: list[MessageRecord]) -> None:
    by_guid = {message.guid: message for message in messages if message.guid}
    for message in messages:
        message.reply_target_sequence = None
        message.reply_target_rowid = None
        message.reply_target_guid = None
        message.reply_target_timestamp_local = None
        message.reply_target_direction = None
        message.reply_target_excerpt = None
        message.reply_context_source = None

        message.reaction_target_sequence = None
        message.reaction_target_rowid = None
        message.reaction_target_guid = None
        message.reaction_target_timestamp_local = None
        message.reaction_target_direction = None
        message.reaction_target_excerpt = None
        message.reactions = []
        message.is_nested = False

        if message.thread_originator_guid and message.thread_originator_guid in by_guid:
            target = by_guid[message.thread_originator_guid]
            if target.sequence < message.sequence:
                set_reply_context(
                    message, target, "thread_originator_guid"
                )

        if message.render_kind == "reaction" and message.associated_message_guid:
            clean_guid = clean_associated_guid(message.associated_message_guid)
            if clean_guid in by_guid:
                target = by_guid[clean_guid]
                message.reaction_target_sequence = target.sequence
                message.reaction_target_rowid = target.source_rowid
                message.reaction_target_guid = target.guid
                message.reaction_target_timestamp_local = target.timestamp_local
                message.reaction_target_direction = target.direction
                message.reaction_target_excerpt = reply_target_excerpt(target)
                
                target.reactions.append(message)
                message.is_nested = True


def missing_reply_reference_count(messages: list[MessageRecord]) -> int:
    exported_guids = {message.guid for message in messages if message.guid}
    missing = 0
    for message in messages:
        if message.thread_originator_guid and message.thread_originator_guid not in exported_guids:
            missing += 1
    return missing


def message_to_dict(message: MessageRecord) -> dict[str, Any]:
    data = {
        "sequence": message.sequence,
        "source_rowid": message.source_rowid,
        "guid": message.guid,
        "timestamp_local": message.timestamp_local,
        "timestamp_utc": message.timestamp_utc,
        "timestamp_raw": message.timestamp_raw,
        "date_edited_local": message.date_edited_local,
        "date_edited_utc": message.date_edited_utc,
        "date_edited_raw": message.date_edited_raw,
        "direction": message.direction,
        "service": message.service,
        "handle": message.handle,
        "text": message.text,
        "body_source": message.body_source,
        "body_status": message.body_status,
        "is_from_me": message.is_from_me,
        "is_sent": message.is_sent,
        "is_delivered": message.is_delivered,
        "is_read": message.is_read,
        "error": message.error,
        "item_type": message.item_type,
        "balloon_bundle_id": message.balloon_bundle_id,
        "payload_data_bytes": message.payload_data_bytes,
        "payload_data_sha256": message.payload_data_sha256,
        "payload_metadata": message.payload_metadata,
        "share_direction": message.share_direction,
        "render_kind": message.render_kind,
        "associated_message_guid": message.associated_message_guid,
        "associated_message_type": message.associated_message_type,
        "reply_to_guid": message.reply_to_guid,
        "thread_originator_guid": message.thread_originator_guid,
        "thread_originator_part": message.thread_originator_part,
        "reply_target_sequence": message.reply_target_sequence,
        "reply_target_rowid": message.reply_target_rowid,
        "reply_target_guid": message.reply_target_guid,
        "reply_target_timestamp_local": message.reply_target_timestamp_local,
        "reply_target_direction": message.reply_target_direction,
        "reply_target_excerpt": message.reply_target_excerpt,
        "reply_context_source": message.reply_context_source,
        "reaction_target_sequence": message.reaction_target_sequence,
        "reaction_target_rowid": message.reaction_target_rowid,
        "reaction_target_guid": message.reaction_target_guid,
        "reaction_target_timestamp_local": message.reaction_target_timestamp_local,
        "reaction_target_direction": message.reaction_target_direction,
        "reaction_target_excerpt": message.reaction_target_excerpt,
        "chat_join_status": message.chat_join_status,
        "sent_with_siri": message.sent_with_siri,
        "is_nested": message.is_nested,
        "edit_history": message.edit_history,
        "attachments": [attachment.__dict__ for attachment in message.attachments],
        "derived_media": [media.__dict__ for media in message.derived_media],
    }
    data.update(plugin_payload_group_data(message))
    return data


def write_jsonl(path: Path, messages: list[MessageRecord]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for message in messages:
            fh.write(json.dumps(message_to_dict(message), ensure_ascii=False, sort_keys=True))
            fh.write("\n")


def write_csv(path: Path, messages: list[MessageRecord]) -> None:
    fields = [
        "sequence",
        "source_rowid",
        "guid",
        "timestamp_local",
        "timestamp_utc",
        "date_edited_local",
        "date_edited_utc",
        "date_edited_raw",
        "direction",
        "service",
        "handle",
        "text",
        "body_source",
        "body_status",
        "is_from_me",
        "is_sent",
        "is_delivered",
        "is_read",
        "error",
        "item_type",
        "balloon_bundle_id",
        "payload_data_bytes",
        "payload_data_sha256",
        "payload_metadata",
        "share_direction",
        "render_kind",
        "associated_message_guid",
        "associated_message_type",
        "reply_to_guid",
        "thread_originator_guid",
        "thread_originator_part",
        "reply_target_sequence",
        "reply_target_rowid",
        "reply_target_guid",
        "reply_target_timestamp_local",
        "reply_target_direction",
        "reply_target_excerpt",
        "reply_context_source",
        "reaction_target_sequence",
        "reaction_target_rowid",
        "reaction_target_guid",
        "reaction_target_timestamp_local",
        "reaction_target_direction",
        "reaction_target_excerpt",
        "chat_join_status",
        "sent_with_siri",
        "is_nested",
        "edit_history_json",
        "edit_history_verbatims",
        "edit_history_timestamps",
        "attachment_count",
        "attachment_rows",
        "attachment_export_files",
        "attachment_original_files",
        "attachment_types",
        "attachment_bytes",
        "attachment_sha256",
        "attachment_statuses",
        "attachment_widths",
        "attachment_heights",
        "attachment_dimensions",
        "attachment_preview_files",
        "attachment_preview_sha256",
        "attachment_preview_statuses",
        "plugin_payload_group_count",
        "plugin_payload_rows",
        "plugin_payload_selected_row",
        "derived_media_files",
        "derived_media_sha256",
        "derived_media_dimensions",
        "derived_media_render_kinds",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for message in messages:
            data = message_to_dict(message)
            data["attachment_count"] = len(message.attachments)
            data["attachment_rows"] = ";".join(str(a.source_rowid) for a in message.attachments)
            data["attachment_export_files"] = ";".join(
                a.export_filename or "" for a in message.attachments
            )
            data["attachment_original_files"] = ";".join(
                a.original_filename or "" for a in message.attachments
            )
            data["attachment_types"] = ";".join(
                a.detected_mime_type or a.mime_type or a.uti or "" for a in message.attachments
            )
            data["attachment_bytes"] = ";".join(
                str(a.total_bytes) if a.total_bytes is not None else "" for a in message.attachments
            )
            data["attachment_sha256"] = ";".join(a.sha256 or "" for a in message.attachments)
            data["attachment_statuses"] = ";".join(a.status for a in message.attachments)
            data["attachment_widths"] = ";".join(
                str(a.width) if a.width is not None else "" for a in message.attachments
            )
            data["attachment_heights"] = ";".join(
                str(a.height) if a.height is not None else "" for a in message.attachments
            )
            data["attachment_dimensions"] = ";".join(
                f"{a.width}x{a.height}" if a.width and a.height else ""
                for a in message.attachments
            )
            data["attachment_preview_files"] = ";".join(
                a.preview_filename or "" for a in message.attachments
            )
            data["attachment_preview_sha256"] = ";".join(
                a.preview_sha256 or "" for a in message.attachments
            )
            data["attachment_preview_statuses"] = ";".join(
                a.preview_status or "" for a in message.attachments
            )
            data["derived_media_files"] = ";".join(
                media.export_filename for media in message.derived_media
            )
            data["derived_media_sha256"] = ";".join(media.sha256 for media in message.derived_media)
            data["derived_media_dimensions"] = ";".join(
                f"{media.width}x{media.height}" if media.width and media.height else ""
                for media in message.derived_media
            )
            data["derived_media_render_kinds"] = ";".join(
                media.render_kind for media in message.derived_media
            )
            data["edit_history_json"] = (
                json.dumps(message.edit_history, ensure_ascii=False, sort_keys=True)
                if message.edit_history
                else ""
            )
            data["edit_history_verbatims"] = "\n---\n".join(
                (edit.get("text") or "").replace("\ufffc", "").strip()
                for edit in message.edit_history
            )
            data["edit_history_timestamps"] = ";".join(
                edit.get("timestamp_local") or "" for edit in message.edit_history
            )
            data["payload_metadata"] = ";".join(message.payload_metadata)
            data["plugin_payload_rows"] = ";".join(str(rowid) for rowid in data["plugin_payload_rows"])
            writer.writerow({field: data.get(field, "") for field in fields})


def write_timeline_jsonl(
    path: Path, messages: list[MessageRecord], calls: list[CallEvent] | None
) -> None:
    items = merge_timeline(messages, calls)
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            if isinstance(item, CallEvent):
                data = {
                    "type": "call",
                    "sequence": item.sequence,
                    "source_rowid": item.source_rowid,
                    "guid": item.unique_id,
                    "timestamp_local": item.timestamp_local,
                    "timestamp_utc": item.timestamp_utc,
                    "timestamp_raw": item.timestamp_raw,
                    "direction": item.direction,
                    "answered_label": item.answered_label,
                    "duration_seconds": item.duration_seconds,
                    "call_type_label": item.call_type_label,
                    "source_label": item.source_label,
                }
            else:
                data = message_to_dict(item)
                data["type"] = "message"
            fh.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")


def css() -> str:
    return """
@page {
  size: Letter;
  margin: 0;
}
* {
  box-sizing: border-box;
}
::-webkit-scrollbar {
  display: none !important;
}
html, body {
  scrollbar-width: none;
  -ms-overflow-style: none;
  overflow-x: hidden;
}
body {
  margin: 0;
  color: #111827;
  background: #f3f4f6;
  font: 12px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
@media print {
  body {
    background: white !important;
    background-color: white !important;
  }
  .page {
    margin: 0 !important;
    border: none !important;
    box-shadow: none !important;
    width: 8.5in !important;
    height: 11in !important;
  }
}
.page {
  width: 8.5in;
  min-height: 11in;
  break-after: page;
  background: white;
  padding: 0.48in 0.5in 0.5in;
  position: relative;
}
.page:last-child {
  break-after: auto;
}
.header {
  border-bottom: 1px solid #d1d5db;
  padding-bottom: 8px;
  margin-bottom: 10px;
  position: relative;
}
.header-year {
  position: absolute;
  top: 0;
  right: 0;
  font-weight: 700;
  font-size: 15px;
  color: #111111;
}
.year-divider {
  text-align: center;
  margin: 20px 0 10px;
  clear: both;
}
.year-divider-text {
  font-weight: 700;
  font-size: 13px;
  color: #111111;
}
.year-divider-line {
  color: #a1a8b3;
  font-size: 10px;
  margin-top: 2px;
}
.header.compact-header {
  padding-bottom: 4px;
  margin-bottom: 6px;
}
.title {
  font-weight: 700;
  font-size: 15px;
}
.meta-grid {
  display: grid;
  grid-template-columns: 1.15in 1fr;
  gap: 2px 10px;
  margin-top: 6px;
  color: #374151;
}
.label {
  color: #6b7280;
}
.message {
  break-inside: avoid;
  page-break-inside: avoid;
  margin: 9px 0 12px;
  clear: both;
}
.message.outgoing {
  margin-left: 1.15in;
}
.message.incoming {
  margin-right: 1.15in;
}
.message-meta {
  margin-bottom: 4px;
}
.meta-primary {
  color: #374151;
  font-size: 10.5px;
  font-weight: 600;
}
.meta-secondary {
  color: #6b7280;
  font-size: 9px;
  margin-top: 1px;
}
.meta-guid {
  font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
}
.continuation-note {
  color: #6b7280;
  font-size: 10px;
  font-style: italic;
  margin-bottom: 3px;
}
.bubble {
  border: 1px solid #d1d5db;
  border-radius: 8px;
  padding: 8px 9px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: #f9fafb;
}
.outgoing .bubble {
  background: #eef6ff;
  border-color: #b9d6f2;
}
.message.reaction .bubble {
  border-style: dashed;
  border-width: 1.5px;
  color: #374151;
}
.message.reaction.incoming .bubble {
  background: #f9fafb;
  border-color: #c4c7c5;
}
.message.reaction.outgoing .bubble {
  background: #f5f9ff;
  border-color: #a9ccf0;
}
.reaction-tag {
  display: block;
  font-size: 8.5px;
  font-weight: 700;
  color: #6b7280;
  text-transform: uppercase;
  margin-bottom: 4px;
  letter-spacing: 0.5px;
}
.reaction-action {
  color: #4f46e5;
  font-weight: 700;
}
.reactions-container {
  margin-top: 6px;
  clear: both;
  margin-left: 8px;
  text-align: left;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}
.reaction-row {
  margin-top: 3px;
  font-size: 10px;
  color: #4b5563;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.reaction-tree-icon {
  color: #9ca3af;
  font-family: monospace;
  margin-right: 4px;
  font-size: 11px;
}
.reaction-badge {
  background: #f3f4f6;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
  padding: 1px 5px;
  font-weight: 700;
  color: #111827;
}
.outgoing .reaction-badge {
  background: #eef6ff;
  border-color: #cbdcfc;
}
.reaction-meta {
  color: #6b7280;
  font-size: 9px;
}
.reply-context {
  border-left: 3px solid #9ca3af;
  background: rgba(255, 255, 255, 0.65);
  padding: 6px 8px;
  margin-bottom: 7px;
  color: #374151;
  font-size: 10.5px;
  white-space: normal;
  overflow-wrap: anywhere;
}
.reply-context-meta {
  font-weight: 700;
  color: #1f2937;
}
.reply-context-excerpt {
  margin-top: 3px;
  color: #4b5563;
}
.message.system-alert {
  margin: 12px 0;
  text-align: center;
  clear: both;
}
.message.system-alert .message-meta {
  margin-bottom: 5px;
}
.system-alert-text {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 5.7in;
  color: #4b5563;
  background: #f3f4f6;
  border: 1px solid #e5e7eb;
  border-radius: 999px;
  padding: 5px 11px;
  font-size: 11px;
}
.location-icon {
  color: #6b7280;
  font-size: 12px;
}
.message.call-event {
  margin: 15px 0;
  text-align: center;
  clear: both;
}
.message.call-event + .message.call-event {
  margin-top: -5px;
}
.call-card {
  display: inline-block;
  width: min(4.85in, 100%);
  background: #ffffff;
  border: 1px solid #dfe4ea;
  border-left: 4px solid #8a949f;
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
  overflow: hidden;
  text-align: left;
  white-space: normal;
}
.call-card.call-status-answered {
  border-left-color: #1f8a57;
}
.call-card.call-status-missed,
.call-card.call-status-unanswered {
  border-left-color: #c6413d;
}
.call-card.call-status-canceled {
  border-left-color: #a66a10;
}
.call-title {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  flex-wrap: nowrap;
  gap: 5px;
  padding: 10px 14px 0;
  font-size: 12.5px;
  font-weight: 650;
  text-align: left;
  white-space: nowrap;
}
.call-title-separator {
  color: #8a949f;
  margin: 0 2px;
}
.call-status-word-answered {
  color: #1f8a57;
}
.call-status-word-missed,
.call-status-word-unanswered {
  color: #c6413d;
}
.call-status-word-canceled {
  color: #a66a10;
}
.call-status-word-unknown {
  color: #7b8490;
}
.call-state-icon {
  flex: 0 0 auto;
  width: 18px !important;
  height: 14px !important;
}
.call-state-icon .call-phone {
  fill: #111111;
}
.call-state-icon-green {
  color: #1f8a57;
}
.call-state-icon-blue {
  color: #2f6f8f;
}
.call-state-icon-red {
  color: #c6413d;
}
.call-state-icon-amber {
  color: #a66a10;
}
.call-state-icon-muted {
  color: #7b8490;
}
.call-duration {
  margin: 4px 14px 0;
  font-size: 11px;
  text-align: left;
}
.call-duration-label {
  color: #4b5563;
  font-weight: 400;
}
.call-duration-value {
  color: #111827;
  font-weight: 600;
}
.call-footer {
  margin-top: 10px;
  border-top: 1px solid #dfe4ea;
  background: #f8f9fa;
  color: #68717d;
  font-size: 8.6px;
  line-height: 1.2;
  padding: 6px 10px;
  text-align: left;
  white-space: nowrap;
}
.location-card {
  overflow: hidden;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  background: #ffffff;
}
.location-card-image {
  display: block;
  max-width: 100%;
  height: auto;
  max-height: 2.7in;
  object-fit: contain;
  border-bottom: 1px solid #d1d5db;
  margin: 0 auto;
}
.location-card-thumbnail-frame {
  padding: 8px 9px 0;
}
.location-card-thumbnail {
  display: block;
  width: auto;
  max-width: 1.4in;
  max-height: 1.1in;
  object-fit: contain;
  border: 1px solid #d1d5db;
  background: #f9fafb;
}
.location-card-body {
  padding: 8px 9px;
  white-space: normal;
}
.location-card-title {
  font-weight: 700;
  color: #111827;
}
.location-card-caption {
  margin-top: 2px;
  color: #4b5563;
  font-size: 10.5px;
  overflow-wrap: anywhere;
}
.attachment {
  margin-top: 7px;
}
.forensic-attachment-card {
  overflow: hidden;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  background: #ffffff;
  white-space: normal;
}
.forensic-attachment-title {
  padding: 5px 7px 3px;
  color: #374151;
  font-size: 9px;
  line-height: 1.2;
  font-weight: 700;
  text-transform: uppercase;
}
.forensic-attachment-card img {
  max-width: 100%;
  max-height: 2.6in;
  object-fit: contain;
  display: block;
  border-top: 1px solid #f3f4f6;
  border-bottom: 1px solid #e5e7eb;
}
.forensic-attachment-card.screenshot-large img {
  max-height: 5.0in;
}
.forensic-attachment-card.small img {
  max-height: 1.2in;
}
.forensic-attachment-card.small .attachment-metadata th,
.forensic-attachment-card.small .attachment-metadata td {
  padding: 1.5px 3px;
  font-size: 8px;
}

.plugin-url-preview {
  margin-top: 7px;
  border-top: 1px solid #e5e7eb;
  padding-top: 6px;
  white-space: normal;
}
.plugin-url-preview-card {
  overflow: hidden;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  background: #ffffff;
}
.plugin-url-preview-label {
  padding: 6px 8px 4px;
  color: #6b7280;
  font-size: 9.5px;
  font-weight: 700;
  text-transform: uppercase;
}
.plugin-url-preview-image {
  display: block;
  max-width: 100%;
  height: auto;
  max-height: 2.2in;
  object-fit: contain;
  border-top: 1px solid #f3f4f6;
  border-bottom: 1px solid #e5e7eb;
  margin: 0 auto;
}
.plugin-url-preview-body {
  padding: 7px 8px 8px;
}
.plugin-url-preview-domain {
  font-weight: 700;
  color: #111827;
  overflow-wrap: anywhere;
}
.plugin-url-preview-url {
  margin-top: 2px;
  color: #4b5563;
  font-size: 10px;
  overflow-wrap: anywhere;
}
.plugin-url-preview-note {
  margin-top: 5px;
  color: #6b7280;
  font-size: 9.5px;
  overflow-wrap: anywhere;
}
.message-fragment {
  white-space: pre-wrap;
}
.attachment-caption {
  padding: 5px 7px 6px;
  color: #374151;
  font-size: 9px;
  line-height: 1.22;
  overflow-wrap: anywhere;
}
.attachment-metadata {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}
.attachment-metadata th {
  width: 1.22in;
  padding: 1px 7px 1px 0;
  color: #6b7280;
  font-weight: 700;
  text-align: left;
  vertical-align: top;
  white-space: nowrap;
}
.attachment-metadata td {
  padding: 1px 0;
  vertical-align: top;
  overflow-wrap: anywhere;
}

.status {
  color: #92400e;
}
.footer {
  position: absolute;
  bottom: 0.32in;
  left: 0.55in;
  right: 0.55in;
  border-top: 1px solid #d1d5db;
  padding-top: 5px;
  color: #6b7280;
  font-size: 10px;
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
}
.footer .page-number {
  white-space: nowrap;
  flex-shrink: 0;
  margin-left: 20px;
}
.siri-note {
  color: #6b7280;
  font-size: 9px;
  font-style: italic;
  margin-top: 3px;
  margin-bottom: 3px;
  clear: both;
}
.outgoing .siri-note {
  text-align: right;
}
.incoming .siri-note {
  text-align: left;
}
.edited-label {
  margin-top: 6px;
  padding-right: 9px;
  font-size: 8.5px;
  color: #3382d8;
  font-weight: 700;
  text-shadow: 0 0 1px #e6fdff;
  text-align: right;
  display: block;
  white-space: normal;
  overflow-wrap: anywhere;
}
.status-edited {
  color: #3b82f6;
  font-weight: 600;
}
.diff-del {
  color: #ef4444;
  text-decoration: line-through;
  background-color: #fee2e2;
  padding: 0 1px;
}
.diff-ins {
  color: #10b981;
  text-decoration: underline;
  font-weight: 600;
  background-color: #d1fae5;
  padding: 0 1px;
}
"""


def html_escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def word_diff_html(a: str, b: str) -> str:
    from difflib import SequenceMatcher
    a_tokens = re.findall(r'\w+|\W+', a) if a else []
    b_tokens = re.findall(r'\w+|\W+', b) if b else []
    matcher = SequenceMatcher(None, a_tokens, b_tokens)
    output = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for token in a_tokens[i1:i2]:
                output.append(html_escape(token))
        elif tag == 'replace':
            del_text = "".join(a_tokens[i1:i2])
            ins_text = "".join(b_tokens[j1:j2])
            if del_text and ins_text:
                output.append(f'<del class="diff-del">{html_escape(del_text)}</del> → <ins class="diff-ins">{html_escape(ins_text)}</ins>')
            elif del_text:
                output.append(f'<del class="diff-del">{html_escape(del_text)}</del>')
            elif ins_text:
                output.append(f'<ins class="diff-ins">{html_escape(ins_text)}</ins>')
        elif tag == 'delete':
            del_text = "".join(a_tokens[i1:i2])
            if del_text:
                output.append(f'<del class="diff-del">{html_escape(del_text)}</del>')
        elif tag == 'insert':
            ins_text = "".join(b_tokens[j1:j2])
            if ins_text:
                output.append(f'<ins class="diff-ins">{html_escape(ins_text)}</ins>')
    return "".join(output)



def is_plugin_payload_attachment(attachment: AttachmentRecord) -> bool:
    values = (
        attachment.original_filename,
        attachment.export_filename,
        attachment.transfer_name,
    )
    return any("pluginPayloadAttachment" in (value or "") for value in values)


def is_renderable_image_attachment(attachment: AttachmentRecord) -> bool:
    mime = attachment.detected_mime_type or attachment.mime_type or ""
    return (
        attachment.status == "copied"
        and bool(attachment.render_filename)
        and mime in IMAGE_RENDER_MIME_TYPES
    )


def plugin_payload_url_preview_attachments(
    attachments: list[AttachmentRecord],
) -> list[AttachmentRecord]:
    candidates = [
        attachment
        for attachment in attachments
        if is_plugin_payload_attachment(attachment) and is_renderable_image_attachment(attachment)
    ]
    return candidates if len(candidates) >= 2 else []


def grouped_plugin_payload_attachments(
    attachments: list[AttachmentRecord],
) -> tuple[list[AttachmentRecord], list[AttachmentRecord]]:
    grouped = plugin_payload_url_preview_attachments(attachments)
    if not grouped:
        return [], attachments
    grouped_ids = {attachment.source_rowid for attachment in grouped}
    remaining = [attachment for attachment in attachments if attachment.source_rowid not in grouped_ids]
    return grouped, remaining


def is_icon_like_plugin_asset(attachment: AttachmentRecord) -> bool:
    if attachment.width is None or attachment.height is None:
        return False
    return attachment.width <= 128 and attachment.height <= 128


def plugin_payload_preview_fallback_attachment(
    attachments: list[AttachmentRecord],
) -> AttachmentRecord | None:
    if not attachments:
        return None
    with_dimensions = [
        attachment
        for attachment in attachments
        if attachment.width is not None
        and attachment.height is not None
        and attachment.width > 0
        and attachment.height > 0
    ]
    if not with_dimensions:
        return sorted(attachments, key=lambda attachment: attachment.source_rowid)[0]
    return max(
        with_dimensions,
        key=lambda attachment: (
            (attachment.width or 0) * (attachment.height or 0),
            attachment.total_bytes if attachment.total_bytes is not None else -1,
            -attachment.source_rowid,
        ),
    )


def select_plugin_payload_preview_attachment(
    attachments: list[AttachmentRecord],
) -> AttachmentRecord | None:
    if not attachments:
        return None
    renderable = [
        attachment
        for attachment in attachments
        if is_renderable_image_attachment(attachment)
    ]
    if not renderable:
        return plugin_payload_preview_fallback_attachment(attachments)
    dimensioned = [
        attachment
        for attachment in renderable
        if attachment.width is not None
        and attachment.height is not None
        and attachment.width > 0
        and attachment.height > 0
    ]
    if not dimensioned:
        return plugin_payload_preview_fallback_attachment(renderable)
    has_larger_asset = any(
        (attachment.width or 0) > 128 or (attachment.height or 0) > 128
        for attachment in dimensioned
    )
    row_order_candidates = [
        attachment
        for attachment in sorted(dimensioned, key=lambda item: item.source_rowid)
        if not (has_larger_asset and is_icon_like_plugin_asset(attachment))
    ]
    if row_order_candidates:
        return row_order_candidates[0]
    return plugin_payload_preview_fallback_attachment(renderable)


def plugin_payload_group_data(message: MessageRecord) -> dict[str, Any]:
    grouped = plugin_payload_url_preview_attachments(message.attachments)
    selected = select_plugin_payload_preview_attachment(grouped)
    return {
        "plugin_payload_group_count": len(grouped),
        "plugin_payload_rows": [attachment.source_rowid for attachment in grouped],
        "plugin_payload_selected_row": selected.source_rowid if selected else None,
    }


def compact_row_ranges(rowids: list[int]) -> str:
    if not rowids:
        return "none"
    sorted_rows = sorted(rowids)
    ranges: list[str] = []
    start = previous = sorted_rows[0]
    for rowid in sorted_rows[1:]:
        if rowid == previous + 1:
            previous = rowid
            continue
        ranges.append(f"{start}-{previous}" if start != previous else str(start))
        start = previous = rowid
    ranges.append(f"{start}-{previous}" if start != previous else str(start))
    return ", ".join(ranges)


def first_url_text(value: str) -> str | None:
    match = re.search(r"https?://[^\s<>'\"]+", value or "")
    if not match:
        return None
    return match.group(0).rstrip(".,);]")


def display_domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.netloc.removeprefix("www.") or None


def attachment_dimensions_text(attachment: AttachmentRecord) -> str:
    if attachment.width is not None and attachment.height is not None:
        return f"{attachment.width}x{attachment.height}"
    return "unknown"


def attachment_media_type(attachment: AttachmentRecord) -> str:
    return attachment.detected_mime_type or attachment.mime_type or attachment.uti or "unknown"


def attachment_metadata_rows(attachment: AttachmentRecord) -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = [
        ("Attachment row ID", attachment.source_rowid),
        ("Exported filename", attachment.export_filename or "not exported"),
        ("Original filename", attachment.original_filename or "unknown"),
        ("SHA-256", attachment.sha256 or "not available"),
        ("Bytes", str(attachment.total_bytes) if attachment.total_bytes is not None else "unknown"),
        ("Media type", attachment_media_type(attachment)),
        ("Dimensions", attachment_dimensions_text(attachment)),
        ("Status", attachment.status),
    ]
    if (
        attachment.status == "copied"
        and attachment.preview_filename
        and attachment.preview_mime_type in PREVIEW_RENDER_MIME_TYPES
        and attachment.preview_status == "preview_generated"
    ):
        rows.extend(
            [
                ("Preview filename", attachment.preview_filename),
                ("Preview SHA-256", attachment.preview_sha256 or "not available"),
                ("Preview status", attachment.preview_status or "unknown"),
            ]
        )
    elif attachment.preview_status:
        rows.append(("Preview status", attachment.preview_status))
    return rows


def attachment_metadata_table_html(attachment: AttachmentRecord) -> str:
    rows = "".join(
        "<tr>"
        f"<th>{html_escape(label)}</th>"
        f"<td>{html_escape(value)}</td>"
        "</tr>"
        for label, value in attachment_metadata_rows(attachment)
    )
    return f'<table class="attachment-metadata"><tbody>{rows}</tbody></table>'


def ordinary_image_exhibit_attachment(attachment: AttachmentRecord) -> bool:
    return is_renderable_image_attachment(attachment) and not is_plugin_payload_attachment(attachment)


def attachment_exhibit_label(message: MessageRecord, exhibit_index: int) -> str:
    return f"Attachment Exhibit {message.sequence}-{exhibit_index}"


def attachment_exhibit_dom_id(message: MessageRecord, exhibit_index: int) -> str:
    return f"attachment-exhibit-{message.sequence}-{exhibit_index}"


def attachment_html(
    attachment: AttachmentRecord,
) -> str:
    media_type = attachment.detected_mime_type or attachment.mime_type or attachment.uti or "unknown"
    image_html = ""
    if attachment.status == "copied" and attachment.render_filename:
        if media_type in IMAGE_RENDER_MIME_TYPES:
            src = f"attachments/{quote(attachment.render_filename)}"
            image_html = f'<img src="{src}" alt="Attachment {html_escape(attachment.source_rowid)}">'
    elif (
        attachment.status == "copied"
        and attachment.preview_filename
        and attachment.preview_mime_type in PREVIEW_RENDER_MIME_TYPES
        and attachment.preview_status == "preview_generated"
    ):
        src = f"derived_media/{quote(attachment.preview_filename)}"
        image_html = f'<img src="{src}" alt="Attachment preview {html_escape(attachment.source_rowid)}">'
    card_class = "attachment forensic-attachment-card"
    if (
        attachment.width
        and attachment.height
        and attachment.height >= 1200
        and attachment.height / attachment.width >= 1.75
    ):
        card_class += " screenshot-large"
    return (
        f'<div class="{card_class}">'
        '<div class="forensic-attachment-title">Forensic Attachment</div>'
        f"{image_html}"
        '<div class="attachment-caption">'
        f"{attachment_metadata_table_html(attachment)}"
        "</div>"
        "</div>"
    )


def attachment_exhibit_html(
    message: MessageRecord,
    attachment: AttachmentRecord,
    exhibit_index: int,
    exhibit_total: int,
) -> str:
    label = attachment_exhibit_label(message, exhibit_index)
    dom_id = attachment_exhibit_dom_id(message, exhibit_index)
    src = f"attachments/{quote(attachment.render_filename or '')}"
    part_text = f"attachment exhibit {exhibit_index} of {exhibit_total}"
    return f"""
<section class="attachment-exhibit" id="{html_escape(dom_id)}" data-sequence="{message.sequence}" data-attachment-row="{attachment.source_rowid}">
  <div class="attachment-exhibit-card">
    <div class="attachment-exhibit-title">{html_escape(label)}</div>
    <div class="attachment-exhibit-meta">
      Message #{message.sequence} | {html_escape(message.direction)} | local {html_escape(message.timestamp_local)} |
      UTC {html_escape(message.timestamp_utc)} | message row {message.source_rowid} | {html_escape(part_text)}
    </div>
    <div class="attachment-exhibit-image-frame">
      <img class="attachment-exhibit-image" src="{src}" alt="{html_escape(label)} row {attachment.source_rowid}">
    </div>
    <div class="attachment-exhibit-caption">{attachment_metadata_table_html(attachment)}</div>
  </div>
</section>
"""


def plugin_payload_url_preview_html(message: MessageRecord, attachments: list[AttachmentRecord]) -> str:
    if not attachments:
        return ""
    selected = select_plugin_payload_preview_attachment(attachments)
    if selected is None:
        return ""
    url = first_url_text(message.text)
    domain = display_domain(url)
    src = f"attachments/{quote(selected.render_filename or '')}"
    rows = [attachment.source_rowid for attachment in attachments]
    note = (
        f"{len(attachments)} plugin payload assets preserved: "
        f"rows {compact_row_ranges(rows)}"
    )
    domain_html = (
        f'<div class="plugin-url-preview-domain">{html_escape(domain)}</div>' if domain else ""
    )
    url_html = f'<div class="plugin-url-preview-url">{html_escape(url)}</div>' if url else ""
    return "".join(
        [
            '<div class="plugin-url-preview">',
            '<div class="plugin-url-preview-card">',
            '<div class="plugin-url-preview-label">URL preview generated by Messages</div>',
            f'<img class="plugin-url-preview-image" src="{src}" ',
            f'alt="Selected plugin payload preview row {html_escape(selected.source_rowid)}">',
            '<div class="plugin-url-preview-body">',
            domain_html,
            url_html,
            f'<div class="plugin-url-preview-note">{html_escape(note)}</div>',
            "</div>",
            "</div>",
            "</div>",
        ]
    )


def location_share_alert_text(message: MessageRecord, label: str) -> str:
    contact = label.strip() or "the contact"
    if message.direction == "incoming":
        return f"{contact} started sharing location with you."
    if message.direction == "outgoing":
        return f"You started sharing location with {contact}."
    if message.share_direction == 1:
        return f"{contact} started sharing location with you."
    return f"You started sharing location with {contact}."


def unsent_message_text(message: MessageRecord, label: str) -> str:
    if message.direction == "outgoing":
        return "You unsent a message"
    contact = label.strip() or "the contact"
    return f"{contact} unsent a message"


def format_call_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown duration"
    if 0 < seconds < 1:
        return "< 1 second"
    total_seconds = max(0, int(round(seconds)))
    if total_seconds < 60:
        return f"{total_seconds} second{'s' if total_seconds != 1 else ''}"
    total_minutes = total_seconds // 60
    if total_minutes < 60:
        return f"{total_minutes} minute{'s' if total_minutes != 1 else ''}"
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if minutes == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return (
        f"{hours} hour{'s' if hours != 1 else ''} "
        f"{minutes} minute{'s' if minutes != 1 else ''}"
    )


def call_status_label(call: CallEvent, participant: str = "contact") -> str:
    status = call_status_key(call)
    if status == "answered":
        return call_direction_title(call, participant)
    return {
        "missed": "Missed",
        "canceled": "Canceled",
        "unanswered": "Unanswered",
    }.get(status, call.answered_label.replace("_", " ").capitalize())


def call_status_key(call: CallEvent) -> str:
    direction = (call.direction or "").lower()
    answered = (call.answered_label or "").lower()
    duration = call.duration_seconds or 0
    if duration > 0 or answered == "answered":
        return "answered"
    if (
        direction == "outgoing"
        and answered == "unanswered_or_missed"
        and duration <= 0
    ):
        return "canceled"
    if answered == "unanswered_or_missed":
        return "missed" if direction == "incoming" else "unanswered"
    return "unknown"


def call_direction_label(call: CallEvent) -> str:
    direction = (call.direction or "").lower()
    if direction == "incoming":
        return "Incoming"
    if direction == "outgoing":
        return "Outgoing"
    return "Unknown"


def call_direction_key(call: CallEvent) -> str:
    direction = (call.direction or "").lower()
    return direction if direction in {"incoming", "outgoing"} else "unknown"


def call_type_text(call: CallEvent) -> str:
    if call.call_type_label and "facetime" in call.call_type_label.lower():
        return "FaceTime call"
    return "phone call"


def call_direction_title(call: CallEvent, participant: str = "contact") -> str:
    direction = call_direction_key(call)
    call_type = call_type_text(call)
    if direction == "incoming":
        return f"Incoming {call_type} from {participant}"
    if direction == "outgoing":
        return f"Outgoing {call_type} to {participant}"
    return f"{call_type.capitalize()} with {participant}"


def call_detail_text(call: CallEvent) -> str:
    parts = [format_human_timestamp(call.timestamp_local)]
    if (call.duration_seconds or 0) > 0:
        parts.append(f"Duration: {format_call_duration(call.duration_seconds)}")
    return " · ".join(parts)


CALL_ICON_DIR = ROOT / "call_icons"
CALL_ICON_FILES = {
    ("answered", "incoming"): "call_icon_review_answered-incoming.svg",
    ("answered", "outgoing"): "call_icon_review_answered-outgoing.svg",
    ("missed", "incoming"): "call_icon_review_missed-incoming.svg",
    ("canceled", "outgoing"): "call_icon_review_canceled-outgoing.svg",
    ("unanswered", "outgoing"): "call_icon_review_missed-outgoing.svg",
}
CALL_ICON_TONE_COLORS = {
    "green": "#1f8a57",
    "red": "#c6413d",
    "amber": "#a66a10",
}


def load_call_icon_asset(filename: str) -> str:
    path = CALL_ICON_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def apply_call_icon_tone(svg: str, tone: str) -> str:
    target = CALL_ICON_TONE_COLORS.get(tone)
    if not target:
        return svg
    for source in CALL_ICON_TONE_COLORS.values():
        svg = svg.replace(source, target)
    return svg


def fallback_call_icon_svg(status: str, direction: str) -> str:
    status = status if status in {"answered", "missed", "canceled", "unanswered"} else "unknown"
    direction = direction if direction in {"incoming", "outgoing"} else "unknown"
    marker_key = {
        ("answered", "incoming"): "incoming-answered",
        ("answered", "outgoing"): "outgoing-answered",
        ("missed", "incoming"): "incoming-missed",
        ("canceled", "outgoing"): "outgoing-canceled",
        ("unanswered", "outgoing"): "outgoing-unanswered",
    }.get((status, direction), "unknown")
    tone = {
        "incoming-answered": "green",
        "outgoing-answered": "green",
        "incoming-missed": "red",
        "outgoing-canceled": "amber",
        "outgoing-unanswered": "red",
    }.get(marker_key, "muted")
    marker_paths = {
        "incoming-answered": '<g transform="translate(16.2 12.6) rotate(-38)"><path class="call-state-arrow" d="M0 0 4.4-3.1v2h8v2.2h-8v2z"/></g>',
        "outgoing-answered": '<g transform="translate(16.2 12.6) rotate(-38)"><path class="call-state-arrow" d="M0-1.1h8v-2l4.4 3.1L8 3.1v-2H0z"/></g>',
        "incoming-missed": '<g transform="translate(16.2 12.6) rotate(-38)"><path class="call-state-arrow" d="M0 0 4.4-3.1v2h8v2.2h-8v2z"/></g><path class="call-state-x" d="m23.6 16.7-1.6-1.6 1.3-1.3 1.6 1.6 1.6-1.6 1.3 1.3-1.6 1.6 1.6 1.6-1.3 1.3-1.6-1.6-1.6 1.6-1.3-1.3z"/>',
        "outgoing-canceled": '<g transform="translate(16.2 12.6) rotate(-38)"><path class="call-state-arrow" d="M0-1.1h8v-2l4.4 3.1L8 3.1v-2H0z"/></g><path class="call-state-x" d="m23.6 16.7-1.6-1.6 1.3-1.3 1.6 1.6 1.6-1.6 1.3 1.3-1.6 1.6 1.6 1.6-1.3 1.3-1.6-1.6-1.6 1.6-1.3-1.3z"/>',
        "outgoing-unanswered": '<g transform="translate(16.2 12.6) rotate(-38)"><path class="call-state-arrow" d="M0-1.1h8v-2l4.4 3.1L8 3.1v-2H0z"/></g><path class="call-state-line" d="M22.2 18.7h5.5v2h-5.5z"/>',
        "unknown": '<path class="call-state-line" d="M22.2 18.7h5.5v2h-5.5z"/>',
    }
    return f"""<svg class="call-state-icon call-state-icon-{status} call-state-icon-{direction} call-state-icon-{tone}" viewBox="0 0 32 24" role="img" aria-label="{html_escape(status)} {html_escape(direction)} call">
  <path class="call-phone" d="M6.06 4.1c-.67 0-1.2.54-1.2 1.2v1.07c0 6.95 5.63 12.58 12.58 12.58h1.07c.67 0 1.2-.54 1.2-1.2v-2.45c0-.55-.38-1.04-.92-1.16l-2.64-.6c-.47-.11-.96.07-1.26.45l-1.05 1.36a10.5 10.5 0 0 1-5.4-5.4l1.36-1.05c.38-.3.56-.79.45-1.26l-.6-2.64a1.2 1.2 0 0 0-1.16-.92H6.06z"/>
  <g class="call-state-marker call-state-marker-{marker_key}" fill="currentColor">
    {marker_paths[marker_key]}
  </g>
</svg>"""


def call_icon_svg(status: str, direction: str) -> str:
    status = status if status in {"answered", "missed", "canceled", "unanswered"} else "unknown"
    direction = direction if direction in {"incoming", "outgoing"} else "unknown"
    tone = {
        ("answered", "incoming"): "green",
        ("answered", "outgoing"): "green",
        ("missed", "incoming"): "red",
        ("canceled", "outgoing"): "amber",
        ("unanswered", "outgoing"): "red",
    }.get((status, direction), "muted")
    filename = CALL_ICON_FILES.get((status, direction))
    if filename and (CALL_ICON_DIR / filename).exists():
        svg = apply_call_icon_tone(load_call_icon_asset(filename), tone)
        class_value = (
            f"call-state-icon call-state-icon-{status} "
            f"call-state-icon-{direction} call-state-icon-{tone}"
        )
        return re.sub(
            r'<svg\b',
            f'<svg class="{class_value}" role="img" '
            f'aria-label="{html_escape(status)} {html_escape(direction)} call" '
            f'data-call-icon-source="{html_escape(filename)}" ',
            svg,
            count=1,
        )
    return fallback_call_icon_svg(status, direction)


def call_event_html(call: CallEvent, label: str = "contact") -> str:
    status = call_status_key(call)
    direction = call_direction_key(call)
    participant = label or "contact"

    call_type = call_type_text(call)

    if direction == "incoming":
        direction_text = f"Incoming {call_type} from {participant}"
    elif direction == "outgoing":
        direction_text = f"Outgoing {call_type} to {participant}"
    else:
        direction_text = f"Direction unknown {call_type} with {participant}"

    status_text = call_status_label(call, participant)
    if status == "answered":
        title_html = (
            f'<span class="call-status-word call-status-word-{status}">{html_escape(status_text)}</span>'
        )
    else:
        title_html = (
            f'<span class="call-status-word call-status-word-{status}">{html_escape(status_text)}</span>'
            '<span class="call-title-separator">·</span>'
            f"<span>{html_escape(direction_text)}</span>"
        )

    duration_html = ""
    if (call.duration_seconds or 0) > 0:
        duration_html = (
            '<div class="call-duration">'
            '<span class="call-duration-label">Duration:</span> '
            f'<span class="call-duration-value">{html_escape(format_call_duration(call.duration_seconds))}</span>'
            "</div>"
        )
    row = call.source_rowid if call.source_rowid is not None else "unknown"
    utc = call.timestamp_utc or "unknown"
    guid = call.unique_id or "unknown"
    footer = (
        f"UTC: {utc} · row: {row} · guid: {guid} · "
        f"source: {call.source_label}"
    )
    local_year = call.timestamp_local[:4] if call.timestamp_local else ""
    return f"""
<section class="message call-event" data-call-sequence="{call.sequence}" data-call-status="{status}" data-call-direction="{direction}" data-local-year="{local_year}">
  <div class="message-meta">
    <div class="meta-primary">#{call.sequence} | Call Event | {html_escape(format_human_timestamp(call.timestamp_local))}</div>
  </div>
  <div class="call-card call-status-{status}">
    <div class="call-title">
      {title_html}
    </div>
    {duration_html}
    <div class="call-footer">{html_escape(footer)}</div>
  </div>
</section>
"""



def findmy_location_card_title(message: MessageRecord, label: str) -> str:
    contact = label.strip() or "the contact"
    if message.direction == "incoming":
        return f"Find My location shared by {contact}"
    if message.direction == "outgoing":
        return f"Find My location shared with {contact}"
    return "Find My location share"


def findmy_location_card_html(message: MessageRecord, label: str = "the contact") -> str:
    preview = next(
        (media for media in message.derived_media if media.mime_type in IMAGE_RENDER_MIME_TYPES),
        None,
    )
    title = findmy_location_card_title(message, label)
    metadata = [value for value in message.payload_metadata if value and value != title]
    caption_parts = ["Rendered from local message payload data."]
    if metadata:
        caption_parts.append("Payload metadata: " + "; ".join(metadata[:3]))
    if preview:
        src = f"derived_media/{quote(preview.export_filename)}"
        dimensions = (
            f"{preview.width}x{preview.height}" if preview.width and preview.height else "unknown dimensions"
        )
        if preview.render_kind == "thumbnail":
            media_html = (
                '<div class="location-card-thumbnail-frame">'
                f'<img class="location-card-thumbnail" src="{src}" '
                'alt="Find My local payload thumbnail">'
                "</div>"
            )
            caption_parts.append(
                f"Local payload thumbnail SHA-256: {preview.sha256}; dimensions: {dimensions}."
            )
        else:
            media_html = (
                f'<img class="location-card-image" src="{src}" alt="Find My location preview">'
            )
            caption_parts.append(f"Preview SHA-256: {preview.sha256}; dimensions: {dimensions}.")
    else:
        media_html = ""
        caption_parts.append("No extractable local preview image was found.")
    return (
        '<div class="location-card">'
        f"{media_html}"
        '<div class="location-card-body">'
        f'<div class="location-card-title">{html_escape(title)}</div>'
        f'<div class="location-card-caption">{html_escape(" ".join(caption_parts))}</div>'
        "</div>"
        "</div>"
    )


def reply_context_html(message: MessageRecord) -> str:
    if not message.reply_context_source or message.reply_target_sequence is None:
        return ""
    meta = (
        f"Reply to #{message.reply_target_sequence} | "
        f"{message.reply_target_direction or 'unknown'} | "
        f"{message.reply_target_timestamp_local or 'unknown time'} | "
        f"row {message.reply_target_rowid or 'unknown'}"
    )
    return (
        '<div class="reply-context">'
        f'<div class="reply-context-meta">{html_escape(meta)}</div>'
        f'<div class="reply-context-excerpt">{html_escape(message.reply_target_excerpt)}</div>'
        "</div>"
    )


REACTION_PREFIX_RE = re.compile(
    r"^("
    r"Liked|Loved|Disliked|Laughed at|Emphasized|Questioned|"
    r"Reacted\s+\S+\s+to|"
    r"Reaccionó\s+con\s+\S+\s+a|"
    r"Removed\s+.*?\s+from|Removed\s+reaction\s+from|"
    r"Le\s+gustó|Me\s+gustó|Te\s+gustó|"
    r"Le\s+encantó|Me\s+encantó|Te\s+encantó|"
    r"No\s+le\s+gustó|No\s+me\s+gustó|No\s+te\s+gustó|"
    r"Le\s+pareció\s+divertido|Me\s+pareció\s+divertido|Te\s+pareció\s+divertido|Se\s+rió\s+de|"
    r"Enfatizó|Me\s+enfatizó|Te\s+enfatizó|"
    r"Preguntó|Me\s+preguntó|Te\s+preguntó|"
    r"Quitó\s+.*?\s+de|Removió\s+.*?\s+de"
    r")(.*)$",
    re.IGNORECASE | re.DOTALL
)


def get_reaction_emoji(prefix: str) -> str:
    prefix_lower = prefix.lower()
    if "disliked" in prefix_lower or "dislike" in prefix_lower or "no le gustó" in prefix_lower or "no me gustó" in prefix_lower or "no te gustó" in prefix_lower:
        return "👎"
    if "loved" in prefix_lower or "love" in prefix_lower or "encantó" in prefix_lower or "encanto" in prefix_lower:
        return "❤️"
    if "liked" in prefix_lower or "like" in prefix_lower or "gustó" in prefix_lower or "gusto" in prefix_lower:
        return "👍"
    if "laughed" in prefix_lower or "laugh" in prefix_lower or "divertido" in prefix_lower or "rió" in prefix_lower or "rio" in prefix_lower:
        return "😂"
    if "emphasized" in prefix_lower or "emphasis" in prefix_lower or "enfatizó" in prefix_lower or "enfatizo" in prefix_lower:
        return "‼️"
    if "questioned" in prefix_lower or "question" in prefix_lower or "preguntó" in prefix_lower or "pregunto" in prefix_lower:
        return "❓"
    return ""


def get_spanish_translation(prefix: str) -> str:
    prefix_lower = prefix.lower()
    if "no le gustó" in prefix_lower or "no me gustó" in prefix_lower or "no te gustó" in prefix_lower:
        return "Disliked"
    if "le encantó" in prefix_lower or "me encantó" in prefix_lower or "te encantó" in prefix_lower:
        return "Loved"
    if "le gustó" in prefix_lower or "me gustó" in prefix_lower or "te gustó" in prefix_lower:
        return "Liked"
    if "divertido" in prefix_lower or "rió" in prefix_lower or "rio" in prefix_lower:
        return "Laughed at"
    if "enfatizó" in prefix_lower or "enfatizo" in prefix_lower:
        return "Emphasized"
    if "preguntó" in prefix_lower or "pregunto" in prefix_lower:
        return "Questioned"
    if "reaccionó con" in prefix_lower or "reacciono con" in prefix_lower:
        # e.g. "Reaccionó con 🫡" -> "Reacted 🫡"
        parts = prefix.split()
        if len(parts) >= 3:
            emoji = parts[2]
            return f"Reacted {emoji}"
        return "Reacted"
    return ""


def format_reaction_text(text: str) -> str:
    escaped = html_escape(text)
    match = REACTION_PREFIX_RE.match(escaped)
    if match:
        prefix, remainder = match.groups()
        prefix_lower = prefix.lower()
        if prefix_lower.endswith(" to"):
            prefix = prefix[:-3]
            remainder = " to" + remainder
        elif prefix_lower.endswith(" a"):
            prefix = prefix[:-2]
            remainder = " a" + remainder
        translation = get_spanish_translation(prefix)
        translation_str = f" ({translation})" if translation else ""
        emoji = get_reaction_emoji(prefix)
        emoji_suffix = f" {emoji}" if emoji else ""
        return f'<span class="reaction-action">{prefix}{translation_str}{emoji_suffix}</span>{remainder}'
    return escaped


def reaction_context_html(message: MessageRecord) -> str:
    if message.render_kind != "reaction" or message.reaction_target_sequence is None:
        return ""
    meta = (
        f"Reaction to #{message.reaction_target_sequence} | "
        f"{message.reaction_target_direction or 'unknown'} | "
        f"{message.reaction_target_timestamp_local or 'unknown time'} | "
        f"row {message.reaction_target_rowid or 'unknown'}"
    )
    return (
        '<div class="reply-context">'
        f'<div class="reply-context-meta">{html_escape(meta)}</div>'
        "</div>"
    )


def write_html(
    path: Path,
    messages: list[MessageRecord],
    chat_summary: dict[str, Any],
    label: str,
    export_started: str,
    source_db_hash: str,
    calls: list[CallEvent] | None = None,
    call_db_hash: str | None = None,
    owner_label: str = "Exporting user",
) -> None:
    handles = ", ".join(chat_summary["handles"])
    message_blocks = []
    current_year = None
    for item in merge_timeline(messages, calls):
        ts = item.timestamp_local or item.timestamp_utc
        item_year = None
        if ts:
            try:
                item_year = ts[:4]
            except Exception:
                pass

        if item_year and item_year != current_year:
            if current_year is not None:
                divider_html = f"""
<section class="message year-divider" data-local-year="{item_year}">
  <div class="year-divider-text">{item_year}</div>
  <div class="year-divider-line">────────────────────────</div>
</section>
"""
                message_blocks.append(divider_html)
            current_year = item_year

        if isinstance(item, CallEvent):
            message_blocks.append(call_event_html(item, label))
            continue
        message = item
        if message.is_nested:
            continue
        status_note = ""
        if message.body_status in {"fallback", "undecoded"}:
            status_note = f' <span class="status">body_status={html_escape(message.body_status)}</span>'
        join_status_html = ""
        if message.chat_join_status != "joined":
            join_status_html = f' | <span class="status">{html_escape(message.chat_join_status)}</span>'
        meta = f"""
  <div class="message-meta">
    <div class="meta-primary">#{message.sequence} | {html_escape(message.direction.capitalize())} | {html_escape(format_human_timestamp(message.timestamp_local))}{join_status_html}</div>
    <div class="meta-secondary">UTC: {html_escape(message.timestamp_utc)} | row: {message.source_rowid} | guid: <span class="meta-guid">{html_escape(message.guid)}</span>{status_note}</div>
  </div>"""
        local_year_attr = f' data-local-year="{message.timestamp_local[:4]}"' if message.timestamp_local else ''
        if message.render_kind == "location_share_alert":
            message_blocks.append(
                f"""
<section class="message system-alert" data-sequence="{message.sequence}"{local_year_attr}>
  {meta}
  <div class="system-alert-text"><span class="location-icon" aria-hidden="true">&#x2197;</span>{html_escape(location_share_alert_text(message, label))}</div>
</section>
"""
            )
            continue
        if message.render_kind == "unsent_message":
            message_blocks.append(
                f"""
<section class="message system-alert" data-sequence="{message.sequence}"{local_year_attr}>
  {meta}
  <div class="system-alert-text">{html_escape(unsent_message_text(message, label))}</div>
</section>
"""
            )
            continue
        if message.render_kind == "reaction":
            body = format_reaction_text(message.text) if message.text else '<span class="status">[No text body]</span>'
            body_html = f'<div class="message-fragment">{body}</div>'
            reply_html = reaction_context_html(message)
        elif message.render_kind == "findmy_location_card":
            body = findmy_location_card_html(message, label)
            body_html = f'<div class="message-fragment">{body}</div>'
            reply_html = reply_context_html(message)
        else:
            clean_text = message.text.replace("\ufffc", "").strip() if message.text else ""
            if clean_text:
                body = html_escape(clean_text)
            elif message.attachments:
                body = ""
            else:
                body = '<span class="status">[No text body]</span>'
            body_html = f'<div class="message-fragment">{body}</div>' if body else ""
            reply_html = reply_context_html(message)
        grouped_plugin_attachments, remaining_attachments = grouped_plugin_payload_attachments(
            message.attachments
        )
        attachment_parts = []
        for attachment in remaining_attachments:
            attachment_parts.append(attachment_html(attachment))
        attachments = plugin_payload_url_preview_html(
            message, grouped_plugin_attachments
        ) + "".join(attachment_parts)
        siri_html = ""
        if message.sent_with_siri:
            siri_html = '<div class="siri-note">Sent with Siri</div>'
        is_reaction = (message.render_kind == "reaction")
        reaction_class = " reaction" if is_reaction else ""
        reaction_tag_html = ""
        if is_reaction and message.reaction_target_sequence is None:
            reaction_tag_html = '<span class="reaction-tag">Reaction</span>'
            
        reactions_html = ""
        if message.reactions:
            rows_html = []
            for rx in message.reactions:
                sender_display = label if not rx.is_from_me else owner_label
                match = REACTION_PREFIX_RE.match(rx.text) if rx.text else None
                prefix = match.group(1) if match else (rx.text or "")
                prefix_lower = prefix.lower()
                if prefix_lower.endswith(" to"):
                    prefix = prefix[:-3]
                elif prefix_lower.endswith(" a"):
                    prefix = prefix[:-2]
                emoji = get_reaction_emoji(prefix)
                emoji_suffix = f" {emoji}" if emoji else ""
                translation = get_spanish_translation(prefix)
                translation_str = f" ({translation})" if translation else ""
                badge_text = f"{prefix}{translation_str}{emoji_suffix}"
                readable_ts = format_human_timestamp(rx.timestamp_local)
                meta_str = (
                    f"by {sender_display} | "
                    f"{rx.direction} | "
                    f"{readable_ts} | "
                    f"row {rx.source_rowid or 'unknown'}"
                )
                rows_html.append(
                    f"""  <div class="reaction-row">
    <span class="reaction-tree-icon">└─</span>
    <span class="reaction-badge">{html_escape(badge_text)}</span>
    <span class="reaction-meta">{html_escape(meta_str)}</span>
  </div>"""
                )
            joined_rows_html = "\n".join(rows_html)
            reactions_html = (
                f'<div class="reactions-container">\n'
                f'{joined_rows_html}\n'
                f'</div>'
            )

        edited_label_html = ""
        if message.date_edited_raw:
            if message.edit_history:
                history_count = len(message.edit_history)
                label_text = (
                    f"Edited · {history_count} prior version{'s' if history_count != 1 else ''} "
                    "· Details in messages.csv and messages.jsonl"
                )
            else:
                label_text = "Edited · Details in messages.csv and messages.jsonl"
            edited_label_html = f'<span class="edited-label">{html_escape(label_text)}</span>'

        message_blocks.append(
            f"""
<section class="message{reaction_class} {html_escape(message.direction)}" data-sequence="{message.sequence}"{local_year_attr}>
  {meta}
  <div class="bubble">{reply_html}{reaction_tag_html}{body_html}{attachments}</div>
  {edited_label_html}
  {reactions_html}
  {siri_html}
</section>
"""
        )

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>iMessage Legal Export - {html_escape(label)}</title>
  <style>{css()}</style>
</head>
<body>
  <main id="transcript"
        data-title="iMessage Legal Export - {html_escape(label)}"
        data-export-started="{html_escape(export_started)}"
        data-source-db-hash="{html_escape(source_db_hash)}"{f' data-call-db-hash="{html_escape(call_db_hash)}"' if call_db_hash else ""}
        data-chat-id="{html_escape(chat_summary['chat_id'])}"
        data-handles="{html_escape(handles)}">
    <template id="page-header">
      <div class="header">
        <div class="title">iMessage Legal Export - {html_escape(label)}</div>
        <div class="header-year"></div>
        <div class="meta-grid">
          <div class="label">Chat ID</div><div>{html_escape(chat_summary['chat_id'])}</div>
          <div class="label">Handles</div><div>{html_escape(handles)}</div>
          <div class="label">Range</div><div>{html_escape(chat_summary['first_local'])} to {html_escape(chat_summary['last_local'])}</div>
          <div class="label">Exported</div><div>{html_escape(export_started)}</div>
          <div class="label">Source SHA-256</div><div>{html_escape(source_db_hash)}</div>
        </div>
      </div>
    </template>
    <div id="message-source">
      {''.join(message_blocks)}
    </div>
  </main>
  <script src="paginate.js"></script>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def write_paginate_js(path: Path) -> None:
    path.write_text(
        """
(() => {
  const transcript = document.getElementById("transcript");
  const source = document.getElementById("message-source");
  if (!transcript || !source || transcript.dataset.paginated === "true") return;

  const headerTemplate = document.getElementById("page-header");
  const messages = Array.from(source.children);
  source.remove();

  const makePage = () => {
    const page = document.createElement("section");
    page.className = "page";
    page.style.height = "11in";
    const isFirstPage = (transcript.querySelectorAll(".page").length === 0);
    if (isFirstPage) {
      page.appendChild(headerTemplate.content.cloneNode(true));
    } else {
      const header = document.createElement("div");
      header.className = "header compact-header";
      header.innerHTML = `<div class="title">${transcript.dataset.title}</div><div class="header-year"></div>`;
      page.appendChild(header);
    }
    const content = document.createElement("div");
    content.className = "page-content";
    page.appendChild(content);
    const footer = document.createElement("div");
    footer.className = "footer";
    let sourceHtml = `<div>Source chat.db SHA-256: ${transcript.dataset.sourceDbHash}</div>`;
    if (transcript.dataset.callDbHash) {
      sourceHtml += `<div>Source CallHistory.storedata SHA-256: ${transcript.dataset.callDbHash}</div>`;
    }
    footer.innerHTML = `<span class="sources">${sourceHtml}</span><span class="page-number"></span>`;
    page.appendChild(footer);
    transcript.appendChild(page);
    return { page, content, footer };
  };

  let current = makePage();
  const footerPadding = 12;
  const maxTextChunk = 1200;

  const messageNumber = (message) => message.dataset.sequence || "?";

  const addContinuationNote = (message, text) => {
    const existing = message.querySelector(".continuation-note");
    if (existing) existing.remove();
    const meta = message.querySelector(".message-meta");
    const note = document.createElement("div");
    note.className = "continuation-note";
    note.textContent = text;
    if (meta?.nextSibling) {
      meta.parentNode.insertBefore(note, meta.nextSibling);
    } else {
      message.insertBefore(note, message.firstChild);
    }
  };

  const findMaxFittingTextLength = (message, text) => {
    const fragment = message.querySelector(".message-fragment");
    if (!fragment) return 0;
    let low = 0;
    let high = text.length;
    let best = 0;
    const originalText = fragment.textContent;
    while (low <= high) {
      const mid = Math.floor((low + high) / 2);
      fragment.textContent = text.slice(0, mid);
      if (fitsCurrentPage(message)) {
        best = mid;
        low = mid + 1;
      } else {
        high = mid - 1;
      }
    }
    fragment.textContent = originalText;
    return best;
  };

  const splitAttachmentMessage = (message, attachments, fragment) => {
    if (attachments.length <= 1) return null;
    const text = fragment?.textContent || "";
    return attachments.map((attachment, index) => {
      const part = message.cloneNode(true);
      const partFragment = part.querySelector(".message-fragment");
      const partAttachments = Array.from(part.querySelectorAll(".attachment"));
      partAttachments.forEach((node, nodeIndex) => {
        if (nodeIndex !== index) node.remove();
      });
      if (partFragment && index > 0) partFragment.textContent = "";
      if (!text && partFragment && index === 0) partFragment.textContent = "";
      part.dataset.continuationPart = String(index + 1);
      part.dataset.continuationTotal = String(attachments.length);
      return part;
    });
  };

  const splitOversizedMessage = (message) => {
    const fragment = message.querySelector(".message-fragment");
    const attachments = Array.from(message.querySelectorAll(".attachment"));
    return (
      splitAttachmentMessage(message, attachments, fragment) ||
      [message]
    );
  };

  const fitsCurrentPage = (message) => {
    const messageBottom = message.getBoundingClientRect().bottom;
    const footerTop = current.footer.getBoundingClientRect().top;
    return messageBottom <= footerTop - footerPadding;
  };

  const placeAtomic = (message) => {
    const hadMessages = current.content.children.length > 0;
    current.content.appendChild(message);
    if (fitsCurrentPage(message)) return;

    if (!hadMessages) {
      message.dataset.oversized = "true";
      return;
    }
    current.content.removeChild(message);
    current = makePage();
    current.content.appendChild(message);
    if (!fitsCurrentPage(message)) {
      message.dataset.oversized = "true";
    }
  };

  const place = (message) => {
    const hadMessages = current.content.children.length > 0;
    current.content.appendChild(message);
    if (fitsCurrentPage(message)) return;

    current.content.removeChild(message);

    const fragment = message.querySelector(".message-fragment");
    const attachments = message.querySelectorAll(".attachment");

    if (fragment && attachments.length === 0) {
      const text = fragment.textContent || "";
      current.content.appendChild(message);
      const bestLength = findMaxFittingTextLength(message, text);
      current.content.removeChild(message);

      if (!hadMessages || bestLength >= 100) {
        let splitAt = text.slice(0, bestLength).lastIndexOf(" ");
        if (splitAt === -1 || splitAt < bestLength * 0.6) {
          splitAt = bestLength;
        }
        if (splitAt > 0 && splitAt < text.length) {
          const chunk1 = text.slice(0, splitAt).trimEnd();
          const chunk2 = text.slice(splitAt).trimStart();
          if (chunk1 && chunk2) {
            const part1 = message.cloneNode(true);
            part1.querySelector(".message-fragment").textContent = chunk1;
            const part2 = message.cloneNode(true);
            part2.querySelector(".message-fragment").textContent = chunk2;
            place(part1);
            place(part2);
            return;
          }
        }
      }

      if (hadMessages) {
        current = makePage();
        place(message);
        return;
      }

      message.dataset.oversized = "true";
      current.content.appendChild(message);
      return;
    }

    const parts = splitOversizedMessage(message);
    if (parts.length === 1 && parts[0] === message) {
      if (hadMessages) {
        current = makePage();
        placeAtomic(message);
      } else {
        message.dataset.oversized = "true";
        current.content.appendChild(message);
      }
      return;
    }
    parts.forEach(place);
  };

  messages.forEach(place);

  const seqGroups = {};
  transcript.querySelectorAll(".message[data-sequence]").forEach((msg) => {
    const seq = msg.dataset.sequence;
    if (!seq) return;
    if (!seqGroups[seq]) seqGroups[seq] = [];
    seqGroups[seq].push(msg);
  });

  Object.keys(seqGroups).forEach((seq) => {
    const parts = seqGroups[seq];
    if (parts.length > 1) {
      parts.forEach((part, index) => {
        part.dataset.continuationPart = String(index + 1);
        part.dataset.continuationTotal = String(parts.length);
        const hasAttachment = part.querySelector(".attachment") !== null;
        const typeStr = hasAttachment ? "attachment" : "text";
        addContinuationNote(part, `Message #${seq} ${typeStr} part ${index + 1} of ${parts.length}`);
        if (index > 0) {
          part.querySelector(".message-meta")?.remove();
          part.querySelector(".edited-label")?.remove();
          part.querySelector(".reactions-container")?.remove();
        }
      });
    }
  });

  const updatePageYears = (pageEl) => {
    const messages = pageEl.querySelectorAll(".message");
    const years = new Set();
    messages.forEach((msg) => {
      const year = msg.dataset.localYear;
      if (year) {
        years.add(year);
      }
    });
    const headerYear = pageEl.querySelector(".header-year");
    if (headerYear) {
      if (years.size === 1) {
        headerYear.textContent = Array.from(years)[0];
      } else if (years.size > 1) {
        const sorted = Array.from(years).sort();
        headerYear.textContent = `${sorted[0]}–${sorted[sorted.length - 1]}`;
      } else {
        headerYear.textContent = "";
      }
    }
  };
  transcript.querySelectorAll(".page").forEach(updatePageYears);

  Array.from(transcript.querySelectorAll(".page-number")).forEach((node, index, all) => {
    node.textContent = `Page ${index + 1} of ${all.length}`;
  });
  const continuations = Array.from(transcript.querySelectorAll(".message[data-continuation-part]")).length;
  const oversized = Array.from(transcript.querySelectorAll(".message[data-oversized='true']")).length;
  transcript.dataset.continuations = String(continuations);
  transcript.dataset.oversized = String(oversized);
  transcript.dataset.paginated = "true";
})();
""".strip()
        + "\n",
        encoding="utf-8",
    )


def write_report(
    path: Path,
    db_path: Path,
    source_db_hash_before: str,
    source_db_hash_after: str,
    chat_summary: dict[str, Any],
    messages: list[MessageRecord],
    args: argparse.Namespace,
    export_started: str,
    call_context: CallExportContext | None = None,
) -> None:
    body_status_counts: dict[str, int] = {}
    for message in messages:
        body_status_counts[message.body_status] = body_status_counts.get(message.body_status, 0) + 1
    missing_attachments = [
        (message.sequence, attachment)
        for message in messages
        for attachment in message.attachments
        if attachment.status != "copied"
    ]
    attachment_count = sum(len(message.attachments) for message in messages)
    copied_attachment_count = sum(
        1
        for message in messages
        for attachment in message.attachments
        if attachment.status == "copied"
    )
    preview_generated_count = sum(
        1
        for message in messages
        for attachment in message.attachments
        if attachment.preview_status == "preview_generated"
    )
    preview_error_count = sum(
        1
        for message in messages
        for attachment in message.attachments
        if attachment.preview_status and attachment.preview_status != "preview_generated"
    )
    inline_attachment_count = sum(
        1
        for message in messages
        for attachment in message.attachments
        if attachment.status == "copied"
        and (
            bool(attachment.render_filename)
            or attachment.preview_status == "preview_generated"
        )
    )
    metadata_only_attachments = [
        (message.sequence, attachment)
        for message in messages
        for attachment in message.attachments
        if attachment.status == "copied"
        and not attachment.render_filename
        and attachment.preview_status != "preview_generated"
    ]
    plugin_payload_preview_groups = [
        (
            message,
            grouped,
            select_plugin_payload_preview_attachment(grouped),
        )
        for message in messages
        if (grouped := plugin_payload_url_preview_attachments(message.attachments))
    ]
    payload_preview_count = sum(len(message.derived_media) for message in messages)
    recovered_unjoined_sms = [
        message for message in messages if message.chat_join_status == "recovered_unjoined_sms"
    ]
    reply_context_count = sum(1 for message in messages if message.reply_context_source)
    reply_context_source_counts = {
        "thread_originator_guid": sum(
            1 for message in messages if message.reply_context_source == "thread_originator_guid"
        ),
        "reply_to_guid": sum(
            1 for message in messages if message.reply_context_source == "reply_to_guid"
        ),
    }
    missing_reply_refs = missing_reply_reference_count(messages)
    command = " ".join(shlex_quote(part) for part in sys.argv)
    lines = [
        "# iMessage Legal Export Extraction Report",
        "",
        f"- Export started: `{export_started}`",
        f"- Source database: `{db_path}`",
        f"- Source database SHA-256 before export: `{source_db_hash_before}`",
        f"- Source database SHA-256 after export: `{source_db_hash_after}`",
        f"- Source database unchanged during export: `{source_db_hash_before == source_db_hash_after}`",
        f"- Command: `{command}`",
        "",
        "## Selected Thread",
        "",
        f"- Label: `{args.label}`",
        f"- Chat ID: `{chat_summary['chat_id']}`",
        f"- Chat GUID: `{chat_summary['guid']}`",
        f"- Service: `{chat_summary['service_name']}`",
        f"- Style: `{chat_summary['style']}`",
        f"- Handles: `{', '.join(chat_summary['handles'])}`",
        f"- First message local: `{chat_summary['first_local']}`",
        f"- Last message local: `{chat_summary['last_local']}`",
        f"- SQL message count: `{chat_summary['messages']}`",
        f"- Exported message count: `{len(messages)}`",
        f"- Recovered unjoined SMS messages: `{len(recovered_unjoined_sms)}`",
        "",
        "## Source Package Relationship",
        "",
    ]
    if call_context is not None:
        lines.extend([
            "- `thread.pdf` and `thread.html` are clean text-only message transcripts.",
            "- `thread_with_calls.pdf` and `thread_with_calls.html` are chronologically integrated text messages and phone call history transcripts.",
        ])
    else:
        lines.append("- `thread.pdf` and `thread.html` are readable transcript views of the conversation.")
    lines.extend([
        "- Dense metadata is preserved in `messages.jsonl`, `messages.csv`, `attachments/`, `derived_media/`, and `manifest.sha256`.",
        "- Edited-message labels in the transcript mark edited messages; source row IDs and revision history remain in the structured metadata files.",
        "",
        "## Edited Messages",
        "",
        f"- Edited messages detected: `{sum(1 for m in messages if m.date_edited_raw)}`",
        f"- Messages with full revision histories parsed: `{sum(1 for m in messages if m.edit_history)}`",
        "",
        "## Body Decode Status",
        "",
    ])
    for status, count in sorted(body_status_counts.items()):
        lines.append(f"- {status}: `{count}`")
    if typedstream is None:
        lines.extend(
            [
                "",
                "### Warning",
                "",
                "- `pytypedstream` is unavailable in this Python environment; "
                "`message.attributedBody` rows without `message.text` are marked "
                "`undecoded` instead of using legacy fallback extraction.",
            ]
        )
    if call_context is not None:
        call_times = [
            parsed
            for event in call_context.events
            if (parsed := parse_iso_timestamp(event.timestamp_local)) is not None
        ]
        first_call = min(call_times).isoformat(timespec="seconds") if call_times else "not available"
        last_call = max(call_times).isoformat(timespec="seconds") if call_times else "not available"
        direction_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for event in call_context.events:
            direction_counts[event.direction] = direction_counts.get(event.direction, 0) + 1
            status = call_status_label(event)
            status_counts[status] = status_counts.get(status, 0) + 1
            lbl = "FaceTime call" if event.call_type_label and "facetime" in event.call_type_label.lower() else "Phone call"
            type_counts[lbl] = type_counts.get(lbl, 0) + 1
        lines.extend(
            [
                "",
                "## Call Timeline Context",
                "",
                f"- Source call database: `{call_context.source_path}`",
                f"- Call history source description: {call_context.source_description}",
                f"- Copied call JSONL: `{call_context.copied_path.name}`",
                f"- Copied call JSONL SHA-256: `{call_context.sha256}`",
                f"- Inline call events rendered: `{len(call_context.events)}`",
                f"- First call local: `{first_call}`",
                f"- Last call local: `{last_call}`",
                "- Phone numbers and private CallHistory enum values are not displayed in the public transcript call cards.",
                "- Public transcript call status treats any positive duration as a completed call while preserving raw private enum values in JSONL.",
                "- Raw call metadata remains preserved in `call_records.jsonl` and `manifest.sha256`.",
            ]
        )
        if type_counts:
            lines.append(
                "- Call types: "
                + ", ".join(f"`{key}` `{value}`" for key, value in sorted(type_counts.items()))
            )
        if direction_counts:
            lines.append(
                "- Call directions: "
                + ", ".join(f"`{key}` `{value}`" for key, value in sorted(direction_counts.items()))
            )
        if status_counts:
            lines.append(
                "- Call statuses/durations: "
                + ", ".join(f"`{key}` `{value}`" for key, value in sorted(status_counts.items()))
            )
    lines.extend(
        [
            "",
            "## Reply Context",
            "",
            f"- Messages with visible reply context: `{reply_context_count}`",
            f"- Reply contexts from `thread_originator_guid`: `{reply_context_source_counts['thread_originator_guid']}`",
            "- Reply contexts from `reply_to_guid`: `0`",
            f"- Reply references with targets missing from this export: `{missing_reply_refs}`",
            "- `reply_to_guid`, `associated_message_guid`, and `associated_message_type` are retained as raw metadata and are not rendered as normal reply context.",
        ]
    )
    lines.extend(
        [
            "",
            "## Attachments",
            "",
            f"- Attachment records: `{attachment_count}`",
            f"- Copied attachments: `{copied_attachment_count}`",
            f"- Missing or unavailable attachments: `{len(missing_attachments)}`",
            f"- Inline or preview-rendered attachments: `{inline_attachment_count}`",
            f"- Generated attachment previews: `{preview_generated_count}`",
            f"- Attachment preview errors: `{preview_error_count}`",
            f"- Payload-derived previews: `{payload_preview_count}`",
            f"- Copied metadata-only attachments: `{len(metadata_only_attachments)}`",
        ]
    )
    if plugin_payload_preview_groups:
        lines.extend(["", "## Plugin Payload URL Previews", ""])
        lines.append(
            "Grouped plugin payload image assets are rendered as a compact URL preview in "
            "`thread.html`; every copied asset remains listed and hashed below."
        )
        lines.append("")
        for message, grouped, selected in plugin_payload_preview_groups[:200]:
            rows = [attachment.source_rowid for attachment in grouped]
            selected_row = selected.source_rowid if selected else "not available"
            lines.append(
                f"- Message #{message.sequence}, source row `{message.source_rowid}`, "
                f"selected preview row `{selected_row}`, attachment rows `{compact_row_ranges(rows)}`"
            )
            for attachment in grouped:
                media_type = attachment.detected_mime_type or attachment.mime_type or attachment.uti or "unknown"
                dimensions = (
                    f", dimensions `{attachment.width}x{attachment.height}`"
                    if attachment.width and attachment.height
                    else ""
                )
                lines.append(
                    f"  - Attachment row `{attachment.source_rowid}`, "
                    f"type `{media_type}`, bytes `{attachment.total_bytes if attachment.total_bytes is not None else 'unknown'}`"
                    f"{dimensions}, SHA-256 `{attachment.sha256 or 'not available'}`"
                )
        if len(plugin_payload_preview_groups) > 200:
            lines.append(f"- ...and {len(plugin_payload_preview_groups) - 200} more groups.")
    if missing_attachments:
        lines.extend(["", "### Missing Attachments", ""])
        for sequence, attachment in missing_attachments[:200]:
            lines.append(
                f"- Message #{sequence}, attachment row `{attachment.source_rowid}`, "
                f"status `{attachment.status}`: "
                f"`{attachment.original_filename or 'no filename'}`"
            )
        if len(missing_attachments) > 200:
            lines.append(f"- ...and {len(missing_attachments) - 200} more.")
    if metadata_only_attachments:
        lines.extend(["", "### Metadata-Only Copied Attachments", ""])
        for sequence, attachment in metadata_only_attachments[:200]:
            media_type = attachment.detected_mime_type or attachment.mime_type or attachment.uti or "unknown"
            lines.append(
                f"- Message #{sequence}, attachment row `{attachment.source_rowid}`, "
                f"type `{media_type}`: "
                f"`{attachment.export_filename or attachment.original_filename or 'no filename'}`"
            )
        if len(metadata_only_attachments) > 200:
            lines.append(f"- ...and {len(metadata_only_attachments) - 200} more.")
    if recovered_unjoined_sms:
        lines.extend(["", "### Recovered Unjoined SMS Messages", ""])
        lines.append(
            "These outgoing SMS rows were present in `message` during the selected chat date "
            "range but had no `chat_message_join` row. They are included chronologically and "
            "marked `recovered_unjoined_sms` in transcript metadata."
        )
        lines.append("")
        for message in recovered_unjoined_sms[:200]:
            lines.append(
                f"- Message #{message.sequence}, row `{message.source_rowid}`, "
                f"local `{message.timestamp_local}`, guid `{message.guid}`"
            )
        if len(recovered_unjoined_sms) > 200:
            lines.append(f"- ...and {len(recovered_unjoined_sms) - 200} more.")
    lines.extend(
        [
            "",
            "## Review Notes",
            "",
            "- Spot-check first, last, and representative messages against macOS Messages before filing.",
            "- Review any `fallback` or `undecoded` body statuses in `messages.csv`.",
            "- Review edited-message revision histories in `messages.jsonl` or the edit-history columns in `messages.csv`.",
            "- This export is a technical evidence-preparation aid, not legal advice.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def shlex_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def write_certification_template(path: Path, db_path: Path, output_dir: Path, label: str) -> None:
    path.write_text(
        f"""# Certification Worksheet

This worksheet is a factual template for counsel or the records custodian to review.
It is not legal advice.

## Evidence Source

- Conversation label: {label}
- Source database copy: {db_path}
- Export directory: {output_dir}
- Export date/time: ______________________________
- Person performing export: ______________________

## Chain of Custody

1. I copied the macOS Messages database from the relevant computer or preserved copy.
2. I exported the selected thread using this local exporter.
3. I generated SHA-256 hashes for the source database, transcript files, metadata files, and attachments.
4. I reviewed the transcript for completeness against the Messages application.
5. I preserved the export directory and did not alter generated evidence files after hashing.

## Review Checklist

- Source database hash in `manifest.sha256` matches the report.
- `messages.csv` row count matches the exported message count in `extraction_report.md`.
- First and last messages match the Messages app.
- Attachment statuses were reviewed.
- Any fallback or undecoded message bodies were reviewed.

Signature: ______________________________

Date: ___________________________________
""",
        encoding="utf-8",
    )


def write_manifest(path: Path, output_dir: Path, db_path: Path) -> None:
    entries: list[tuple[str, str]] = [(sha256_file(db_path), str(db_path))]
    for file_path in sorted(p for p in output_dir.rglob("*") if p.is_file()):
        if file_path == path:
            continue
        entries.append((sha256_file(file_path), str(file_path.relative_to(output_dir))))
    path.write_text("".join(f"{digest}  {name}\n" for digest, name in entries), encoding="utf-8")


GENERATED_OUTPUT_FILES = {
    "messages.csv",
    "messages.jsonl",
    "timeline.jsonl",
    "call_records.jsonl",
    "thread.html",
    "thread.pdf",
    "thread_with_calls.html",
    "thread_with_calls.pdf",
    "paginate.js",
    "manifest.sha256",
    "extraction_report.md",
    "certification_template.md",
    "pdf_render_error.txt",
}


def clean_generated_output(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    for name in GENERATED_OUTPUT_FILES:
        path = output_dir / name
        if path.exists() and path.is_file():
            path.unlink()
    for name in ("attachments", "derived_media"):
        path = output_dir / name
        if path.exists():
            shutil.rmtree(path)


def run_renderer(output_dir: Path, html_path: Path, pdf_path: Path) -> tuple[bool, str]:
    renderer = ROOT / "render_pdf.mjs"
    if not renderer.exists():
        return False, "render_pdf.mjs is missing"
    try:
        result = subprocess.run(
            ["node", str(renderer), str(html_path), str(pdf_path)],
            cwd=output_dir,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "renderer failed").strip()
    return True, (result.stdout or "PDF rendered").strip()


def export_thread(args: argparse.Namespace) -> int:
    if hasattr(args, "timezone"):
        configure_timezone(args.timezone)
    db_path = expand_path(args.db)
    output_dir = expand_path(args.out)
    attachments_root = expand_path(args.attachments_root)
    attachments_dir = output_dir / "attachments"
    derived_media_dir = output_dir / "derived_media"
    clean_generated_output(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    attachments_dir.mkdir(parents=True, exist_ok=True)
    derived_media_dir.mkdir(parents=True, exist_ok=True)

    export_started = dt.datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")
    source_hash_before = sha256_file(db_path)
    with connect_db(db_path) as conn:
        chat_summary = load_chat_summary(conn, args.chat_id)
        messages = load_messages(
            conn, args.chat_id, attachments_root, attachments_dir, derived_media_dir
        )
    source_hash_after = sha256_file(db_path)
    call_context = prepare_call_export(
        getattr(args, "calls_jsonl", None),
        getattr(args, "call_db", None),
        chat_summary,
        output_dir,
        getattr(
            args,
            "call_source_description",
            "Supplied CallHistory.storedata database",
        ),
    )

    joined_message_count = sum(1 for message in messages if message.chat_join_status == "joined")
    if int(chat_summary["messages"]) != joined_message_count:
        raise RuntimeError(
            f"message count mismatch: SQL summary={chat_summary['messages']}, "
            f"joined exported={joined_message_count}"
        )

    write_jsonl(output_dir / "messages.jsonl", messages)
    write_csv(output_dir / "messages.csv", messages)
    write_timeline_jsonl(
        output_dir / "timeline.jsonl",
        messages,
        call_context.events if call_context else None,
    )
    write_paginate_js(output_dir / "paginate.js")
    write_html(
        output_dir / "thread.html",
        messages,
        chat_summary,
        args.label,
        export_started,
        source_hash_before,
        calls=None,
        owner_label=getattr(args, "owner_label", "Exporting user"),
    )
    if call_context:
        write_html(
            output_dir / "thread_with_calls.html",
            messages,
            chat_summary,
            args.label,
            export_started,
            source_hash_before,
            calls=call_context.events,
            call_db_hash=call_context.sha256,
            owner_label=getattr(args, "owner_label", "Exporting user"),
        )
    write_report(
        output_dir / "extraction_report.md",
        db_path,
        source_hash_before,
        source_hash_after,
        chat_summary,
        messages,
        args,
        export_started,
        call_context,
    )
    write_certification_template(
        output_dir / "certification_template.md", db_path, output_dir, args.label
    )

    if not args.no_pdf:
        ok_thread, msg_thread = run_renderer(output_dir, output_dir / "thread.html", output_dir / "thread.pdf")
        ok_calls, msg_calls = True, ""
        if call_context:
            ok_calls, msg_calls = run_renderer(
                output_dir, output_dir / "thread_with_calls.html", output_dir / "thread_with_calls.pdf"
            )

        if not ok_thread or not ok_calls:
            err_msg = ""
            if not ok_thread:
                err_msg += f"thread.html rendering failed: {msg_thread}\n"
            if not ok_calls:
                err_msg += f"thread_with_calls.html rendering failed: {msg_calls}\n"
            (output_dir / "pdf_render_error.txt").write_text(err_msg, encoding="utf-8")
            print(
                "PDF rendering was skipped or failed. See pdf_render_error.txt. "
                "Run `npm install` and retry the export.",
                file=sys.stderr,
            )
        else:
            print(msg_thread)
            if call_context:
                print(msg_calls)

    write_manifest(output_dir / "manifest.sha256", output_dir, db_path)
    print(f"Exported {len(messages)} messages to {output_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imessage_legal_exporter",
        description="Export macOS Messages chat.db threads as legal-style evidence packages.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-threads", help="List candidate chat threads.")
    list_parser.add_argument("--db", required=True, help="Path to copied chat.db.")
    list_parser.add_argument("--limit", type=int, default=50, help="Maximum threads to list.")
    list_parser.add_argument(
        "--timezone",
        help="IANA timezone for local timestamps (default: detected system timezone).",
    )
    list_parser.set_defaults(func=list_threads)

    export_parser = subparsers.add_parser("export", help="Export a selected chat thread.")
    export_parser.add_argument("--db", required=True, help="Path to copied chat.db.")
    export_parser.add_argument("--chat-id", required=True, type=int, help="chat.ROWID to export.")
    export_parser.add_argument("--label", required=True, help="Human label for the transcript.")
    export_parser.add_argument(
        "--owner-label",
        default="Exporting user",
        help="Name used for outgoing reaction attribution (default: Exporting user).",
    )
    export_parser.add_argument(
        "--attachments-root",
        default="~/Library/Messages/Attachments",
        help="Root used to resolve Messages attachment paths.",
    )
    export_parser.add_argument("--out", required=True, help="Output directory.")
    export_parser.add_argument(
        "--timezone",
        help="IANA timezone for local timestamps (default: detected system timezone).",
    )
    export_parser.add_argument(
        "--calls-jsonl",
        help="Optional matched call_records.jsonl to copy and render as neutral timeline context.",
    )
    export_parser.add_argument(
        "--call-db",
        help="Optional path to copied CallHistory.storedata database for automated call timeline integration.",
    )
    export_parser.add_argument(
        "--call-source-description",
        default="Supplied CallHistory.storedata database",
        help="Factual provenance label shown for the supplied call-history source.",
    )
    export_parser.add_argument("--no-pdf", action="store_true", help="Skip Playwright PDF rendering.")
    export_parser.set_defaults(func=export_thread)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
