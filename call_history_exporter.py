#!/usr/bin/env python3
"""Export matched CallHistory.storedata rows as an evidence package."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


APPLE_EPOCH_OFFSET_SECONDS = 978_307_200
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


ACQUISITION_COMMAND = (
    "Not recorded; provide --acquisition-command with the actual copy or extraction process."
)

CALL_FIELDS = [
    "Z_PK",
    "ZUNIQUE_ID",
    "ZDATE",
    "ZDURATION",
    "ZORIGINATED",
    "ZANSWERED",
    "ZCALLTYPE",
    "ZCALL_CATEGORY",
    "ZFACE_TIME_DATA",
    "ZSERVICE_PROVIDER",
    "ZISO_COUNTRY_CODE",
    "ZLOCATION",
    "ZNAME",
    "ZREAD",
    "ZDISCONNECTED_CAUSE",
    "ZFILTERED_OUT_REASON",
    "ZJUNKCONFIDENCE",
    "ZJUNKIDENTIFICATIONCATEGORY",
    "ZNUMBER_AVAILABILITY",
    "ZVERIFICATIONSTATUS",
    "ZADDRESS",
    "ZCONVERSATIONID",
    "ZLOCALPARTICIPANTUUID",
    "ZOUTGOINGLOCALPARTICIPANTUUID",
    "ZPARTICIPANTGROUPUUID",
    "ZAUTOANSWEREDREASON",
    "ZWASEMERGENCYCALL",
    "ZUSEDEMERGENCYVIDEOSTREAMING",
    "ZBLOCKEDBYEXTENSION",
    "ZCALLDIRECTORYIDENTITYTYPE",
    "ZSCREENSHARINGTYPE",
    "ZIDENTITYEXTENSION",
    "ZINITIATOR",
    "ZBLOCKEDBYEXTENSIONNAME",
    "ZNEEDEDSCANNOUNCEMENT",
    "ZCOMMUNICATIONTRUSTSCORE",
    "ZREMINDERUUID",
    "ZORIGINATINGDEVICENAME",
    "ZORIGINATINGUITYPE",
    "ZHANDLE_TYPE",
    "ZHASMESSAGE",
    "ZIMAGEURL",
]

CSV_FIELDS = [
    "sequence",
    "z_pk",
    "zunique_id",
    "timestamp_utc",
    "timestamp_local",
    "zdate_raw",
    "duration_seconds",
    "direction",
    "call_type_label",
    "answered_label",
    "zaddress",
    "normalized_address",
    "zname",
    "remote_participant_handles",
    "remote_participant_normalized_handles",
    "remote_participant_handle_pks",
    "conversation_id_hex",
    "local_participant_uuid_hex",
    "outgoing_local_participant_uuid_hex",
    "participant_group_uuid_hex",
    "raw_json",
]

PRIVATE_ENUM_NOTE = (
    "Apple does not publicly document the private SQLite enum mappings for many "
    "CallHistory.storedata fields. Raw values are preserved; labels are cautious "
    "operator aids, not forensic conclusions."
)

PUBLIC_CALL_SOURCE_LABEL = "Supplied CallHistory.storedata database"
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


@dataclass(frozen=True)
class CallRecord:
    sequence: int
    raw: dict[str, Any]
    timestamp_utc: str | None
    timestamp_local: str | None
    duration_seconds: float | None
    normalized_address: str
    direction: str
    answered_label: str
    call_type_label: str
    participant_handles: list[str]
    participant_normalized_handles: list[str]
    participant_handle_pks: list[int]


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


def apple_timestamp_to_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    try:
        seconds = float(value) + APPLE_EPOCH_OFFSET_SECONDS
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)


def timestamp_strings(value: Any) -> tuple[str | None, str | None]:
    timestamp = apple_timestamp_to_datetime(value)
    if timestamp is None:
        return None, None
    return (
        timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
        timestamp.astimezone(LOCAL_TZ).isoformat(timespec="seconds"),
    )


def normalize_phone_identifier(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    digits = re.sub(r"\D+", "", text)
    if not digits:
        return text.lower()
    if len(digits) == 10:
        return "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return digits
    return digits


def match_keys(value: Any) -> set[str]:
    normalized = normalize_phone_identifier(value)
    if not normalized:
        return set()
    keys = {normalized}
    digits = re.sub(r"\D+", "", str(value))
    if len(normalized) >= 10 and normalized.isdigit():
        keys.add(normalized[-10:])
    if len(digits) >= 10:
        keys.add(digits[-10:])
    return keys


def mask_phone_number(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    digits = [ch for ch in text if ch.isdigit()]
    if len(digits) < 7:
        return text
    keep_prefix = 2 if len(digits) > 10 else 1
    keep_suffix = 4
    digit_index = 0
    rendered: list[str] = []
    for ch in text:
        if not ch.isdigit():
            rendered.append(ch)
            continue
        if digit_index < keep_prefix or digit_index >= len(digits) - keep_suffix:
            rendered.append(ch)
        elif digit_index == keep_prefix:
            rendered.append("***")
        digit_index += 1
    return "".join(rendered)


def html_escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def value_to_jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    return value


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def load_message_match_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            for field in ("handle", "zaddress", "phone", "identifier"):
                keys.update(match_keys(row.get(field)))
    return keys


def blob_hex(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]*", value):
        return value
    return ""


def direction_label(raw: dict[str, Any]) -> str:
    if raw.get("ZORIGINATED") == 1:
        return "outgoing"
    if raw.get("ZORIGINATED") == 0:
        return "incoming"
    return "unknown"


def answered_label(raw: dict[str, Any]) -> str:
    if raw.get("ZANSWERED") == 1:
        return "answered"
    if raw.get("ZANSWERED") == 0:
        return "unanswered_or_missed"
    return "unknown"


def call_status_key(record: CallRecord) -> str:
    direction = (record.direction or "").lower()
    answered = (record.answered_label or "").lower()
    duration = record.duration_seconds or 0
    if duration > 0 or answered == "answered":
        return "answered"
    if answered == "unanswered_or_missed":
        if direction == "incoming":
            return "missed"
        if direction == "outgoing" and duration <= 0:
            return "canceled"
        if direction == "outgoing":
            return "unanswered"
    return "unknown"


def call_direction_key(record: CallRecord) -> str:
    direction = (record.direction or "").lower()
    return direction if direction in {"incoming", "outgoing"} else "unknown"


def call_direction_phrase(record: CallRecord, participant: str) -> str:
    direction = call_direction_key(record)
    call_type = call_type_text(record)
    if direction == "incoming":
        return f"Incoming {call_type} from {participant}"
    if direction == "outgoing":
        return f"Outgoing {call_type} to {participant}"
    return f"Direction unknown {call_type} with {participant}"


def call_type_text(record: CallRecord) -> str:
    if record.call_type_label and "facetime" in record.call_type_label.lower():
        return "FaceTime call"
    return "phone call"


def call_direction_title(record: CallRecord, participant: str) -> str:
    direction = call_direction_key(record)
    call_type = call_type_text(record)
    if direction == "incoming":
        return f"Incoming {call_type} from {participant}"
    if direction == "outgoing":
        return f"Outgoing {call_type} to {participant}"
    return f"{call_type.capitalize()} with {participant}"


def call_title(record: CallRecord, label: str) -> str:
    participant = str(record.raw.get("ZNAME") or label or "contact").strip() or "contact"
    if call_status_key(record) == "answered":
        return call_direction_title(record, participant)
    status = {
        "missed": "Missed",
        "canceled": "Canceled",
        "unanswered": "Unanswered",
        "unknown": "Unknown",
    }.get(call_status_key(record), "Unknown")
    return f"{status} {call_direction_phrase(record, participant)}"


def call_status_text(record: CallRecord, label: str = "contact") -> str:
    participant = str(record.raw.get("ZNAME") or label or "contact").strip() or "contact"
    if call_status_key(record) == "answered":
        return call_direction_title(record, participant)
    return {
        "missed": "Missed",
        "canceled": "Canceled",
        "unanswered": "Unanswered",
        "unknown": "Unknown",
    }.get(call_status_key(record), "Unknown")


def format_call_duration_precise(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown duration"
    if 0 < float(seconds) < 1:
        return "< 1 second"
    total_seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs or not parts:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts)


def format_public_timestamp(value: str | None) -> str:
    if not value:
        return "unknown time"
    try:
        parsed = dt.datetime.fromisoformat(value)
        day = parsed.day
        return parsed.strftime(f"%A, %B {day}, %Y at %I:%M:%S %p")
    except ValueError:
        return value


def strip_xml_namespace(node: ET.Element) -> None:
    if "}" in node.tag:
        node.tag = node.tag.rsplit("}", 1)[1]
    for child in list(node):
        strip_xml_namespace(child)


@lru_cache(maxsize=None)
def load_call_icon_asset(filename: str) -> str:
    path = CALL_ICON_DIR / filename
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    strip_xml_namespace(root)
    prefix = re.sub(r"[^a-zA-Z0-9_]+", "_", path.stem)
    id_map: dict[str, str] = {}
    for node in root.iter():
        node_id = node.attrib.get("id")
        if node_id:
            new_id = f"{prefix}_{node_id}"
            id_map[node_id] = new_id
            node.set("id", new_id)
    for node in root.iter():
        for attr, value in list(node.attrib.items()):
            if value.startswith("#") and value[1:] in id_map:
                node.set(attr, f"#{id_map[value[1:]]}")
            else:
                for old_id, new_id in id_map.items():
                    value = value.replace(f"url(#{old_id})", f"url(#{new_id})")
                node.set(attr, value)
    root.attrib.pop("width", None)
    root.attrib.pop("height", None)
    root.set("focusable", "false")
    return ET.tostring(root, encoding="unicode", method="xml")


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
        return svg.replace(
            "<svg ",
            (
                f'<svg class="{class_value}" role="img" '
                f'aria-label="{html_escape(status)} {html_escape(direction)} call" '
                f'data-call-icon-source="{html_escape(filename)}" '
            ),
            1,
        )
    return fallback_call_icon_svg(status, direction)


def call_type_label(raw: dict[str, Any]) -> str:
    call_type = raw.get("ZCALLTYPE")
    face_time_data = raw.get("ZFACE_TIME_DATA")
    service_provider = raw.get("ZSERVICE_PROVIDER")
    if face_time_data or service_provider == "com.apple.FaceTime":
        return "facetime_or_data_call"
    if call_type == 1:
        return "phone_or_audio_call"
    return "unknown_private_enum"


def load_participant_map(conn: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    if not (
        table_exists(conn, "Z_2REMOTEPARTICIPANTHANDLES")
        and table_exists(conn, "ZHANDLE")
    ):
        return {}
    join_columns = table_columns(conn, "Z_2REMOTEPARTICIPANTHANDLES")
    handle_columns = table_columns(conn, "ZHANDLE")
    if not {"Z_2REMOTEPARTICIPANTCALLS", "Z_4REMOTEPARTICIPANTHANDLES"}.issubset(
        set(join_columns)
    ):
        return {}
    selected = [column for column in ("Z_PK", "ZVALUE", "ZNORMALIZEDVALUE", "ZTYPE") if column in handle_columns]
    if not selected:
        return {}
    rows = conn.execute(
        f"""
        SELECT j.Z_2REMOTEPARTICIPANTCALLS AS call_pk,
               {", ".join(f"h.{column}" for column in selected)}
        FROM Z_2REMOTEPARTICIPANTHANDLES j
        LEFT JOIN ZHANDLE h ON h.Z_PK = j.Z_4REMOTEPARTICIPANTHANDLES
        ORDER BY j.Z_2REMOTEPARTICIPANTCALLS, h.Z_PK
        """
    )
    participant_map: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        call_pk = row["call_pk"]
        participant_map.setdefault(call_pk, []).append(
            {key.lower(): value_to_jsonable(row[key]) for key in row.keys() if key != "call_pk"}
        )
    return participant_map


def load_matching_calls(conn: sqlite3.Connection, target_keys: set[str]) -> list[CallRecord]:
    call_columns = table_columns(conn, "ZCALLRECORD")
    selected_columns = [column for column in CALL_FIELDS if column in call_columns]
    if "Z_PK" not in selected_columns:
        raise RuntimeError("ZCALLRECORD.Z_PK is required")
    opt_filter = "WHERE COALESCE(Z_OPT, 1) > 0" if "Z_OPT" in call_columns else ""
    rows = conn.execute(
        f"SELECT {', '.join(selected_columns)} FROM ZCALLRECORD {opt_filter} ORDER BY ZDATE, Z_PK"
    ).fetchall()
    participant_map = load_participant_map(conn)
    records: list[CallRecord] = []
    for row in rows:
        raw = {column: value_to_jsonable(row[column]) for column in selected_columns}
        address = raw.get("ZADDRESS")
        candidate_keys = match_keys(address)
        for participant in participant_map.get(raw["Z_PK"], []):
            candidate_keys.update(match_keys(participant.get("znormalizedvalue")))
            candidate_keys.update(match_keys(participant.get("zvalue")))
        if not candidate_keys.intersection(target_keys):
            continue
        participant_rows = participant_map.get(raw["Z_PK"], [])
        utc, local = timestamp_strings(raw.get("ZDATE"))
        duration = raw.get("ZDURATION")
        try:
            duration_seconds = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_seconds = None
        records.append(
            CallRecord(
                sequence=len(records) + 1,
                raw=raw,
                timestamp_utc=utc,
                timestamp_local=local,
                duration_seconds=duration_seconds,
                normalized_address=normalize_phone_identifier(address),
                direction=direction_label(raw),
                answered_label=answered_label(raw),
                call_type_label=call_type_label(raw),
                participant_handles=[
                    str(p.get("zvalue") or "") for p in participant_rows if p.get("zvalue")
                ],
                participant_normalized_handles=[
                    str(p.get("znormalizedvalue") or "")
                    for p in participant_rows
                    if p.get("znormalizedvalue")
                ],
                participant_handle_pks=[
                    int(p["z_pk"])
                    for p in participant_rows
                    if p.get("z_pk") is not None
                ],
            )
        )
    return records


def record_to_dict(record: CallRecord) -> dict[str, Any]:
    return {
        "sequence": record.sequence,
        "z_pk": record.raw.get("Z_PK"),
        "zunique_id": record.raw.get("ZUNIQUE_ID"),
        "timestamp_utc": record.timestamp_utc,
        "timestamp_local": record.timestamp_local,
        "zdate_raw": record.raw.get("ZDATE"),
        "duration_seconds": record.duration_seconds,
        "direction": record.direction,
        "call_type_label": record.call_type_label,
        "answered_label": record.answered_label,
        "zaddress": record.raw.get("ZADDRESS"),
        "normalized_address": record.normalized_address,
        "zname": record.raw.get("ZNAME"),
        "remote_participant_handles": record.participant_handles,
        "remote_participant_normalized_handles": record.participant_normalized_handles,
        "remote_participant_handle_pks": record.participant_handle_pks,
        "conversation_id_hex": blob_hex(record.raw, "ZCONVERSATIONID"),
        "local_participant_uuid_hex": blob_hex(record.raw, "ZLOCALPARTICIPANTUUID"),
        "outgoing_local_participant_uuid_hex": blob_hex(record.raw, "ZOUTGOINGLOCALPARTICIPANTUUID"),
        "participant_group_uuid_hex": blob_hex(record.raw, "ZPARTICIPANTGROUPUUID"),
        "raw": record.raw,
    }


def write_jsonl(path: Path, records: list[CallRecord]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record_to_dict(record), ensure_ascii=False, sort_keys=True))
            fh.write("\n")


def write_csv(path: Path, records: list[CallRecord]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            data = record_to_dict(record)
            row = {field: data.get(field, "") for field in CSV_FIELDS}
            row["remote_participant_handles"] = ";".join(record.participant_handles)
            row["remote_participant_normalized_handles"] = ";".join(
                record.participant_normalized_handles
            )
            row["remote_participant_handle_pks"] = ";".join(
                str(pk) for pk in record.participant_handle_pks
            )
            row["raw_json"] = json.dumps(data["raw"], ensure_ascii=False, sort_keys=True)
            writer.writerow(row)


def write_schema(conn: sqlite3.Connection, path: Path) -> None:
    rows = conn.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE sql IS NOT NULL AND type IN ('table', 'index', 'trigger', 'view')
        ORDER BY type, name
        """
    ).fetchall()
    path.write_text("\n\n".join(row["sql"] + ";" for row in rows) + "\n", encoding="utf-8")


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall():
        table = row["name"]
        counts[table] = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
    return counts


