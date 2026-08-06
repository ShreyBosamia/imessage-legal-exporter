import csv
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from zoneinfo import ZoneInfo

import call_history_exporter as exporter

exporter.LOCAL_TZ = ZoneInfo("America/Los_Angeles")


class CallHistoryExporterTest(unittest.TestCase):
    def test_apple_timestamp_conversion(self):
        utc, local = exporter.timestamp_strings(0)

        self.assertEqual(utc, "2001-01-01T00:00:00Z")
        self.assertTrue(local.startswith("2000-12-31T16:00:00"))

    def test_phone_normalization_matches_us_variants(self):
        self.assertEqual(exporter.normalize_phone_identifier("(202) 555-0101"), "12025550101")
        self.assertIn("2025550101", exporter.match_keys("+1 202 555 0101"))

    def test_call_type_label(self):
        # FaceTime from ZFACE_TIME_DATA
        self.assertEqual(exporter.call_type_label({"ZFACE_TIME_DATA": 1}), "facetime_or_data_call")
        # FaceTime from ZSERVICE_PROVIDER
        self.assertEqual(
            exporter.call_type_label({"ZSERVICE_PROVIDER": "com.apple.FaceTime"}),
            "facetime_or_data_call",
        )
        # Telephony phone call
        self.assertEqual(exporter.call_type_label({"ZCALLTYPE": 1}), "phone_or_audio_call")
        # Unknown type
        self.assertEqual(exporter.call_type_label({}), "unknown_private_enum")

    def test_export_preserves_raw_values_and_redacts_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "call_history_copy.db"
            messages_jsonl = root / "messages.jsonl"
            out = root / "out"
            self._create_call_db(db)
            messages_jsonl.write_text(
                json.dumps({"handle": "+1 (202) 555-0101", "text": "private"}) + "\n",
                encoding="utf-8",
            )

            status = exporter.export_call_history(
                Namespace(
                    call_db=str(db),
                    messages_jsonl=str(messages_jsonl),
                    label="Example Contact",
                    out=str(out),
                    no_pdf=True,
                )
            )

            self.assertEqual(status, 0)
            self.assertTrue((out / "source" / "call_history_copy.db").exists())
            self.assertTrue((out / "schema.sql").exists())
            self.assertTrue((out / "source_manifest.sha256").exists())

            with (out / "call_records.csv").open(encoding="utf-8-sig") as fh:
                csv_rows = list(csv.DictReader(fh))
            self.assertEqual(len(csv_rows), 1)
            self.assertEqual(csv_rows[0]["zaddress"], "+12025550101")
            self.assertEqual(csv_rows[0]["normalized_address"], "12025550101")
            self.assertEqual(csv_rows[0]["direction"], "outgoing")
            self.assertEqual(csv_rows[0]["answered_label"], "answered")
            self.assertEqual(csv_rows[0]["zname"], "Example Contact")
            self.assertEqual(csv_rows[0]["local_participant_uuid_hex"], "010203")
            self.assertIn('"ZDISCONNECTED_CAUSE": 42', csv_rows[0]["raw_json"])
            self.assertIn("2025550101", csv_rows[0]["remote_participant_normalized_handles"])

            json_rows = [
                json.loads(line)
                for line in (out / "call_records.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(json_rows), 1)
            self.assertEqual(json_rows[0]["zaddress"], "+12025550101")
            self.assertEqual(json_rows[0]["raw"]["ZFILTERED_OUT_REASON"], 8)
            self.assertEqual(json_rows[0]["raw"]["ZLOCALPARTICIPANTUUID"], "010203")

            report = (out / "call_history_report.md").read_text(encoding="utf-8")
            self.assertIn("Example Contact", report)
            self.assertIn(exporter.ACQUISITION_COMMAND, report)
            self.assertIn("Apple does not publicly document", report)
            self.assertIn("sync, backup, or retention activity", report)
            self.assertIn("inference, not proven", report)
            self.assertIn("`Z_PRIMARYKEY.CallRecord` max is `2252`", report)
            self.assertNotIn("+12025550101", report)
            self.assertNotIn("2025550101", report)
            self.assertIn("***0101", report)

            manifest = (out / "source_manifest.sha256").read_text(encoding="utf-8")
            self.assertIn("source/call_history_copy.db", manifest)
            self.assertIn("call_records.csv", manifest)
            self.assertIn("call_records.jsonl", manifest)
            self.assertIn("call_history.html", manifest)
            self.assertIn("call_history_report.md", manifest)
            self.assertNotIn("call_history.pdf", manifest)

            html = (out / "call_history.html").read_text(encoding="utf-8")
            self.assertIn("Sunday, December 31, 2000 at 04:00:10 PM", html)
            self.assertIn(
                '<span class="call-status-word call-status-word-answered">Outgoing phone call to Example Contact</span>',
                html,
            )
            self.assertIn('<span class="call-duration-label">Duration:</span> <span class="call-duration-value">1 minute 2 seconds</span>', html)
            self.assertIn(
                "UTC: 2001-01-01T00:00:10Z · Call row: 100 · Source: "
                "Supplied CallHistory.storedata database · Preserved in call_records.jsonl",
                html,
            )
            self.assertIn("call-state-icon-answered", html)
            self.assertIn("call-state-icon-outgoing", html)
            self.assertIn("width: min(6.65in, 100%);", html)
            self.assertIn("justify-content: flex-start;", html)
            self.assertIn("white-space: nowrap;", html)
            self.assertNotIn("+12025550101", html)
            self.assertNotIn("***0101", html)
            self.assertNotIn("ZCALLTYPE", html)
            self.assertNotIn("ZANSWERED", html)
            self.assertNotIn("ZDURATION", html)
            self.assertNotIn("ZUNIQUE_ID", html)

    def test_no_unmatched_records_are_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "call_history_copy.db"
            messages_jsonl = root / "messages.jsonl"
            out = root / "out"
            self._create_call_db(db)
            messages_jsonl.write_text(json.dumps({"handle": "+1 202 555 0199"}) + "\n")

            exporter.export_call_history(
                Namespace(
                    call_db=str(db),
                    messages_jsonl=str(messages_jsonl),
                    label="Synthetic",
                    out=str(out),
                    no_pdf=True,
                )
            )

            self.assertEqual((out / "call_records.jsonl").read_text(encoding="utf-8"), "")
            with (out / "call_records.csv").open(encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(rows, [])

    def test_display_helpers_map_status_title_icon_and_precise_duration(self):
        answered_incoming = self._record(direction="incoming", answered_label="answered", duration=52)
        answered_outgoing = self._record(direction="outgoing", answered_label="answered", duration=3905)
        missed = self._record(direction="incoming", answered_label="unanswered_or_missed", duration=0)
        canceled = self._record(direction="outgoing", answered_label="unanswered_or_missed", duration=0)
        duration_completed = self._record(
            direction="outgoing", answered_label="unanswered_or_missed", duration=7071.708137989044
        )
        facetime_completed = self._record(
            direction="incoming",
            answered_label="answered",
            duration=1200,
            call_type_label="facetime_audio_call",
        )

        self.assertEqual(exporter.call_status_key(answered_incoming), "answered")
        self.assertEqual(exporter.call_status_key(answered_outgoing), "answered")
        self.assertEqual(exporter.call_status_key(missed), "missed")
        self.assertEqual(exporter.call_status_key(canceled), "canceled")
        self.assertEqual(exporter.call_status_key(duration_completed), "answered")
        self.assertEqual(
            exporter.call_title(duration_completed, "Fallback Name"),
            "Outgoing phone call to Example Contact",
        )
        self.assertEqual(
            exporter.call_title(facetime_completed, "Fallback Name"),
            "Incoming FaceTime call from Example Contact",
        )
        self.assertEqual(
            exporter.call_title(missed, "Fallback Name"),
            "Missed Incoming phone call from Example Contact",
        )
        self.assertEqual(
            exporter.call_title(canceled, "Fallback Name"),
            "Canceled Outgoing phone call to Example Contact",
        )
        self.assertEqual(exporter.format_call_duration_precise(0.42), "< 1 second")
        self.assertEqual(exporter.format_call_duration_precise(52), "52 seconds")
        self.assertEqual(exporter.format_call_duration_precise(75), "1 minute 15 seconds")
        self.assertEqual(
            exporter.format_call_duration_precise(3905),
            "1 hour 5 minutes 5 seconds",
        )
        self.assertEqual(
            exporter.format_call_duration_precise(7071.708137989044),
            "1 hour 57 minutes 52 seconds",
        )
        self.assertIn("call-state-icon-answered", exporter.call_icon_svg("answered", "incoming"))
        self.assertIn("call-state-icon-green", exporter.call_icon_svg("answered", "incoming"))
        self.assertIn(
            'data-call-icon-source="call_icon_review_answered-incoming.svg"',
            exporter.call_icon_svg("answered", "incoming"),
        )
        self.assertIn("call-state-icon-green", exporter.call_icon_svg("answered", "outgoing"))
        self.assertNotIn("call-state-icon-blue", exporter.call_icon_svg("answered", "outgoing"))
        self.assertIn(
            'data-call-icon-source="call_icon_review_answered-outgoing.svg"',
            exporter.call_icon_svg("answered", "outgoing"),
        )
        self.assertIn("call-state-icon-red", exporter.call_icon_svg("missed", "incoming"))
        self.assertIn(
            'data-call-icon-source="call_icon_review_missed-incoming.svg"',
            exporter.call_icon_svg("missed", "incoming"),
        )
        self.assertIn("call-state-icon-amber", exporter.call_icon_svg("canceled", "outgoing"))
        self.assertNotIn("call-state-icon-red", exporter.call_icon_svg("canceled", "outgoing"))
        self.assertIn(
            'data-call-icon-source="call_icon_review_canceled-outgoing.svg"',
            exporter.call_icon_svg("canceled", "outgoing"),
        )
        self.assertIn("call-state-icon-red", exporter.call_icon_svg("unanswered", "outgoing"))
        self.assertNotIn("call-state-icon-amber", exporter.call_icon_svg("unanswered", "outgoing"))
        self.assertIn(
            'data-call-icon-source="call_icon_review_missed-outgoing.svg"',
            exporter.call_icon_svg("unanswered", "outgoing"),
        )
        self.assertIn("#a66a10", exporter.call_icon_svg("canceled", "outgoing"))
        self.assertIn("#c6413d", exporter.call_icon_svg("unanswered", "outgoing"))
        self.assertNotIn("call-state-badge", exporter.call_icon_svg("answered", "incoming"))
        self.assertNotIn('stroke="#fff"', exporter.call_icon_svg("missed", "incoming"))

    def test_public_html_duration_for_completed_calls(self):
        records = [
            self._record(
                sequence=1,
                pk=201,
                direction="incoming",
                answered_label="answered",
                duration=3905,
            ),
            self._record(
                sequence=2,
                pk=202,
                direction="incoming",
                answered_label="unanswered_or_missed",
                duration=0,
            ),
            self._record(
                sequence=3,
                pk=203,
                direction="outgoing",
                answered_label="unanswered_or_missed",
                duration=0,
            ),
            self._record(
                sequence=4,
                pk=204,
                direction="outgoing",
                answered_label="unanswered_or_missed",
                duration=7071.708137989044,
            ),
        ]
        html = "\n".join(exporter.call_history_card_html(record, "Fallback") for record in records)

        self.assertIn('class="call-title"', html)
        self.assertIn(
            '<span class="call-status-word call-status-word-answered">Incoming phone call from Example Contact</span>',
            html,
        )
        self.assertIn(
            '<span class="call-status-word call-status-word-answered">Outgoing phone call to Example Contact</span>',
            html,
        )
        self.assertIn(
            '<span class="call-status-word call-status-word-missed">Missed</span>',
            html,
        )
        self.assertIn(
            '<span class="call-status-word call-status-word-canceled">Canceled</span>',
            html,
        )
        self.assertEqual(html.count("Duration:"), 2)
        self.assertIn('<span class="call-duration-label">Duration:</span> <span class="call-duration-value">1 hour 5 minutes 5 seconds</span>', html)
        self.assertIn('<span class="call-duration-label">Duration:</span> <span class="call-duration-value">1 hour 57 minutes 52 seconds</span>', html)
        self.assertIn('data-call-status="missed" data-call-direction="incoming"', html)
        self.assertIn('data-call-status="canceled" data-call-direction="outgoing"', html)
        self.assertNotIn("+12025550101", html)
        self.assertNotIn("***0101", html)
        self.assertNotIn("ZCALLTYPE", html)
        self.assertNotIn("ZANSWERED", html)
        self.assertNotIn("ZDURATION", html)
        self.assertNotIn("ZUNIQUE_ID", html)

    def test_manifest_includes_call_history_pdf_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "call_history.html").write_text("<html></html>", encoding="utf-8")
            (root / "call_history.pdf").write_bytes(b"%PDF-1.4\n")

            exporter.write_manifest(root / "source_manifest.sha256", root)

            manifest = (root / "source_manifest.sha256").read_text(encoding="utf-8")
            self.assertIn("call_history.html", manifest)
            self.assertIn("call_history.pdf", manifest)

    def _create_call_db(self, db: Path) -> None:
        with sqlite3.connect(db) as conn:
            conn.executescript(
                """
                CREATE TABLE ZCALLRECORD (
                    Z_PK INTEGER PRIMARY KEY,
                    Z_ENT INTEGER,
                    Z_OPT INTEGER,
                    ZANSWERED INTEGER,
                    ZCALL_CATEGORY INTEGER,
                    ZCALLTYPE INTEGER,
                    ZDISCONNECTED_CAUSE INTEGER,
                    ZFACE_TIME_DATA INTEGER,
                    ZFILTERED_OUT_REASON INTEGER,
                    ZJUNKCONFIDENCE INTEGER,
                    ZNUMBER_AVAILABILITY INTEGER,
                    ZORIGINATED INTEGER,
                    ZREAD INTEGER,
                    ZVERIFICATIONSTATUS INTEGER,
                    ZDATE TIMESTAMP,
                    ZDURATION FLOAT,
                    ZADDRESS VARCHAR,
                    ZISO_COUNTRY_CODE VARCHAR,
                    ZJUNKIDENTIFICATIONCATEGORY VARCHAR,
                    ZLOCATION VARCHAR,
                    ZNAME VARCHAR,
                    ZSERVICE_PROVIDER VARCHAR,
                    ZUNIQUE_ID VARCHAR,
                    ZCONVERSATIONID BLOB,
                    ZLOCALPARTICIPANTUUID BLOB,
                    ZOUTGOINGLOCALPARTICIPANTUUID BLOB,
                    ZPARTICIPANTGROUPUUID BLOB
                );
                CREATE TABLE ZHANDLE (
                    Z_PK INTEGER PRIMARY KEY,
                    Z_ENT INTEGER,
                    Z_OPT INTEGER,
                    ZTYPE INTEGER,
                    ZNORMALIZEDVALUE VARCHAR,
                    ZVALUE VARCHAR
                );
                CREATE TABLE Z_2REMOTEPARTICIPANTHANDLES (
                    Z_2REMOTEPARTICIPANTCALLS INTEGER,
                    Z_4REMOTEPARTICIPANTHANDLES INTEGER
                );
                CREATE TABLE Z_PRIMARYKEY (
                    Z_ENT INTEGER PRIMARY KEY,
                    Z_NAME VARCHAR,
                    Z_SUPER INTEGER,
                    Z_MAX INTEGER
                );
                """
            )
            conn.execute(
                """
                INSERT INTO ZCALLRECORD
                (Z_PK, Z_ENT, Z_OPT, ZANSWERED, ZCALL_CATEGORY, ZCALLTYPE,
                 ZDISCONNECTED_CAUSE, ZFACE_TIME_DATA, ZFILTERED_OUT_REASON,
                 ZJUNKCONFIDENCE, ZNUMBER_AVAILABILITY, ZORIGINATED, ZREAD,
                 ZVERIFICATIONSTATUS, ZDATE, ZDURATION, ZADDRESS, ZISO_COUNTRY_CODE,
                 ZJUNKIDENTIFICATIONCATEGORY, ZLOCATION, ZNAME, ZSERVICE_PROVIDER,
                 ZUNIQUE_ID, ZCONVERSATIONID, ZLOCALPARTICIPANTUUID,
                 ZOUTGOINGLOCALPARTICIPANTUUID, ZPARTICIPANTGROUPUUID)
                VALUES
                (100, 2, 1, 1, 7, 1, 42, 0, 8, 3, 4, 1, 1, 9, 10, 61.5,
                 '+12025550101', 'US', 'category', 'Washington',
                 'Example Contact', 'Carrier', 'unique-call',
                 X'AA55', X'010203', X'040506', X'070809')
                """
            )

            conn.execute(
                """
                INSERT INTO ZCALLRECORD
                (Z_PK, Z_ENT, Z_OPT, ZANSWERED, ZCALL_CATEGORY, ZCALLTYPE,
                 ZDISCONNECTED_CAUSE, ZFACE_TIME_DATA, ZFILTERED_OUT_REASON,
                 ZORIGINATED, ZDATE, ZDURATION, ZADDRESS, ZNAME, ZUNIQUE_ID)
                VALUES
                (101, 2, 1, 0, 1, 16, 0, 1, 0, 0, 20, 2, '+12025550103',
                 'Other Person', 'other-call')
                """
            )
            conn.execute(
                """
                INSERT INTO ZHANDLE
                (Z_PK, Z_ENT, Z_OPT, ZTYPE, ZNORMALIZEDVALUE, ZVALUE)
                VALUES (5, 4, 1, 1, '2025550101', '+12025550101')
                """
            )
            conn.execute(
                """
                INSERT INTO Z_2REMOTEPARTICIPANTHANDLES
                (Z_2REMOTEPARTICIPANTCALLS, Z_4REMOTEPARTICIPANTHANDLES)
                VALUES (100, 5)
                """
            )
            conn.execute(
                """
                INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_SUPER, Z_MAX)
                VALUES (2, 'CallRecord', 0, 2252)
                """
            )

    def _record(
        self,
        *,
        sequence: int = 1,
        pk: int = 1234,
        direction: str,
        answered_label: str,
        duration: float,
        call_type_label: str = "phone_or_audio_call",
    ) -> exporter.CallRecord:
        return exporter.CallRecord(
            sequence=sequence,
            raw={"Z_PK": pk, "ZNAME": "Example Contact", "ZADDRESS": "+12025550101"},
            timestamp_utc="2026-03-03T23:04:00Z",
            timestamp_local="2026-03-03T15:04:00-08:00",
            duration_seconds=duration,
            normalized_address="12025550101",
            direction=direction,
            answered_label=answered_label,
            call_type_label=call_type_label,
            participant_handles=["+12025550101"],
            participant_normalized_handles=["2025550101"],
            participant_handle_pks=[5],
        )


if __name__ == "__main__":
    unittest.main()