def active_call_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    columns = table_columns(conn, "ZCALLRECORD")
    opt_filter = "WHERE COALESCE(Z_OPT, 1) > 0" if "Z_OPT" in columns else ""
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count, MIN(ZDATE) AS min_date, MAX(ZDATE) AS max_date
        FROM ZCALLRECORD {opt_filter}
        """
    ).fetchone()
    min_utc, min_local = timestamp_strings(row["min_date"])
    max_utc, max_local = timestamp_strings(row["max_date"])
    return {
        "active_call_count": row["count"],
        "first_utc": min_utc,
        "first_local": min_local,
        "last_utc": max_utc,
        "last_local": max_local,
    }


def primarykey_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not table_exists(conn, "Z_PRIMARYKEY"):
        return []
    return [
        {key: value_to_jsonable(row[key]) for key in row.keys()}
        for row in conn.execute("SELECT * FROM Z_PRIMARYKEY ORDER BY Z_ENT").fetchall()
    ]


def write_report(
    path: Path,
    *,
    label: str,
    copied_db_path: Path,
    source_hash: str,
    copied_hash: str,
    records: list[CallRecord],
    target_key_count: int,
    summary: dict[str, Any],
    counts: dict[str, int],
    primarykeys: list[dict[str, Any]],
    export_started: str,
    acquisition_command: str,
    source_description: str,
) -> None:
    call_primary = next(
        (row for row in primarykeys if str(row.get("Z_NAME")) == "CallRecord"),
        None,
    )
    lines = [
        "# Call History Export Report",
        "",
        "This report is a technical evidence-preparation aid, not legal advice.",
        "",
        "## Evidence Source",
        "",
        f"- Label: {label}",
        f"- Export started: `{export_started}`",
        f"- Source description: {source_description}",
        f"- Acquisition command/process: `{acquisition_command}`",
        f"- Export queried copied source database: `{copied_db_path}`",
        f"- Original source SHA-256 before copy: `{source_hash}`",
        f"- Copied source SHA-256: `{copied_hash}`",
            "- The exporter queried only the supplied database copy; it did not query a live device or cloud service.",
        "",
        "## Matching Logic",
        "",
        "- Match keys were derived from normalized handles in the supplied `messages.jsonl`.",
        f"- Target match key count: `{target_key_count}`",
        f"- Matched active call records exported: `{len(records)}`",
        "- Phone numbers are redacted in this Markdown report; full values remain in CSV/JSONL.",
        "",
        "## Database Summary",
        "",
        f"- Active `ZCALLRECORD` rows: `{summary['active_call_count']}`",
        f"- First active call UTC: `{summary['first_utc']}`",
        f"- Last active call UTC: `{summary['last_utc']}`",
        f"- First active call local: `{summary['first_local']}`",
        f"- Last active call local: `{summary['last_local']}`",
        "",
        "### Table Counts",
        "",
    ]
    for table, count in counts.items():
        lines.append(f"- `{table}`: `{count}`")
    lines.extend(["", "### Z_PRIMARYKEY", ""])
    if primarykeys:
        for row in primarykeys:
            name = row.get("Z_NAME")
            max_value = row.get("Z_MAX")
            lines.append(f"- `{name}`: max `{max_value}`")
    else:
        lines.append("- `Z_PRIMARYKEY` table was not present.")
    if call_primary and call_primary.get("Z_MAX") is not None:
        lines.extend(
            [
                "",
                "### Gap Signal",
                "",
                (
                    f"- `Z_PRIMARYKEY.CallRecord` max is `{call_primary.get('Z_MAX')}` while "
                    f"active `ZCALLRECORD` rows are `{summary['active_call_count']}`. "
                    "Active rows may not represent all historical calls."
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## Metadata Interpretation",
            "",
            "- `ZDATE` is interpreted as an Apple epoch timestamp (`ZDATE + 978307200`).",
            "- `ZDURATION` is preserved as seconds.",
            "- `ZADDRESS` is the remote number or identifier.",
            "- `ZORIGINATED` is treated as an outgoing/incoming direction flag.",
            (
                "- `ZANSWERED` is preserved as an answered/unanswered-style private enum, "
                "but public PDF call status treats any positive `ZDURATION` as a completed call."
            ),
            "- `ZUNIQUE_ID` is preserved as a deduplication/authentication identifier.",
            f"- {PRIVATE_ENUM_NOTE}",
            "",
            "## Matched Calls",
            "",
        ]
    )
    if not records:
        lines.append("- No matched call records were exported.")
    for record in records:
        remote = mask_phone_number(record.raw.get("ZADDRESS"))
        participants = ", ".join(mask_phone_number(value) for value in record.participant_handles)
        if not participants:
            participants = "none linked"
        name = record.raw.get("ZNAME") or ""
        name_text = f", name `{name}`" if name else ""
        lines.append(
            f"- #{record.sequence}: row `{record.raw.get('Z_PK')}`, UTC `{record.timestamp_utc}`, "
            f"{record.direction}, {record.answered_label}, duration `{record.duration_seconds}`, "
            f"remote `{remote}`{name_text}, linked handles `{participants}`"
        )
    lines.extend(
        [
            "",
            "## Process Notes and Limitations",
            "",
            (
                "- The supplied call-history database may have been updated by device, sync, "
                "backup, or retention activity. Its acquisition history must be documented separately. "
                "Any causal explanation is an inference, not proven by this database alone."
            ),
            "- Free-page, WAL, and deleted-record recovery are outside this export pass.",
            "- `schema.sql` preserves the SQLite schema for legal and forensic review.",
            "- `source_manifest.sha256` hashes the copied source database and generated outputs.",
            "",
            "## References",
            "",
            (
                "- Practical Mobile Forensics notes `CallHistory.storedata` and `ZCALLRECORD` "
                "as iOS call-history sources and cautions that active databases can retain "
                "limited visible rows: "
                "https://www.oreilly.com/library/view/practical-mobile-forensics/9781838647520/884428ff-4711-4a8b-934e-acfc249c7ec0.xhtml"
            ),
            (
                "- DFIR Review discusses missing-record analysis and `Z_PRIMARYKEY` gaps in "
                "`CallHistory.storedata`: https://dfir.pubpub.org/pub/33vkc2ul"
            ),
            (
                "- n0fate documents macOS `~/Library/Application Support/CallHistoryDB/"
                "CallHistory.storedata` as a local continuity/call-history artifact: "
                "https://forensic.n0fate.com/2014-09-11/yosemite-forensic-artifacts"
            ),
            (
                "- Apple documents high-level call record type concepts but not this private "
                "SQLite schema: https://developer.apple.com/documentation/intents/incallrecordtype/received"
            ),
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def call_history_card_html(
    record: CallRecord,
    label: str,
    source_label: str = PUBLIC_CALL_SOURCE_LABEL,
) -> str:
    status = call_status_key(record)
    direction = call_direction_key(record)
    participant = str(record.raw.get("ZNAME") or label or "contact").strip() or "contact"
    direction_text = call_direction_phrase(record, participant)
    status_text = call_status_text(record, label)
    duration_html = ""
    if (record.duration_seconds or 0) > 0:
        duration_html = (
            '<div class="call-duration">'
            '<span class="call-duration-label">Duration:</span> '
            f'<span class="call-duration-value">{html_escape(format_call_duration_precise(record.duration_seconds))}</span>'
            "</div>"
        )
    if status == "answered":
        title_html = (
            f'<span class="call-status-word call-status-word-{status}">{html_escape(status_text)}</span>'
            f"{call_icon_svg(status, direction)}"
        )
    else:
        title_html = (
            f'<span class="call-status-word call-status-word-{status}">{html_escape(status_text)}</span>'
            f"{call_icon_svg(status, direction)}"
            f"<span>{html_escape(direction_text)}</span>"
        )
    row = record.raw.get("Z_PK") if record.raw.get("Z_PK") is not None else "unknown"
    utc = record.timestamp_utc or "unknown"
    footer = (
        f"UTC: {utc} · Call row: {row} · Source: {source_label} · "
        "Preserved in call_records.jsonl"
    )
    return f"""
<section class="call-timeline-item" data-call-sequence="{record.sequence}" data-call-status="{status}" data-call-direction="{direction}">
  <div class="call-public-timestamp">{html_escape(format_public_timestamp(record.timestamp_local))}</div>
  <article class="call-card call-status-{status}">
    <div class="call-title">
      {title_html}
    </div>
    {duration_html}
    <div class="call-footer">{html_escape(footer)}</div>
  </article>
</section>"""


def write_call_history_html(
    path: Path,
    records: list[CallRecord],
    label: str,
    export_started: str,
    source_hash: str,
    source_label: str = PUBLIC_CALL_SOURCE_LABEL,
) -> None:
    cards = "\n".join(
        call_history_card_html(record, label, source_label) for record in records
    )
    if not cards:
        cards = '<p class="empty-state">No matched call records were exported.</p>'
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Call History Evidence - {html_escape(label)}</title>
  <style>
    @page {{
      size: Letter;
      margin: 0.58in 0.52in;
    }}
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #68717d;
      --hairline: #dfe4ea;
      --panel: #ffffff;
      --paper: #f6f7f8;
      --green: #1f8a57;
      --blue: #2f6f8f;
      --red: #c6413d;
      --amber: #a66a10;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 12px;
      line-height: 1.45;
    }}
    main {{
      max-width: 7.35in;
      margin: 0 auto;
    }}
    header {{
      border-bottom: 1px solid var(--hairline);
      margin-bottom: 22px;
      padding-bottom: 12px;
    }}
    h1 {{
      margin: 0 0 5px;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0;
    }}
    .meta {{
      color: var(--muted);
      font-size: 10.5px;
    }}
    .timeline {{
      display: flex;
      flex-direction: column;
      gap: 15px;
    }}
    .call-timeline-item {{
      break-inside: avoid;
      page-break-inside: avoid;
      display: flex;
      flex-direction: column;
      align-items: center;
    }}
    .call-public-timestamp {{
      color: var(--muted);
      font-size: 10.5px;
      margin: 0 0 5px;
      text-align: center;
    }}
    .call-card {{
      width: min(6.65in, 100%);
      background: var(--panel);
      border: 1px solid var(--hairline);
      border-left: 4px solid #8a949f;
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
      overflow: hidden;
    }}
    .call-status-answered {{
      border-left-color: var(--green);
    }}
    .call-status-missed,
    .call-status-unanswered {{
      border-left-color: var(--red);
    }}
    .call-status-canceled {{
      border-left-color: var(--amber);
    }}
    .call-title {{
      display: flex;
      align-items: center;
      justify-content: flex-start;
      flex-wrap: nowrap;
      gap: 5px;
      padding: 13px 18px 0;
      font-size: 14px;
      font-weight: 650;
      text-align: left;
      white-space: nowrap;
    }}
    .call-status-word-answered {{
      color: var(--green);
    }}
    .call-status-word-missed,
    .call-status-word-unanswered {{
      color: var(--red);
    }}
    .call-status-word-canceled {{
      color: var(--amber);
    }}
    .call-status-word-unknown {{
      color: #5f6874;
    }}
    .call-state-icon {{
      flex: 0 0 auto;
      width: 34px;
      height: 25px;
    }}
    .call-state-icon .call-phone {{
      fill: #111111;
    }}
    .call-state-icon-green {{
      color: var(--green);
    }}
    .call-state-icon-blue {{
      color: var(--blue);
    }}
    .call-state-icon-red {{
      color: var(--red);
    }}
    .call-state-icon-amber {{
      color: var(--amber);
    }}
    .call-state-icon-muted {{
      color: #7b8490;
    }}
    .call-duration {{
      margin: 4px 18px 0;
      font-size: 12px;
      text-align: left;
    }}
    .call-duration-label {{
      color: #4b5563;
      font-weight: 400;
    }}
    .call-duration-value {{
      color: #111827;
      font-weight: 600;
    }}
    .call-footer {{
      margin-top: 12px;
      border-top: 1px solid var(--hairline);
      background: #f8f9fa;
      color: var(--muted);
      font-size: 8.6px;
      line-height: 1.2;
      padding: 7px 12px;
      text-align: left;
      white-space: nowrap;
    }}
    .empty-state {{
      color: var(--muted);
      text-align: center;
      margin-top: 1in;
    }}
    @media print {{
      body {{
        background: #ffffff;
      }}
      main {{
        max-width: none;
      }}
      .call-card {{
        box-shadow: none;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Call History Evidence</h1>
      <div class="meta">Label: {html_escape(label)} · Export started: {html_escape(export_started)} · Source SHA-256: {html_escape(source_hash)}</div>
    </header>
    <div class="timeline">
      {cards}
    </div>
  </main>
</body>
</html>
"""
    path.write_text(html_text, encoding="utf-8")


def render_call_history_pdf(html_path: Path, pdf_path: Path) -> tuple[bool, str]:
    renderer = ROOT / "render_call_history_pdf.mjs"
    if not renderer.exists():
        return False, "render_call_history_pdf.mjs is missing"
    try:
        result = subprocess.run(
            ["node", str(renderer), str(html_path), str(pdf_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        return False, details or f"renderer exited {result.returncode}"
    return True, (result.stdout or "").strip()


def write_manifest(path: Path, output_dir: Path) -> None:
    entries: list[tuple[str, str]] = []
    for file_path in sorted(p for p in output_dir.rglob("*") if p.is_file()):
        if file_path == path:
            continue
        entries.append((sha256_file(file_path), str(file_path.relative_to(output_dir))))
    path.write_text("".join(f"{digest}  {name}\n" for digest, name in entries), encoding="utf-8")


def clean_output(output_dir: Path) -> None:
    for relative in (
        "call_records.csv",
        "call_records.jsonl",
        "call_history.html",
        "call_history.pdf",
        "call_history_report.md",
        "source_manifest.sha256",
        "schema.sql",
    ):
        path = output_dir / relative
        if path.exists() and path.is_file():
            path.unlink()
    source_dir = output_dir / "source"
    if source_dir.exists():
        shutil.rmtree(source_dir)


def copy_source_database(source_db: Path, output_dir: Path) -> Path:
    source_dir = output_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    copied = source_dir / "call_history_copy.db"
    shutil.copy2(source_db, copied)
    return copied


def export_call_history(args: argparse.Namespace) -> int:
    if hasattr(args, "timezone"):
        configure_timezone(args.timezone)
    source_db = expand_path(args.call_db)
    messages_jsonl = expand_path(args.messages_jsonl)
    output_dir = expand_path(args.out)
    if not source_db.exists():
        raise FileNotFoundError(source_db)
    if not messages_jsonl.exists():
        raise FileNotFoundError(messages_jsonl)

    output_dir.mkdir(parents=True, exist_ok=True)
    clean_output(output_dir)

    export_started = dt.datetime.now(tz=LOCAL_TZ).isoformat(timespec="seconds")
    source_hash = sha256_file(source_db)
    copied_db = copy_source_database(source_db, output_dir)
    copied_hash = sha256_file(copied_db)
    if source_hash != copied_hash:
        raise RuntimeError("source database hash changed during copy")

    target_keys = load_message_match_keys(messages_jsonl)
    if not target_keys:
        raise RuntimeError("no matchable handles found in messages JSONL")

    with closing(connect_db(copied_db)) as conn:
        records = load_matching_calls(conn, target_keys)
        write_schema(conn, output_dir / "schema.sql")
        counts = table_counts(conn)
        summary = active_call_summary(conn)
        primarykeys = primarykey_summary(conn)

    write_csv(output_dir / "call_records.csv", records)
    write_jsonl(output_dir / "call_records.jsonl", records)
    write_call_history_html(
        output_dir / "call_history.html",
        records,
        args.label,
        export_started,
        source_hash,
        getattr(args, "source_description", PUBLIC_CALL_SOURCE_LABEL),
    )
    if not getattr(args, "no_pdf", False):
        rendered, message = render_call_history_pdf(
            output_dir / "call_history.html",
            output_dir / "call_history.pdf",
        )
        if not rendered:
            print(f"Warning: call_history.pdf was not rendered: {message}", file=sys.stderr)
    write_report(
        output_dir / "call_history_report.md",
        label=args.label,
        copied_db_path=copied_db,
        source_hash=source_hash,
        copied_hash=copied_hash,
        records=records,
        target_key_count=len(target_keys),
        summary=summary,
        counts=counts,
        primarykeys=primarykeys,
        export_started=export_started,
        acquisition_command=getattr(args, "acquisition_command", ACQUISITION_COMMAND),
        source_description=getattr(args, "source_description", PUBLIC_CALL_SOURCE_LABEL),
    )
    write_manifest(output_dir / "source_manifest.sha256", output_dir)
    print(f"Exported {len(records)} matched call records to {output_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="call_history_exporter",
        description="Export matched CallHistory.storedata rows as an evidence package.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export", help="Export matched call records.")
    export_parser.add_argument("--call-db", required=True, help="Path to copied CallHistory DB.")
    export_parser.add_argument(
        "--messages-jsonl",
        required=True,
        help="Path to existing iMessage messages.jsonl for handle matching.",
    )
    export_parser.add_argument("--label", required=True, help="Human-facing contact label.")
    export_parser.add_argument("--out", required=True, help="Output package directory.")
    export_parser.add_argument(
        "--timezone",
        help="IANA timezone for local timestamps (default: detected system timezone).",
    )
    export_parser.add_argument(
        "--acquisition-command",
        default=ACQUISITION_COMMAND,
        help="Acquisition command or process to document in the Markdown report.",
    )
    export_parser.add_argument(
        "--source-description",
        default=PUBLIC_CALL_SOURCE_LABEL,
        help="Short source description to document in the Markdown report.",
    )
    export_parser.add_argument("--no-pdf", action="store_true", help="Skip Playwright PDF rendering.")
    export_parser.set_defaults(func=export_call_history)
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
    sys.exit(main())
