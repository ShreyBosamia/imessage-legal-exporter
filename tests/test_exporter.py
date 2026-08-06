import csv
import json
import struct
import sqlite3
import subprocess
import tempfile
import textwrap
import unittest
import zlib
from argparse import Namespace
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import imessage_legal_exporter as exporter

exporter.LOCAL_TZ = ZoneInfo("America/Los_Angeles")


class ExporterTest(unittest.TestCase):
    def test_mask_identifier_masks_phone_numbers(self):
        self.assertEqual(exporter.mask_identifier("+12025550100"), "+12***0100")
        self.assertEqual(exporter.mask_identifier("chat123"), "chat123")

    def test_decode_body_uses_attributed_string_payload_without_metadata(self):
        root = self._fake_archived_object(
            "NSAttributedString",
            contents=[
                self._fake_typed_value([self._fake_nsstring("Clean synthetic message")]),
                self._fake_typed_value(
                    [
                        self._fake_archived_object(
                            "NSDictionary",
                            contents=[
                                self._fake_typed_value(
                                    [self._fake_nsstring("__kIMMessagePartAttributeName")]
                                )
                            ],
                        )
                    ]
                ),
            ],
        )

        body, source, status = self._decode_with_fake_typedstream(root)

        self.assertEqual(body, "Clean synthetic message")
        self.assertEqual(source, "message.attributedBody")
        self.assertEqual(status, "attributed")

    def test_decode_body_preserves_short_attributed_string_payload(self):
        root = self._fake_archived_object(
            "NSAttributedString",
            contents=[self._fake_typed_value([self._fake_nsstring("Np")])],
        )

        body, source, status = self._decode_with_fake_typedstream(root)

        self.assertEqual(body, "Np")
        self.assertEqual(source, "message.attributedBody")
        self.assertEqual(status, "attributed")

    def test_decode_body_keeps_message_text_precedence(self):
        body, source, status = exporter.decode_body("Plain text wins", b"not-a-typedstream")

        self.assertEqual(body, "Plain text wins")
        self.assertEqual(source, "message.text")
        self.assertEqual(status, "ok")

    def test_decode_body_without_typedstream_marks_attributed_body_undecoded(self):
        original_typedstream = exporter.typedstream
        try:
            exporter.typedstream = None
            body, source, status = exporter.decode_body(None, b"synthetic-archive")
        finally:
            exporter.typedstream = original_typedstream

        self.assertEqual(body, "")
        self.assertEqual(source, "message.attributedBody")
        self.assertEqual(status, "undecoded")

    def test_decode_body_uses_bounded_fallback_for_malformed_typedstream(self):
        blob = (
            b"\x04\x0bstreamtyped\x00NSAttributedString\x00NSObject\x00NSString\x00"
            b"Clean fallback message\x00NSDictionary\x00__kIMMessagePartAttributeName\x00"
            b"bplist00DDScannerResult"
        )
        body, source, status = self._decode_with_failing_typedstream(blob)

        self.assertEqual(body, "Clean fallback message")
        self.assertEqual(source, "message.attributedBody")
        self.assertEqual(status, "fallback")

    def test_decode_body_fallback_strips_archive_control_prefix_artifacts(self):
        for prefix in b"+=()*@":
            with self.subTest(prefix=chr(prefix)):
                blob = (
                    b"\x04\x0bstreamtyped\x00NSAttributedString\x00NSObject\x00NSString\x00"
                    + bytes([prefix])
                    + b"Clean fallback message\x00NSDictionary\x00"
                    b"__kIMMessagePartAttributeName\x00"
                )

                body, source, status = self._decode_with_failing_typedstream(blob)

                self.assertEqual(body, "Clean fallback message")
                self.assertEqual(source, "message.attributedBody")
                self.assertEqual(status, "fallback")

    def test_report_warns_when_typedstream_is_unavailable(self):
        original_typedstream = exporter.typedstream
        try:
            exporter.typedstream = None
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                exporter.write_report(
                    root / "extraction_report.md",
                    root / "chat.db",
                    "0" * 64,
                    "0" * 64,
                    {
                        "chat_id": 1,
                        "guid": "chat-guid",
                        "service_name": "iMessage",
                        "style": 45,
                        "handles": ["+12025550102"],
                        "first_local": "2026-01-01T00:00:00-08:00",
                        "last_local": "2026-01-01T00:00:00-08:00",
                        "messages": 1,
                    },
                    [self._message(body_status="undecoded")],
                    Namespace(label="Synthetic Contact"),
                    "2026-01-01T00:30:00-08:00",
                )
                report = (root / "extraction_report.md").read_text(encoding="utf-8")
        finally:
            exporter.typedstream = original_typedstream

        self.assertIn("### Warning", report)
        self.assertIn("`pytypedstream` is unavailable", report)

    def test_export_with_synthetic_chat_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "chat.db"
            attachments_root = root / "Library" / "Messages" / "Attachments"
            attachment_file = attachments_root / "aa" / "bb" / "photo.jpg"
            attachment_file.parent.mkdir(parents=True)
            attachment_file.write_bytes(b"fake-jpeg")
            self._create_db(db, attachment_file)

            out = root / "out"
            (out / "attachments").mkdir(parents=True)
            (out / "attachments" / "stale.bin").write_bytes(b"stale")
            (out / "derived_media").mkdir()
            (out / "derived_media" / "stale.png").write_bytes(b"stale")
            (out / "thread.pdf").write_bytes(b"stale")
            status = exporter.export_thread(
                Namespace(
                    db=str(db),
                    chat_id=1,
                    label="Synthetic Contact",
                    attachments_root=str(attachments_root),
                    out=str(out),
                    no_pdf=True,
                )
            )

            self.assertEqual(status, 0)
            self.assertTrue((out / "messages.csv").exists())
            self.assertTrue((out / "messages.jsonl").exists())
            self.assertTrue((out / "thread.html").exists())
            self.assertTrue((out / "manifest.sha256").exists())
            self.assertTrue(any((out / "attachments").iterdir()))
            self.assertFalse((out / "attachments" / "stale.bin").exists())
            self.assertFalse((out / "derived_media" / "stale.png").exists())
            report = (out / "extraction_report.md").read_text()
            self.assertIn("Exported message count: `2`", report)
            self.assertIn("Copied attachments: `1`", report)

    def test_export_includes_recovered_unjoined_outgoing_sms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "chat.db"
            attachments_root = root / "Library" / "Messages" / "Attachments"
            attachment_file = attachments_root / "aa" / "bb" / "photo.jpg"
            attachment_file.parent.mkdir(parents=True)
            attachment_file.write_bytes(b"fake-jpeg")
            self._create_db(db, attachment_file)
            orphan_date = 1500 * 1_000_000_000
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO message VALUES (3, 'sms-orphan', 'Recovered SMS', NULL, ?, 1, 1, 0, 0, 0, 0, 'SMS', 0, NULL, 0, NULL)",
                    (orphan_date,),
                )

            out = root / "out"
            status = exporter.export_thread(
                Namespace(
                    db=str(db),
                    chat_id=1,
                    label="Synthetic Contact",
                    attachments_root=str(attachments_root),
                    out=str(out),
                    no_pdf=True,
                )
            )

            self.assertEqual(status, 0)
            rows = [
                json.loads(line)
                for line in (out / "messages.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["guid"] for row in rows], ["m1", "sms-orphan", "m2"])
            self.assertEqual(rows[1]["sequence"], 2)
            self.assertEqual(rows[1]["service"], "SMS")
            self.assertEqual(rows[1]["chat_join_status"], "recovered_unjoined_sms")
            report = (out / "extraction_report.md").read_text(encoding="utf-8")
            self.assertIn("Exported message count: `3`", report)
            self.assertIn("Recovered unjoined SMS messages: `1`", report)

    def test_export_reads_thread_originator_columns_from_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "chat.db"
            attachments_root = root / "Library" / "Messages" / "Attachments"
            attachment_file = attachments_root / "aa" / "bb" / "photo.jpg"
            attachment_file.parent.mkdir(parents=True)
            attachment_file.write_bytes(b"fake-jpeg")
            self._create_db(db, attachment_file)
            with sqlite3.connect(db) as conn:
                conn.execute("ALTER TABLE message ADD COLUMN thread_originator_guid TEXT")
                conn.execute("ALTER TABLE message ADD COLUMN thread_originator_part INTEGER")
                conn.execute(
                    "UPDATE message SET thread_originator_guid = 'm1', thread_originator_part = 0 WHERE guid = 'm2'"
                )

            out = root / "out"
            status = exporter.export_thread(
                Namespace(
                    db=str(db),
                    chat_id=1,
                    label="Synthetic Contact",
                    attachments_root=str(attachments_root),
                    out=str(out),
                    no_pdf=True,
                )
            )

            self.assertEqual(status, 0)
            rows = [
                json.loads(line)
                for line in (out / "messages.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rows[1]["thread_originator_guid"], "m1")
            self.assertEqual(rows[1]["thread_originator_part"], 0)
            self.assertEqual(rows[1]["reply_target_sequence"], 1)
            self.assertEqual(rows[1]["reply_context_source"], "thread_originator_guid")

    def test_call_event_parsing_and_public_formatting(self):
        rows = [
            {
                "sequence": 1,
                "timestamp_local": "2026-03-03T14:59:00-08:00",
                "timestamp_utc": "2026-03-03T22:59:00Z",
                "duration_seconds": 0,
                "direction": "incoming",
                "answered_label": "unanswered_or_missed",
                "z_pk": 1234,
                "zaddress": "+12025550101",
                "remote_participant_handles": ["+12025550101"],
                "raw": {"ZADDRESS": "+12025550101", "ZANSWERED": 0, "ZDURATION": 0},
            },
            {
                "sequence": 2,
                "timestamp_local": "2026-03-03T15:05:00-08:00",
                "timestamp_utc": "2026-03-03T23:05:00Z",
                "duration_seconds": 1200,
                "direction": "incoming",
                "answered_label": "answered",
                "call_type_label": "facetime_audio_call",
                "z_pk": 1235,
                "raw": {"Z_PK": 1235, "ZUNIQUE_ID": "call-guid"},
            },
            {
                "sequence": 3,
                "timestamp_local": "2026-03-03T15:40:00-08:00",
                "timestamp_utc": "2026-03-03T23:40:00Z",
                "duration_seconds": 0,
                "direction": "outgoing",
                "answered_label": "unanswered_or_missed",
                "z_pk": 1236,
            },
            {
                "sequence": 4,
                "timestamp_local": "2026-03-03T15:42:00-08:00",
                "timestamp_utc": "2026-03-03T23:42:00Z",
                "duration_seconds": 7071.708137989044,
                "direction": "outgoing",
                "answered_label": "unanswered_or_missed",
                "z_pk": 1237,
            },
        ]

        events = [exporter.call_event_from_dict(row, index + 1) for index, row in enumerate(rows)]
        html = "".join(exporter.call_event_html(event, "Example Contact") for event in events)

        self.assertEqual(exporter.call_status_key(events[0]), "missed")
        self.assertEqual(exporter.call_status_key(events[1]), "answered")
        self.assertEqual(exporter.call_status_key(events[2]), "canceled")
        self.assertEqual(exporter.call_status_key(events[3]), "answered")
        self.assertEqual(exporter.call_status_label(events[0]), "Missed")
        self.assertEqual(
            exporter.call_status_label(events[1], "Example Contact"),
            "Incoming FaceTime call from Example Contact",
        )
        self.assertEqual(exporter.call_status_label(events[2]), "Canceled")
        self.assertEqual(
            exporter.call_status_label(events[3], "Example Contact"),
            "Outgoing phone call to Example Contact",
        )
        self.assertEqual(exporter.format_call_duration(0.42), "< 1 second")
        self.assertEqual(exporter.format_call_duration(52.2), "52 seconds")
        self.assertEqual(exporter.format_call_duration(3900), "1 hour 5 minutes")
        self.assertEqual(exporter.format_call_duration(7071.708137989044), "1 hour 57 minutes")
        self.assertIn('data-call-status="missed" data-call-direction="incoming"', html)
        self.assertIn('data-call-status="answered" data-call-direction="incoming"', html)
        self.assertIn('data-call-status="canceled" data-call-direction="outgoing"', html)
        self.assertIn('data-call-status="answered" data-call-direction="outgoing"', html)
        self.assertIn('class="call-card call-status-missed"', html)
        self.assertIn('class="call-card call-status-canceled"', html)
        self.assertIn('<span class="call-status-word call-status-word-missed">Missed</span>', html)
        self.assertIn('<span class="call-status-word call-status-word-canceled">Canceled</span>', html)
        self.assertIn('<span class="call-status-word call-status-word-answered">Incoming FaceTime call from Example Contact</span>', html)
        self.assertIn('<span class="call-status-word call-status-word-answered">Outgoing phone call to Example Contact</span>', html)
        self.assertIn('<div class="call-duration"><span class="call-duration-label">Duration:</span> <span class="call-duration-value">20 minutes</span></div>', html)
        self.assertIn('<div class="call-duration"><span class="call-duration-label">Duration:</span> <span class="call-duration-value">1 hour 57 minutes</span></div>', html)
        self.assertIn("row: 1234", html)
        self.assertIn("guid: call-guid", html)
        self.assertNotIn("ZCALLTYPE", html)
        self.assertNotIn("ZANSWERED", html)
        self.assertNotIn("ZDURATION", html)
        self.assertNotIn("ZUNIQUE_ID", html)
        self.assertNotIn("+12025550101", html)

    def test_calls_merge_chronologically_and_render_between_messages(self):
        before = self._message(
            sequence=1,
            timestamp_local="2026-03-03T14:58:00-08:00",
            timestamp_utc="2026-03-03T22:58:00+00:00",
            text="Before call",
        )
        after = self._message(
            sequence=2,
            source_rowid=2,
            guid="after",
            timestamp_local="2026-03-03T15:10:00-08:00",
            timestamp_utc="2026-03-03T23:10:00+00:00",
            text="After call",
        )
        call = exporter.CallEvent(
            sequence=1,
            source_rowid=1234,
            unique_id="call-guid",
            timestamp_local="2026-03-03T14:59:00-08:00",
            timestamp_utc="2026-03-03T22:59:00Z",
            timestamp_raw=794617140,
            direction="incoming",
            answered_label="unanswered_or_missed",
            duration_seconds=0,
            call_type_label="phone_or_audio_call",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                [before, after],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-03-03T14:58:00-08:00",
                    "last_local": "2026-03-03T15:10:00-08:00",
                },
                "Synthetic Contact",
                "2026-03-03T15:30:00-08:00",
                "0" * 64,
                [call],
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertLess(html.index("Before call"), html.index('class="call-title"'))
        self.assertLess(html.index('class="call-title"'), html.index("After call"))
        self.assertIn('class="message call-event"', html)
        self.assertIn('data-call-status="missed" data-call-direction="incoming"', html)

    def test_export_with_calls_jsonl_copies_reports_and_hashes_call_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "chat.db"
            calls_jsonl = root / "call_records_source.jsonl"
            attachments_root = root / "Library" / "Messages" / "Attachments"
            attachment_file = attachments_root / "aa" / "bb" / "photo.jpg"
            attachment_file.parent.mkdir(parents=True)
            attachment_file.write_bytes(b"fake-jpeg")
            self._create_db(db, attachment_file)
            calls_jsonl.write_text(
                json.dumps(
                    {
                        "sequence": 1,
                        "timestamp_local": "2000-12-31T16:20:00-08:00",
                        "timestamp_utc": "2001-01-01T00:20:00Z",
                        "duration_seconds": 0,
                        "direction": "outgoing",
                        "answered_label": "unanswered_or_missed",
                        "z_pk": 1234,
                        "zaddress": "+12025550101",
                        "raw": {
                            "Z_PK": 1234,
                            "ZADDRESS": "+12025550101",
                            "ZANSWERED": 0,
                            "ZDURATION": 0,
                            "ZUNIQUE_ID": "call-guid",
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            out = root / "out"
            status = exporter.export_thread(
                Namespace(
                    db=str(db),
                    chat_id=1,
                    label="Synthetic Contact",
                    attachments_root=str(attachments_root),
                    out=str(out),
                    calls_jsonl=str(calls_jsonl),
                    no_pdf=True,
                )
            )

            self.assertEqual(status, 0)
            self.assertEqual(
                (out / "call_records.jsonl").read_text(encoding="utf-8"),
                calls_jsonl.read_text(encoding="utf-8"),
            )
            html_text = (out / "thread.html").read_text(encoding="utf-8")
            self.assertNotIn('class="message call-event"', html_text)

            html_calls = (out / "thread_with_calls.html").read_text(encoding="utf-8")
            self.assertIn('data-call-status="canceled" data-call-direction="outgoing"', html_calls)
            self.assertIn('class="call-card call-status-canceled"', html_calls)
            call_section = html_calls[html_calls.index('<section class="message call-event"') :]
            call_section = call_section[: call_section.index("</section>")]
            self.assertNotIn("+12025550101", call_section)
            report = (out / "extraction_report.md").read_text(encoding="utf-8")
            self.assertIn("## Call Timeline Context", report)
            self.assertIn("Inline call events rendered: `1`", report)
            self.assertIn("Phone numbers and private CallHistory enum values are not displayed in the public transcript call cards.", report)
            self.assertIn("Raw call metadata remains preserved in `call_records.jsonl`", report)
            manifest = (out / "manifest.sha256").read_text(encoding="utf-8")
            self.assertIn("call_records.jsonl", manifest)
            self.assertIn("thread.html", manifest)
            self.assertIn("thread_with_calls.html", manifest)

    def test_export_with_call_db_extracts_and_hashes_call_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "chat.db"
            call_db = root / "call_history.db"
            attachments_root = root / "Library" / "Messages" / "Attachments"
            attachment_file = attachments_root / "aa" / "bb" / "photo.jpg"
            attachment_file.parent.mkdir(parents=True)
            attachment_file.write_bytes(b"fake-jpeg")

            self._create_db(db, attachment_file)

            # Create a mock call database
            with sqlite3.connect(call_db) as conn:
                conn.executescript(
                    """
                    CREATE TABLE ZCALLRECORD (
                        Z_PK INTEGER PRIMARY KEY,
                        Z_ENT INTEGER,
                        ZANSWERED INTEGER,
                        ZCALLTYPE INTEGER,
                        ZORIGINATED INTEGER,
                        ZDATE TIMESTAMP,
                        ZDURATION FLOAT,
                        ZADDRESS VARCHAR,
                        ZUNIQUE_ID VARCHAR
                    );
                    """
                )
                conn.execute(
                    """
                    INSERT INTO ZCALLRECORD
                    (Z_PK, Z_ENT, ZANSWERED, ZCALLTYPE, ZORIGINATED, ZDATE, ZDURATION, ZADDRESS, ZUNIQUE_ID)
                    VALUES (100, 2, 1, 1, 1, 10, 61.5, '+12025550101', 'unique-call')
                    """
                )
                conn.commit()

            out = root / "out"
            status = exporter.export_thread(
                Namespace(
                    db=str(db),
                    chat_id=1,
                    label="Synthetic Contact",
                    attachments_root=str(attachments_root),
                    out=str(out),
                    call_db=str(call_db),
                    calls_jsonl=None,
                    no_pdf=True,
                )
            )

            self.assertEqual(status, 0)
            self.assertTrue((out / "source" / "call_history_copy.db").exists())
            self.assertTrue((out / "call_records.jsonl").exists())

            html_text = (out / "thread.html").read_text(encoding="utf-8")
            self.assertNotIn('class="message call-event"', html_text)

            html_calls = (out / "thread_with_calls.html").read_text(encoding="utf-8")
            self.assertIn('data-call-status="answered" data-call-direction="outgoing"', html_calls)
            self.assertIn('class="call-card call-status-answered"', html_calls)
            self.assertIn('<div class="call-duration"><span class="call-duration-label">Duration:</span> <span class="call-duration-value">1 minute</span></div>', html_calls)

            report = (out / "extraction_report.md").read_text(encoding="utf-8")
            self.assertIn("## Call Timeline Context", report)
            self.assertIn("Inline call events rendered: `1`", report)
            self.assertIn("Copied call JSONL: `call_records.jsonl`", report)

    def test_plugin_payload_png_signature_renders_as_image(self):
        attachment = self._attachment_with_signature(
            source_rowid=1,
            filename="payload.pluginPayloadAttachment.png",
            signature=b"\x89PNG\r\n\x1a\nsynthetic",
        )

        html = exporter.attachment_html(attachment)

        self.assertIn("<img", html)
        self.assertIn("payload.pluginPayloadAttachment.png", html)

    def test_plugin_payload_jpeg_signature_renders_as_image(self):
        attachment = self._attachment_with_signature(
            source_rowid=2,
            filename="payload.pluginPayloadAttachment.jpg",
            signature=b"\xff\xd8\xff\xe0synthetic\xff\xd9",
        )

        html = exporter.attachment_html(attachment)

        self.assertIn("<img", html)
        self.assertIn("payload.pluginPayloadAttachment.jpg", html)

    def test_unknown_binary_attachment_remains_caption_only(self):
        attachment = self._attachment_with_signature(
            source_rowid=3,
            filename="payload.pluginPayloadAttachment",
            signature=b"\x00\x01\x02synthetic",
        )

        html = exporter.attachment_html(attachment)

        self.assertNotIn("<img", html)
        self.assertIn("<th>Attachment row ID</th><td>3</td>", html)

    def test_heic_preview_renders_from_derived_media(self):
        attachment = exporter.AttachmentRecord(
            source_rowid=4,
            guid="att-4",
            original_filename="photo.heic",
            resolved_path=None,
            export_filename="photo.heic",
            mime_type="image/heic",
            uti="public.heic",
            transfer_name="photo.heic",
            total_bytes=100,
            sha256="1" * 64,
            status="copied",
            detected_mime_type="image/heic",
            preview_filename="msg_000001_att_4_preview.jpg",
            preview_mime_type="image/jpeg",
            preview_sha256="2" * 64,
            preview_status="preview_generated",
        )

        html = exporter.attachment_html(attachment)

        self.assertIn("<img", html)
        self.assertIn("derived_media/msg_000001_att_4_preview.jpg", html)
        self.assertIn("<th>Preview SHA-256</th><td>2222222222222222222222222222222222222222222222222222222222222222</td>", html)

    def test_create_heic_preview_uses_sips_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "photo.heic"
            source.write_bytes(b"synthetic-heic")
            derived_media = root / "derived_media"
            derived_media.mkdir()

            def fake_run(command, **_kwargs):
                destination = Path(command[-1])
                destination.write_bytes(b"synthetic-jpg")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(exporter.subprocess, "run", side_effect=fake_run):
                filename, mime_type, status, error = exporter.create_heic_preview(
                    source, 7, 42, derived_media
                )

            self.assertEqual(filename, "msg_000007_att_42_preview.jpg")
            self.assertEqual(mime_type, "image/jpeg")
            self.assertEqual(status, "preview_generated")
            self.assertIsNone(error)
            self.assertEqual((derived_media / filename).read_bytes(), b"synthetic-jpg")

    def test_findmy_payload_embedded_jpeg_extracts_derived_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            derived_media = root / "derived_media"
            derived_media.mkdir()
            jpeg = self._jpeg_bytes(96, 72)
            payload = b"bplist00archive-prefix" + jpeg + b"archive-suffix"

            media = exporter.extract_payload_preview(7, 900, payload, derived_media)

            self.assertIsNotNone(media)
            assert media is not None
            self.assertEqual(media.mime_type, "image/jpeg")
            self.assertEqual(media.width, 96)
            self.assertEqual(media.height, 72)
            self.assertEqual(media.render_kind, "thumbnail")
            self.assertTrue((derived_media / media.export_filename).exists())
            self.assertEqual((derived_media / media.export_filename).read_bytes(), jpeg)

    def test_findmy_payload_embedded_png_records_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            derived_media = root / "derived_media"
            derived_media.mkdir()
            png = self._png_bytes(320, 180)
            payload = b"archive-prefix" + png + b"archive-suffix"

            media = exporter.extract_payload_preview(8, 901, payload, derived_media)

            self.assertIsNotNone(media)
            assert media is not None
            self.assertEqual(media.mime_type, "image/png")
            self.assertEqual(media.width, 320)
            self.assertEqual(media.height, 180)
            self.assertEqual(media.render_kind, "image")

    def test_findmy_payload_thumbnail_renders_as_low_res_card(self):
        message = self._message(
            sequence=2,
            render_kind="findmy_location_card",
            balloon_bundle_id="com.apple.findmy.FindMyMessagesApp",
            derived_media=[
                exporter.DerivedMediaRecord(
                    source="message.payload_data",
                    export_filename="msg_000002_payload_900_preview.jpg",
                    mime_type="image/jpeg",
                    sha256="1" * 64,
                    bytes=3600,
                    width=96,
                    height=72,
                    render_kind="thumbnail",
                )
            ],
        )

        card = exporter.findmy_location_card_html(message)

        self.assertIn("location-card-thumbnail", card)
        self.assertNotIn("location-card-image", card)
        self.assertIn("Local payload thumbnail SHA-256", card)
        self.assertIn("96x72", card)
        self.assertNotIn("Preview SHA-256", card)

    def test_findmy_payload_full_preview_keeps_image_card_style(self):
        message = self._message(
            sequence=2,
            render_kind="findmy_location_card",
            balloon_bundle_id="com.apple.findmy.FindMyMessagesApp",
            derived_media=[
                exporter.DerivedMediaRecord(
                    source="message.payload_data",
                    export_filename="msg_000002_payload_900_preview.png",
                    mime_type="image/png",
                    sha256="1" * 64,
                    bytes=20000,
                    width=640,
                    height=360,
                    render_kind="image",
                )
            ],
        )

        card = exporter.findmy_location_card_html(message)

        self.assertIn("location-card-image", card)
        self.assertIn("Preview SHA-256", card)
        self.assertIn("640x360", card)

    def test_findmy_payload_without_image_renders_fallback_card(self):
        message = self._message(
            sequence=1,
            render_kind="findmy_location_card",
            balloon_bundle_id="com.apple.findmy.FindMyMessagesApp",
            payload_metadata=["Synthetic location metadata"],
        )

        card = exporter.findmy_location_card_html(message)

        self.assertIn("Find My location shared by the contact", card)
        self.assertIn("No extractable local preview image was found.", card)
        self.assertNotIn("<img", card)

    def test_findmy_payload_thumbnail_stays_small_in_rendered_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            derived_media = root / "derived_media"
            derived_media.mkdir()
            preview_filename = "msg_000002_payload_900_preview.png"
            (derived_media / preview_filename).write_bytes(self._png_bytes(96, 72))
            exporter.write_paginate_js(root / "paginate.js")
            message = self._message(
                sequence=2,
                render_kind="findmy_location_card",
                balloon_bundle_id="com.apple.findmy.FindMyMessagesApp",
                derived_media=[
                    exporter.DerivedMediaRecord(
                        source="message.payload_data",
                        export_filename=preview_filename,
                        mime_type="image/png",
                        sha256="1" * 64,
                        bytes=(derived_media / preview_filename).stat().st_size,
                        width=96,
                        height=72,
                        render_kind="thumbnail",
                    )
                ],
            )
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )

            script = textwrap.dedent(
                """
                import { pathToFileURL } from "node:url";
                import { chromium } from "playwright";

                const htmlPath = process.argv[1];
                const browser = await chromium.launch({ headless: true });
                try {
                  const page = await browser.newPage({
                    viewport: { width: 816, height: 1056 },
                    deviceScaleFactor: 1,
                  });
                  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
                  await page.waitForFunction(
                    () => document.getElementById("transcript")?.dataset.paginated === "true",
                    null,
                    { timeout: 30000 }
                  );
                  await page.waitForFunction(
                    () => Array.from(document.images).every((img) => img.complete),
                    null,
                    { timeout: 30000 }
                  );
                  const stats = await page.evaluate(() => {
                    const img = document.querySelector(".location-card-thumbnail");
                    const card = document.querySelector(".location-card");
                    const style = window.getComputedStyle(img);
                    return {
                      imageWidth: img.getBoundingClientRect().width,
                      cardWidth: card.getBoundingClientRect().width,
                      objectFit: style.objectFit,
                    };
                  });
                  console.log(JSON.stringify(stats));
                } finally {
                  await browser.close();
                }
                """
            )
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(root / "thread.html")],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"Playwright Find My thumbnail test unavailable: {result.stderr.strip()}")

            stats = json.loads(result.stdout)
            self.assertLess(stats["imageWidth"], stats["cardWidth"])
            self.assertLessEqual(stats["imageWidth"], 134.5)
            self.assertEqual(stats["objectFit"], "contain")

    def test_location_share_alert_incoming_text_uses_message_direction(self):
        message = self._message(
            sequence=3,
            item_type=4,
            share_direction=0,
            direction="incoming",
            render_kind="location_share_alert",
        )

        self.assertEqual(
            exporter.location_share_alert_text(message, "Example Contact"),
            "Example Contact started sharing location with you.",
        )

    def test_location_share_alert_outgoing_text(self):
        message = self._message(
            sequence=5,
            item_type=4,
            share_direction=0,
            direction="outgoing",
            is_from_me=1,
            render_kind="location_share_alert",
        )

        self.assertEqual(
            exporter.location_share_alert_text(message, "Example Contact"),
            "You started sharing location with Example Contact.",
        )

    def test_unsent_message_detection_and_incoming_text(self):
        message = self._message(
            text="",
            body_status="empty",
            item_type=0,
            date_edited_raw=797151890965205888,
            date_edited_local="2026-04-06T00:04:50-07:00",
            date_edited_utc="2026-04-06T07:04:50+00:00",
            direction="incoming",
        )

        self.assertTrue(exporter.is_unsent_message(message))
        message.render_kind = "unsent_message"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Example Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertIn("Example Contact unsent a message", html)
        self.assertNotIn("[No text body]", html)

    def test_unsent_message_outgoing_text_and_export_fields(self):
        message = self._message(
            text="",
            body_status="empty",
            render_kind="unsent_message",
            direction="outgoing",
            date_edited_raw=797151890965205888,
            date_edited_local="2026-04-06T00:04:50-07:00",
            date_edited_utc="2026-04-06T07:04:50+00:00",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_jsonl(root / "messages.jsonl", [message])
            exporter.write_csv(root / "messages.csv", [message])
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )
            row = json.loads((root / "messages.jsonl").read_text(encoding="utf-8"))
            csv_text = (root / "messages.csv").read_text(encoding="utf-8-sig")
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertEqual(row["date_edited_raw"], 797151890965205888)
        self.assertEqual(row["date_edited_local"], "2026-04-06T00:04:50-07:00")
        self.assertIn("date_edited_raw", csv_text)
        self.assertIn("2026-04-06T07:04:50+00:00", csv_text)
        self.assertIn("You unsent a message", html)

    def test_empty_non_unsent_message_still_renders_no_text_body(self):
        message = self._message(text="", body_status="empty", date_edited_raw=0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertIn("[No text body]", html)

    def test_plugin_payload_preview_selection_ignores_icon_when_content_exists(self):
        icon = self._attachment_with_signature(
            source_rowid=1,
            filename="icon.pluginPayloadAttachment.png",
            signature=self._png_bytes(128, 128),
            total_bytes=8000,
        )
        content = self._attachment_with_signature(
            source_rowid=2,
            filename="content.pluginPayloadAttachment.png",
            signature=self._png_bytes(320, 180),
            total_bytes=6000,
        )

        selected = exporter.select_plugin_payload_preview_attachment([icon, content])

        self.assertEqual(selected, content)

    def test_plugin_payload_preview_selection_prefers_first_non_icon_row(self):
        first_content = self._attachment_with_signature(
            source_rowid=1293,
            filename="first-content.pluginPayloadAttachment.png",
            signature=self._png_bytes(360, 240),
        )
        later_larger = self._attachment_with_signature(
            source_rowid=1294,
            filename="later-larger.pluginPayloadAttachment.png",
            signature=self._png_bytes(400, 260),
        )

        selected = exporter.select_plugin_payload_preview_attachment([later_larger, first_content])

        self.assertEqual(selected, first_content)

    def test_plugin_payload_preview_selection_uses_row_order_for_non_icons(self):
        first = self._attachment_with_signature(
            source_rowid=10,
            filename="first.pluginPayloadAttachment.png",
            signature=self._png_bytes(300, 200),
            total_bytes=1000,
        )
        larger_bytes = self._attachment_with_signature(
            source_rowid=11,
            filename="larger-bytes.pluginPayloadAttachment.png",
            signature=self._png_bytes(300, 200),
            total_bytes=1200,
        )
        later_same_bytes = self._attachment_with_signature(
            source_rowid=12,
            filename="later-same-bytes.pluginPayloadAttachment.png",
            signature=self._png_bytes(300, 200),
            total_bytes=1200,
        )

        selected = exporter.select_plugin_payload_preview_attachment(
            [first, larger_bytes, later_same_bytes]
        )

        self.assertEqual(selected, first)

    def test_plugin_payload_preview_selection_falls_back_without_dimensions(self):
        first = self._attachment_with_signature(
            source_rowid=1,
            filename="first.pluginPayloadAttachment.png",
            signature=b"\x89PNG\r\n\x1a\nsynthetic",
        )
        second = self._attachment_with_signature(
            source_rowid=2,
            filename="second.pluginPayloadAttachment.png",
            signature=b"\x89PNG\r\n\x1a\nsynthetic",
        )

        selected = exporter.select_plugin_payload_preview_attachment([first, second])

        self.assertEqual(selected, first)

    def test_plugin_payload_image_group_renders_compact_url_preview_card(self):
        attachments = [
            self._attachment_with_signature(
                source_rowid=index,
                filename=f"payload-{index}.pluginPayloadAttachment.png",
                signature=self._png_bytes(120 + index, 90 + index),
            )
            for index in range(1, 6)
        ]
        message = self._message(text="https://example.test", attachments=attachments)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertIn('class="message-fragment">https://example.test</div>', html)
        self.assertIn('class="plugin-url-preview"', html)
        self.assertIn("URL preview generated by Messages", html)
        self.assertIn("example.test", html)
        self.assertIn("5 plugin payload assets preserved: rows 1-5", html)
        self.assertEqual(html.count('class="plugin-url-preview-image"'), 1)
        self.assertNotIn('class="plugin-gallery"', html)
        self.assertNotIn('class="plugin-metadata-table"', html)
        self.assertNotIn('class="plugin-metadata-row"', html)
        self.assertNotIn('class="attachment"', html)

    def test_plugin_payload_preview_metadata_remains_in_json_csv_and_report(self):
        attachments = [
            self._attachment_with_signature(
                source_rowid=rowid,
                filename=f"payload-{rowid}.pluginPayloadAttachment.png",
                signature=self._png_bytes(width, height),
                total_bytes=bytes_count,
            )
            for rowid, width, height, bytes_count in (
                (1292, 128, 128, 9000),
                (1293, 360, 240, 12000),
                (1294, 400, 260, 14000),
                (1295, 360, 240, 13000),
                (1296, 220, 180, 10000),
            )
        ]
        for index, attachment in enumerate(attachments):
            attachment.sha256 = f"{index + 1}" * 64
        message = self._message(
            sequence=1073,
            source_rowid=8123,
            text="https://example.test/article",
            attachments=attachments,
        )
        chat_summary = {
            "chat_id": 1,
            "guid": "chat-guid",
            "service_name": "iMessage",
            "style": 45,
            "handles": ["+12025550102"],
            "first_local": "2026-01-01T00:00:00-08:00",
            "last_local": "2026-01-01T00:00:00-08:00",
            "messages": 1,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_jsonl(root / "messages.jsonl", [message])
            exporter.write_csv(root / "messages.csv", [message])
            exporter.write_report(
                root / "extraction_report.md",
                root / "chat.db",
                "0" * 64,
                "0" * 64,
                chat_summary,
                [message],
                Namespace(label="Synthetic Contact"),
                "2026-01-01T00:30:00-08:00",
            )
            json_row = json.loads((root / "messages.jsonl").read_text(encoding="utf-8"))
            csv_row = next(
                csv.DictReader((root / "messages.csv").read_text(encoding="utf-8-sig").splitlines())
            )
            report = (root / "extraction_report.md").read_text(encoding="utf-8")

        self.assertEqual(json_row["plugin_payload_group_count"], 5)
        self.assertEqual(json_row["plugin_payload_rows"], [1292, 1293, 1294, 1295, 1296])
        self.assertEqual(json_row["plugin_payload_selected_row"], 1293)
        self.assertEqual([row["source_rowid"] for row in json_row["attachments"]], [1292, 1293, 1294, 1295, 1296])
        self.assertEqual(json_row["attachments"][2]["width"], 400)
        self.assertEqual(json_row["attachments"][2]["height"], 260)
        self.assertEqual(csv_row["plugin_payload_group_count"], "5")
        self.assertEqual(csv_row["plugin_payload_rows"], "1292;1293;1294;1295;1296")
        self.assertEqual(csv_row["plugin_payload_selected_row"], "1293")
        self.assertEqual(csv_row["attachment_rows"], "1292;1293;1294;1295;1296")
        self.assertIn("400x260", csv_row["attachment_dimensions"])
        self.assertIn("Plugin Payload URL Previews", report)
        self.assertIn("Message #1073, source row `8123`, selected preview row `1293`", report)
        self.assertIn("attachment rows `1292-1296`", report)
        for attachment in attachments:
            self.assertIn(f"Attachment row `{attachment.source_rowid}`", report)
            self.assertIn(attachment.sha256, report)

    def test_multi_photo_attachments_render_as_forensic_cards(self):
        message = self._message(
            text="ordinary photos",
            attachments=[
                self._attachment_with_signature(
                    source_rowid=index,
                    filename=f"photo-{index}.png",
                    signature=self._png_bytes(240 + index, 160 + index),
                    total_bytes=2000 + index,
                )
                for index in range(1, 4)
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertNotIn('class="plugin-gallery"', html)
        self.assertNotIn('class="plugin-url-preview"', html)
        self.assertEqual(html.count('class="message outgoing"'), 0)
        self.assertEqual(html.count('class="message incoming"'), 1)
        self.assertEqual(html.count('class="attachment forensic-attachment-card"'), 3)
        self.assertEqual(html.count("Forensic Attachment"), 3)
        self.assertEqual(html.count('class="attachment-exhibit"'), 0)
        self.assertNotIn("Full-size exhibit", html)
        for index in range(1, 4):
            self.assertNotIn(f"Attachment Exhibit 1-{index}", html)
            self.assertIn(f"<th>Attachment row ID</th><td>{index}</td>", html)
            self.assertIn(f"<th>Exported filename</th><td>photo-{index}.png</td>", html)
            self.assertIn("<th>SHA-256</th><td>0000000000000000000000000000000000000000000000000000000000000000</td>", html)
            self.assertIn(f"<th>Bytes</th><td>{2000 + index}</td>", html)
            self.assertIn("<th>Media type</th><td>image/png</td>", html)
            self.assertIn(f"<th>Dimensions</th><td>{240 + index}x{160 + index}</td>", html)

    def test_attachment_only_message_has_no_empty_message_fragment_line(self):
        message = self._message(
            text="\ufffc",  # Only contains the Object Replacement Character
            attachments=[
                self._attachment_with_signature(
                    source_rowid=42,
                    filename="photo.png",
                    signature=self._png_bytes(240, 160),
                )
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertNotIn('class="message-fragment"', html)
        self.assertNotIn("\ufffc", html)
        self.assertNotIn("[No text body]", html)

    def test_message_edit_history_renders_properly(self):
        msg_with_history = self._message(
            sequence=1,
            text="Final Text",
            date_edited_raw=1,
            date_edited_local="2026-01-01T00:05:00-08:00",
            edit_history=[
                {"text": "Original Text", "timestamp_local": "2026-01-01T00:00:00-08:00"}
            ]
        )

        msg_edited_only = self._message(
            sequence=2,
            text="Edited Text",
            date_edited_raw=1,
            date_edited_local="2026-01-01T00:05:00-08:00",
            edit_history=[]
        )
        msg_with_multiple_versions = self._message(
            sequence=3,
            text="Final Multiple Text",
            date_edited_raw=1,
            date_edited_local="2026-01-01T00:10:00-08:00",
            edit_history=[
                {"text": "Original Multiple Text", "timestamp_local": "2026-01-01T00:00:00-08:00"},
                {"text": "Intermediate Multiple Text", "timestamp_local": "2026-01-01T00:05:00-08:00"},
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            messages = [msg_with_history, msg_edited_only, msg_with_multiple_versions]
            exporter.write_jsonl(root / "messages.jsonl", messages)
            exporter.write_csv(root / "messages.csv", messages)
            exporter.write_html(
                root / "thread.html",
                messages,
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )
            json_rows = [
                json.loads(line)
                for line in (root / "messages.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            csv_rows = list(
                csv.DictReader((root / "messages.csv").read_text(encoding="utf-8-sig").splitlines())
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertIn('<div class="message-fragment">Final Text</div>', html)
        self.assertEqual(html.count('class="edited-label"'), 3)
        self.assertIn(
            '<div class="bubble"><div class="message-fragment">Final Text</div></div>\n'
            '  <span class="edited-label">Edited · 1 prior version · Details in messages.csv and messages.jsonl</span>',
            html,
        )
        self.assertIn("Edited · 2 prior versions · Details in messages.csv and messages.jsonl", html)
        self.assertIn(">Edited · Details in messages.csv and messages.jsonl</span>", html)
        self.assertNotIn("details in messages.csv/jsonl", html)
        self.assertNotIn(".bubble .edited-label", html)
        self.assertNotIn('class="edited-summary"', html)
        self.assertNotIn("Source rows in messages.jsonl / messages.csv", html)
        self.assertNotIn("msg #", html)
        self.assertNotIn("row 1</div>", html)
        self.assertNotIn("Last edited", html)
        self.assertNotIn("Last Edited:", html)
        self.assertNotIn('class="status-edited"', html)
        self.assertNotIn("Edited • Revision History", html)
        self.assertNotIn('class="edit-history"', html)
        self.assertNotIn("Verbatim:", html)
        self.assertNotIn("Redline:", html)
        self.assertNotIn('class="diff-del"', html)
        self.assertNotIn('class="diff-ins"', html)
        self.assertNotIn('"Original Text"', html)
        self.assertEqual(
            json_rows[0]["edit_history"],
            [{"text": "Original Text", "timestamp_local": "2026-01-01T00:00:00-08:00"}],
        )
        self.assertEqual(json_rows[1]["edit_history"], [])
        self.assertEqual(
            json_rows[2]["edit_history"],
            [
                {"text": "Original Multiple Text", "timestamp_local": "2026-01-01T00:00:00-08:00"},
                {"text": "Intermediate Multiple Text", "timestamp_local": "2026-01-01T00:05:00-08:00"},
            ],
        )
        self.assertIn("edit_history_json", csv_rows[0])
        self.assertIn('"Original Text"', csv_rows[0]["edit_history_json"])
        self.assertEqual(csv_rows[0]["edit_history_verbatims"], "Original Text")
        self.assertEqual(csv_rows[0]["edit_history_timestamps"], "2026-01-01T00:00:00-08:00")
        self.assertEqual(csv_rows[1]["edit_history_json"], "")
        self.assertIn('"Intermediate Multiple Text"', csv_rows[2]["edit_history_json"])

    def test_paginate_js_removes_edited_summary_from_text_continuations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_paginate_js(root / "paginate.js")
            message = self._message(
                sequence=2,
                source_rowid=22,
                guid="edited-long",
                text=" ".join(f"word{index}" for index in range(1800)),
                direction="outgoing",
                date_edited_raw=1,
                date_edited_local="2026-01-01T00:05:00-08:00",
                edit_history=[
                    {"text": "Original long text", "timestamp_local": "2026-01-01T00:00:00-08:00"}
                ],
            )
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:01:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )

            script = textwrap.dedent(
                """
                import { pathToFileURL } from "node:url";
                import { chromium } from "playwright";

                const htmlPath = process.argv[1];
                const browser = await chromium.launch({ headless: true });
                try {
                  const page = await browser.newPage({
                    viewport: { width: 816, height: 1056 },
                    deviceScaleFactor: 1,
                  });
                  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
                  await page.waitForFunction(
                    () => document.getElementById("transcript")?.dataset.paginated === "true",
                    null,
                    { timeout: 30000 }
                  );
                  const stats = await page.evaluate(() => {
                    const blocks = Array.from(document.querySelectorAll(".page .message[data-sequence='2']"));
                    return {
                      messageBlocks: blocks.length,
                      editedLabels: document.querySelectorAll(".edited-label").length,
                      labelsInsideBubble: document.querySelectorAll(".bubble .edited-label").length,
                      editedSummaries: document.querySelectorAll(".edited-summary").length,
                      continuationLabels: blocks.slice(1).filter(
                        (node) => node.querySelector(".edited-label")
                      ).length,
                      continuationMetas: blocks.slice(1).filter(
                        (node) => node.querySelector(".message-meta")
                      ).length,
                      emptyPages: Array.from(document.querySelectorAll(".page")).filter(
                        (node) => node.querySelectorAll(".message").length === 0
                      ).length,
                    };
                  });
                  console.log(JSON.stringify(stats));
                } finally {
                  await browser.close();
                }
                """
            )
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(root / "thread.html")],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"Playwright edited-text pagination test unavailable: {result.stderr.strip()}")

            stats = json.loads(result.stdout)
            self.assertGreater(stats["messageBlocks"], 1)
            self.assertEqual(stats["editedLabels"], 1)
            self.assertEqual(stats["labelsInsideBubble"], 0)
            self.assertEqual(stats["editedSummaries"], 0)
            self.assertEqual(stats["continuationLabels"], 0)
            self.assertEqual(stats["continuationMetas"], 0)
            self.assertEqual(stats["emptyPages"], 0)

    def test_tall_screenshot_attachment_renders_inline_without_exhibit(self):
        message = self._message(
            sequence=201,
            source_rowid=10307,
            text="screenshot",
            attachments=[
                self._attachment_with_signature(
                    source_rowid=1781,
                    filename="screenshot.png",
                    signature=self._png_bytes(1179, 2556),
                    total_bytes=974178,
                )
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertNotIn('class="attachment-exhibit"', html)
        self.assertNotIn('id="attachment-exhibit-201-1"', html)
        self.assertNotIn("Full-size exhibit:", html)
        self.assertNotIn("Attachment Exhibit 201-1", html)
        self.assertIn('class="attachment forensic-attachment-card screenshot-large"', html)
        self.assertIn("<th>Attachment row ID</th><td>1781</td>", html)
        self.assertIn("<th>SHA-256</th><td>0000000000000000000000000000000000000000000000000000000000000000</td>", html)
        self.assertIn("<th>Exported filename</th><td>screenshot.png</td>", html)
        self.assertIn("<th>Bytes</th><td>974178</td>", html)
        self.assertIn("<th>Media type</th><td>image/png</td>", html)
        self.assertIn("<th>Dimensions</th><td>1179x2556</td>", html)

    def test_image_exhibit_metadata_not_in_report(self):
        attachment = self._attachment_with_signature(
            source_rowid=1781,
            filename="screenshot.png",
            signature=self._png_bytes(1179, 2556),
            total_bytes=974178,
        )
        attachment.sha256 = "a" * 64
        message = self._message(
            sequence=201,
            source_rowid=10307,
            text="screenshot",
            attachments=[attachment],
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_report(
                root / "extraction_report.md",
                root / "chat.db",
                "0" * 64,
                "0" * 64,
                {
                    "chat_id": 1,
                    "guid": "chat-guid",
                    "service_name": "iMessage",
                    "style": 45,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                    "messages": 1,
                },
                [message],
                Namespace(label="Synthetic Contact"),
                "2026-01-01T00:30:00-08:00",
            )
            report = (root / "extraction_report.md").read_text(encoding="utf-8")

        self.assertNotIn("Full-size image exhibit pages", report)
        self.assertNotIn("## Attachment Image Exhibits", report)
        self.assertNotIn("Attachment Exhibit 201-1", report)

    def test_message_sent_with_siri_renders_siri_note(self):
        message = self._message(
            sequence=50,
            text="Sent from siri",
            sent_with_siri=True,
            direction="outgoing",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertIn('<div class="siri-note">Sent with Siri</div>', html)

    def test_message_reaction_renders_reaction_bubble(self):
        # 1. Test derive_render_kind logic
        self.assertEqual(exporter.derive_render_kind(0, None, "some-guid", 2001), "reaction")
        self.assertEqual(exporter.derive_render_kind(0, None, "some-guid", 2006), "reaction")
        self.assertEqual(exporter.derive_render_kind(0, None, "some-guid", 3003), "reaction")
        self.assertEqual(exporter.derive_render_kind(0, None, None, 2001), "message")
        self.assertEqual(exporter.derive_render_kind(0, None, "some-guid", 10), "message")

        # 2. Test clean_associated_guid logic
        self.assertEqual(exporter.clean_associated_guid("bp:GUID-123"), "GUID-123")
        self.assertEqual(exporter.clean_associated_guid("p:0/GUID-456"), "GUID-456")
        self.assertEqual(exporter.clean_associated_guid("GUID-789"), "GUID-789")
        self.assertIsNone(exporter.clean_associated_guid(None))

        # 3. Test format_reaction_text logic
        self.assertIn('<span class="reaction-action">Liked 👍</span>', exporter.format_reaction_text("Liked “hello”"))
        self.assertIn('<span class="reaction-action">Reacted 🫡</span> to “hello”', exporter.format_reaction_text("Reacted 🫡 to “hello”"))
        self.assertIn('<span class="reaction-action">Reaccionó con 🫡 (Reacted 🫡)</span> a “hola”', exporter.format_reaction_text("Reaccionó con 🫡 a “hola”"))
        self.assertIn('<span class="reaction-action">Removed a laugh from 😂</span>', exporter.format_reaction_text("Removed a laugh from “hello”"))
        self.assertIn('<span class="reaction-action">Loved ❤️</span>', exporter.format_reaction_text("Loved “line1\nline2”"))
        self.assertIn('<span class="reaction-action">Le gustó (Liked) 👍</span>', exporter.format_reaction_text("Le gustó “hello”"))
        self.assertIn('<span class="reaction-action">Le encantó (Loved) ❤️</span>', exporter.format_reaction_text("Le encantó “hello”"))

        # 4. Test annotation logic
        target = self._message(sequence=1, guid="target-guid", text="target message text")
        reaction = self._message(
            sequence=2,
            guid="reaction-guid",
            associated_message_guid="bp:target-guid",
            associated_message_type=2001,
            text="Liked “target message text”",
            render_kind="reaction",
            direction="outgoing",
            is_from_me=1,
        )
        untargeted_reaction = self._message(
            sequence=3,
            guid="untargeted-guid",
            associated_message_guid="bp:missing-guid",
            associated_message_type=2001,
            text="Liked “something else”",
            render_kind="reaction",
            direction="incoming",
            is_from_me=0,
        )
        messages = [target, reaction, untargeted_reaction]
        exporter.annotate_reply_context(messages)
        self.assertEqual(reaction.reaction_target_sequence, 1)
        self.assertEqual(reaction.reaction_target_excerpt, "target message text")
        self.assertTrue(reaction.is_nested)
        self.assertEqual(len(target.reactions), 1)
        self.assertFalse(untargeted_reaction.is_nested)

        # 5. Test HTML rendering
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                messages,
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
                owner_label="Example Exporter",
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertIn('class="message reaction incoming"', html)
        self.assertNotIn('class="message reaction outgoing"', html)
        self.assertIn('<div class="reactions-container">', html)
        self.assertIn('<span class="reaction-badge">Liked 👍</span>', html)
        self.assertIn('by Example Exporter | outgoing | Thu, Jan 1 at 12:00 AM', html)

    def test_attachment_metadata_labels_do_not_wrap_in_rendered_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachments_dir = root / "attachments"
            attachments_dir.mkdir()
            png = self._png_bytes(240, 160)
            (attachments_dir / "photo.png").write_bytes(png)
            exporter.write_paginate_js(root / "paginate.js")
            message = self._message(
                sequence=1,
                attachments=[
                    self._attachment_with_signature(
                        source_rowid=1,
                        filename="photo.png",
                        signature=png,
                    )
                ],
            )
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )

            script = textwrap.dedent(
                """
                import { pathToFileURL } from "node:url";
                import { chromium } from "playwright";

                const htmlPath = process.argv[1];
                const browser = await chromium.launch({ headless: true });
                try {
                  const page = await browser.newPage({
                    viewport: { width: 816, height: 1056 },
                    deviceScaleFactor: 1,
                  });
                  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
                  await page.waitForFunction(
                    () => document.getElementById("transcript")?.dataset.paginated === "true",
                    null,
                    { timeout: 30000 }
                  );
                  const stats = await page.evaluate(() => {
                    const labels = Array.from(document.querySelectorAll(".attachment-metadata th"));
                    return labels.map((label) => {
                      const style = getComputedStyle(label);
                      const lineHeight = Number.parseFloat(style.lineHeight);
                      return {
                        text: label.textContent,
                        whiteSpace: style.whiteSpace,
                        height: label.getBoundingClientRect().height,
                        lineHeight,
                      };
                    });
                  });
                  console.log(JSON.stringify(stats));
                } finally {
                  await browser.close();
                }
                """
            )
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(root / "thread.html")],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"Playwright metadata label test unavailable: {result.stderr.strip()}")

            labels = json.loads(result.stdout)

        self.assertTrue(labels)
        for label in labels:
            self.assertEqual(label["whiteSpace"], "nowrap")
            self.assertLessEqual(label["height"], label["lineHeight"] * 1.5)

    def test_nonrenderable_plugin_payload_stays_explicit_attachment_row(self):
        message = self._message(
            text="plugin metadata",
            attachments=[
                self._attachment_with_signature(
                    source_rowid=1,
                    filename="payload.pluginPayloadAttachment",
                    signature=b"\x00\x01\x02synthetic",
                ),
                self._attachment_with_signature(
                    source_rowid=2,
                    filename="payload-2.pluginPayloadAttachment.png",
                    signature=b"\x89PNG\r\n\x1a\nsynthetic",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )
            html = (root / "thread.html").read_text(encoding="utf-8")

        self.assertNotIn('class="plugin-gallery"', html)
        self.assertNotIn('class="plugin-url-preview"', html)
        self.assertNotIn('class="attachment-exhibit"', html)
        self.assertIn("<th>Attachment row ID</th><td>1</td>", html)
        self.assertIn("<th>Status</th><td>copied</td>", html)

    def test_thread_originator_guid_reply_context_renders(self):
        target = self._message(
            sequence=1021,
            source_rowid=11409,
            guid="target-guid",
            text="Earlier message being replied to.",
            direction="incoming",
            timestamp_local="2026-03-19T20:37:08-07:00",
        )
        reply = self._message(
            sequence=1030,
            source_rowid=11418,
            guid="reply-guid",
            text="Reply body",
            direction="outgoing",
            thread_originator_guid="target-guid",
            thread_originator_part="0",
        )
        messages = [target, reply]

        exporter.annotate_reply_context(messages)
        html = exporter.reply_context_html(reply)

        self.assertEqual(reply.reply_context_source, "thread_originator_guid")
        self.assertIn("Reply to #1021", html)
        self.assertIn("incoming", html)
        self.assertIn("2026-03-19T20:37:08-07:00", html)
        self.assertIn("row 11409", html)
        self.assertIn("Earlier message being replied to.", html)

    def test_reply_to_guid_alone_does_not_render_reply_context(self):
        target = self._message(sequence=4, source_rowid=44, guid="reply-to-target", text="Fallback target")
        reply = self._message(
            sequence=5,
            source_rowid=45,
            guid="reply-to-message",
            text="Reply",
            reply_to_guid="reply-to-target",
        )
        messages = [target, reply]

        exporter.annotate_reply_context(messages)

        self.assertIsNone(reply.reply_context_source)
        self.assertEqual(exporter.reply_context_html(reply), "")

    def test_thread_originator_guid_renders_reply_and_raw_fields_export(self):
        reply_to_target = self._message(sequence=8, source_rowid=80, guid="reply-to-target", text="Wrong target")
        thread_target = self._message(sequence=9, source_rowid=90, guid="thread-target", text="Primary target")
        reply = self._message(
            sequence=10,
            source_rowid=100,
            guid="reply",
            text="Reply",
            reply_to_guid="reply-to-target",
            thread_originator_guid="thread-target",
            thread_originator_part="2",
        )
        messages = [reply_to_target, thread_target, reply]
        exporter.annotate_reply_context(messages)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_jsonl(root / "messages.jsonl", messages)
            exporter.write_csv(root / "messages.csv", messages)
            exporter.write_html(
                root / "thread.html",
                messages,
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:02:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )

            rows = [
                json.loads(line)
                for line in (root / "messages.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            csv_text = (root / "messages.csv").read_text(encoding="utf-8-sig")
            html_text = (root / "thread.html").read_text(encoding="utf-8")

        self.assertEqual(reply.reply_target_sequence, 9)
        self.assertEqual(reply.reply_context_source, "thread_originator_guid")
        self.assertEqual(rows[2]["reply_to_guid"], "reply-to-target")
        self.assertEqual(rows[2]["thread_originator_guid"], "thread-target")
        self.assertEqual(rows[2]["thread_originator_part"], "2")
        self.assertIn("reply-to-target", csv_text)
        self.assertIn("thread-target", csv_text)
        self.assertIn("Reply to #9", html_text)
        self.assertNotIn("Reply to #8", html_text)

    def test_thread_originator_guid_does_not_render_forward_targets(self):
        reply = self._message(sequence=1, source_rowid=10, guid="reply", thread_originator_guid="future")
        future_target = self._message(sequence=2, source_rowid=20, guid="future")
        messages = [reply, future_target]

        exporter.annotate_reply_context(messages)

        self.assertIsNone(reply.reply_context_source)
        self.assertEqual(exporter.reply_context_html(reply), "")

    def test_missing_reply_target_does_not_crash_and_report_counts_it(self):
        message = self._message(
            sequence=1,
            source_rowid=1,
            guid="reply",
            text="Reply",
            thread_originator_guid="missing-target",
        )
        messages = [message]
        exporter.annotate_reply_context(messages)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_report(
                root / "extraction_report.md",
                root / "chat.db",
                "0" * 64,
                "0" * 64,
                {
                    "chat_id": 1,
                    "guid": "chat-guid",
                    "service_name": "iMessage",
                    "style": 45,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:00:00-08:00",
                    "messages": 1,
                },
                messages,
                Namespace(label="Synthetic Contact"),
                "2026-01-01T00:30:00-08:00",
            )
            report = (root / "extraction_report.md").read_text(encoding="utf-8")

        self.assertIsNone(message.reply_context_source)
        self.assertEqual(exporter.missing_reply_reference_count(messages), 1)
        self.assertIn("Messages with visible reply context: `0`", report)
        self.assertIn("Reply references with targets missing from this export: `1`", report)

    def test_associated_message_guid_alone_does_not_render_reply_context(self):
        target = self._message(sequence=1, guid="tapback-target", text="Tapback target")
        tapback = self._message(
            sequence=2,
            guid="tapback",
            text="",
            associated_message_guid="tapback-target",
            associated_message_type=2000,
        )
        messages = [target, tapback]

        exporter.annotate_reply_context(messages)

        self.assertIsNone(tapback.reply_context_source)
        self.assertEqual(exporter.reply_context_html(tapback), "")

    def test_reply_context_boxes_do_not_break_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_paginate_js(root / "paginate.js")
            target = self._message(
                sequence=1,
                source_rowid=11,
                guid="target",
                text="Synthetic target " * 20,
                direction="incoming",
            )
            reply = self._message(
                sequence=2,
                source_rowid=12,
                guid="reply",
                text="Synthetic reply " * 40,
                direction="outgoing",
                thread_originator_guid="target",
            )
            messages = [target, reply]
            exporter.annotate_reply_context(messages)
            exporter.write_html(
                root / "thread.html",
                messages,
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:01:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )

            script = textwrap.dedent(
                """
                import { pathToFileURL } from "node:url";
                import { chromium } from "playwright";

                const htmlPath = process.argv[1];
                const browser = await chromium.launch({ headless: true });
                try {
                  const page = await browser.newPage({
                    viewport: { width: 816, height: 1056 },
                    deviceScaleFactor: 1,
                  });
                  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
                  await page.waitForFunction(
                    () => document.getElementById("transcript")?.dataset.paginated === "true",
                    null,
                    { timeout: 30000 }
                  );
                  const stats = await page.evaluate(() => {
                    const reply = document.querySelector(".reply-context");
                    const bubble = reply?.closest(".bubble");
                    return {
                      replyBoxes: document.querySelectorAll(".reply-context").length,
                      emptyPages: Array.from(document.querySelectorAll(".page")).filter(
                        (node) => node.querySelectorAll(".message").length === 0
                      ).length,
                      oversized: document.querySelectorAll("[data-oversized='true']").length,
                      replyWidth: reply?.getBoundingClientRect().width || 0,
                      bubbleWidth: bubble?.getBoundingClientRect().width || 0,
                    };
                  });
                  console.log(JSON.stringify(stats));
                } finally {
                  await browser.close();
                }
                """
            )
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(root / "thread.html")],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"Playwright reply-context smoke test unavailable: {result.stderr.strip()}")

            stats = json.loads(result.stdout)
            self.assertEqual(stats["replyBoxes"], 1)
            self.assertEqual(stats["emptyPages"], 0)
            self.assertEqual(stats["oversized"], 0)
            self.assertLessEqual(stats["replyWidth"], stats["bubbleWidth"])

    def test_paginate_js_packs_short_messages_after_first_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_paginate_js(root / "paginate.js")
            messages = [
                exporter.MessageRecord(
                    sequence=index,
                    source_rowid=index,
                    guid=f"synthetic-{index}",
                    text=f"Synthetic short message {index}.",
                    body_source="text",
                    body_status="text",
                    timestamp_local="2026-01-01T00:00:00-08:00",
                    timestamp_utc="2026-01-01T08:00:00+00:00",
                    timestamp_raw=index,
                    date_edited_local=None,
                    date_edited_utc=None,
                    date_edited_raw=0,
                    direction="outgoing" if index % 2 else "incoming",
                    service="iMessage",
                    handle="+12025550102",
                    is_from_me=index % 2,
                    is_sent=1,
                    is_delivered=1,
                    is_read=1,
                    error=0,
                    item_type=0,
                    balloon_bundle_id=None,
                    payload_data_bytes=0,
                    payload_data_sha256=None,
                    payload_metadata=[],
                    share_direction=0,
                    render_kind="message",
                    associated_message_guid=None,
                    associated_message_type=0,
                    reply_to_guid=None,
                )
                for index in range(1, 81)
            ]
            exporter.write_html(
                root / "thread.html",
                messages,
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:29:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )

            script = textwrap.dedent(
                """
                import { pathToFileURL } from "node:url";
                import { chromium } from "playwright";

                const htmlPath = process.argv[1];
                const browser = await chromium.launch({ headless: true });
                try {
                  const page = await browser.newPage({
                    viewport: { width: 816, height: 1056 },
                    deviceScaleFactor: 1,
                  });
                  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
                  await page.waitForFunction(
                    () => document.getElementById("transcript")?.dataset.paginated === "true",
                    null,
                    { timeout: 30000 }
                  );
                  const stats = await page.evaluate(() => {
                    const pageCounts = Array.from(document.querySelectorAll(".page")).map(
                      (node) => node.querySelectorAll(".message").length
                    );
                    const sequences = Array.from(document.querySelectorAll(".page .message")).map(
                      (node) => node.dataset.sequence
                    );
                    return {
                      pageCounts,
                      sequences,
                      uniqueSequences: new Set(sequences).size,
                    };
                  });
                  console.log(JSON.stringify(stats));
                } finally {
                  await browser.close();
                }
                """
            )
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(root / "thread.html")],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"Playwright pagination smoke test unavailable: {result.stderr.strip()}")

            stats = json.loads(result.stdout)
            self.assertLess(len(stats["pageCounts"]), len(messages))
            self.assertTrue(any(count > 1 for count in stats["pageCounts"][1:]))
            self.assertEqual(len(messages), len(stats["sequences"]))
            self.assertEqual(len(messages), stats["uniqueSequences"])

    def test_paginate_js_keeps_fitting_multi_photo_message_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachments_dir = root / "attachments"
            attachments_dir.mkdir()
            exporter.write_paginate_js(root / "paginate.js")
            attachments = []
            for index in range(1, 4):
                filename = f"fit-{index}.png"
                png = self._png_bytes(96, 64)
                (attachments_dir / filename).write_bytes(png)
                attachments.append(
                    self._attachment_with_signature(
                        source_rowid=index,
                        filename=filename,
                        signature=png,
                    )
                )
            message = self._message(
                sequence=2,
                text="Synthetic attachment group",
                direction="outgoing",
                attachments=attachments,
            )
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:01:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )

            script = textwrap.dedent(
                """
                import { pathToFileURL } from "node:url";
                import { chromium } from "playwright";

                const htmlPath = process.argv[1];
                const browser = await chromium.launch({ headless: true });
                try {
                  const page = await browser.newPage({
                    viewport: { width: 816, height: 1056 },
                    deviceScaleFactor: 1,
                  });
                  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
                  await page.waitForFunction(
                    () => document.getElementById("transcript")?.dataset.paginated === "true",
                    null,
                    { timeout: 30000 }
                  );
                  await page.waitForFunction(
                    () => Array.from(document.images).every((img) => img.complete),
                    null,
                    { timeout: 30000 }
                  );
                  const stats = await page.evaluate(() => {
                    const blocks = Array.from(document.querySelectorAll(".page .message[data-sequence='2']"));
                    return {
                      messageBlocks: blocks.length,
                      cards: document.querySelectorAll(".forensic-attachment-card").length,
                      continuations: document.querySelectorAll(".message[data-continuation-part]").length,
                      exhibits: document.querySelectorAll(".attachment-exhibit").length,
                      oversized: document.querySelectorAll("[data-oversized='true']").length,
                      emptyPages: Array.from(document.querySelectorAll(".page")).filter(
                        (node) => node.querySelectorAll(".message").length === 0
                      ).length,
                    };
                  });
                  console.log(JSON.stringify(stats));
                } finally {
                  await browser.close();
                }
                """
            )
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(root / "thread.html")],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"Playwright pagination fit test unavailable: {result.stderr.strip()}")

            stats = json.loads(result.stdout)
            self.assertEqual(stats["messageBlocks"], 1)
            self.assertEqual(stats["cards"], 3)
            self.assertEqual(stats["exhibits"], 0)
            self.assertEqual(stats["continuations"], 0)
            self.assertEqual(stats["oversized"], 0)
            self.assertEqual(stats["emptyPages"], 0)

    def test_paginate_js_splits_oversized_multi_photo_message_at_card_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachments_dir = root / "attachments"
            attachments_dir.mkdir()
            exporter.write_paginate_js(root / "paginate.js")
            attachments = []
            for index in range(1, 7):
                filename = f"large-{index}.png"
                png = self._png_bytes(640, 520)
                (attachments_dir / filename).write_bytes(png)
                attachments.append(
                    self._attachment_with_signature(
                        source_rowid=index,
                        filename=filename,
                        signature=png,
                    )
                )
            message = self._message(
                sequence=2,
                text="Synthetic attachment group",
                direction="outgoing",
                attachments=attachments,
            )
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:01:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )

            script = textwrap.dedent(
                """
                import { pathToFileURL } from "node:url";
                import { chromium } from "playwright";

                const htmlPath = process.argv[1];
                const browser = await chromium.launch({ headless: true });
                try {
                  const page = await browser.newPage({
                    viewport: { width: 816, height: 1056 },
                    deviceScaleFactor: 1,
                  });
                  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
                  await page.waitForFunction(
                    () => document.getElementById("transcript")?.dataset.paginated === "true",
                    null,
                    { timeout: 30000 }
                  );
                  await page.waitForFunction(
                    () => Array.from(document.images).every((img) => img.complete),
                    null,
                    { timeout: 30000 }
                  );
                  const stats = await page.evaluate(() => {
                    const blocks = Array.from(document.querySelectorAll(".page .message[data-sequence='2']"));
                    const cardsPerBlock = blocks.map(
                      (node) => node.querySelectorAll(".forensic-attachment-card").length
                    );
                    return {
                      messageBlocks: blocks.length,
                      cardsPerBlock,
                      cards: document.querySelectorAll(".forensic-attachment-card").length,
                      continuationNotes: document.querySelectorAll(".continuation-note").length,
                      continuations: document.querySelectorAll(".message[data-continuation-part]").length,
                      exhibits: document.querySelectorAll(".attachment-exhibit").length,
                      oversized: document.querySelectorAll("[data-oversized='true']").length,
                      emptyPages: Array.from(document.querySelectorAll(".page")).filter(
                        (node) => node.querySelectorAll(".message").length === 0
                      ).length,
                    };
                  });
                  console.log(JSON.stringify(stats));
                } finally {
                  await browser.close();
                }
                """
            )
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(root / "thread.html")],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"Playwright pagination split test unavailable: {result.stderr.strip()}")

            stats = json.loads(result.stdout)
            self.assertEqual(stats["messageBlocks"], 6)
            self.assertEqual(stats["cards"], 6)
            self.assertEqual(stats["exhibits"], 0)
            self.assertEqual(stats["cardsPerBlock"], [1, 1, 1, 1, 1, 1])
            self.assertEqual(stats["continuationNotes"], 6)
            self.assertEqual(stats["continuations"], 6)
            self.assertEqual(stats["oversized"], 0)
            self.assertEqual(stats["emptyPages"], 0)

    def test_paginate_js_removes_edited_summary_from_attachment_continuations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachments_dir = root / "attachments"
            attachments_dir.mkdir()
            exporter.write_paginate_js(root / "paginate.js")
            attachments = []
            for index in range(1, 7):
                filename = f"edited-large-{index}.png"
                png = self._png_bytes(640, 520)
                (attachments_dir / filename).write_bytes(png)
                attachments.append(
                    self._attachment_with_signature(
                        source_rowid=index,
                        filename=filename,
                        signature=png,
                    )
                )
            message = self._message(
                sequence=2,
                source_rowid=22,
                text="Synthetic edited attachment group",
                direction="outgoing",
                attachments=attachments,
                date_edited_raw=1,
                date_edited_local="2026-01-01T00:05:00-08:00",
                edit_history=[
                    {"text": "Original attachment caption", "timestamp_local": "2026-01-01T00:00:00-08:00"}
                ],
            )
            exporter.write_html(
                root / "thread.html",
                [message],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-01-01T00:00:00-08:00",
                    "last_local": "2026-01-01T00:01:00-08:00",
                },
                "Synthetic Contact",
                "2026-01-01T00:30:00-08:00",
                "0" * 64,
            )

            script = textwrap.dedent(
                """
                import { pathToFileURL } from "node:url";
                import { chromium } from "playwright";

                const htmlPath = process.argv[1];
                const browser = await chromium.launch({ headless: true });
                try {
                  const page = await browser.newPage({
                    viewport: { width: 816, height: 1056 },
                    deviceScaleFactor: 1,
                  });
                  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
                  await page.waitForFunction(
                    () => document.getElementById("transcript")?.dataset.paginated === "true",
                    null,
                    { timeout: 30000 }
                  );
                  await page.waitForFunction(
                    () => Array.from(document.images).every((img) => img.complete),
                    null,
                    { timeout: 30000 }
                  );
                  const stats = await page.evaluate(() => {
                    const blocks = Array.from(document.querySelectorAll(".page .message[data-sequence='2']"));
                    return {
                      messageBlocks: blocks.length,
                      cards: document.querySelectorAll(".forensic-attachment-card").length,
                      editedLabels: document.querySelectorAll(".edited-label").length,
                      labelsInsideBubble: document.querySelectorAll(".bubble .edited-label").length,
                      editedSummaries: document.querySelectorAll(".edited-summary").length,
                      continuationLabels: blocks.slice(1).filter(
                        (node) => node.querySelector(".edited-label")
                      ).length,
                      continuationMetas: blocks.slice(1).filter(
                        (node) => node.querySelector(".message-meta")
                      ).length,
                      emptyPages: Array.from(document.querySelectorAll(".page")).filter(
                        (node) => node.querySelectorAll(".message").length === 0
                      ).length,
                    };
                  });
                  console.log(JSON.stringify(stats));
                } finally {
                  await browser.close();
                }
                """
            )
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(root / "thread.html")],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"Playwright edited-attachment pagination test unavailable: {result.stderr.strip()}")

            stats = json.loads(result.stdout)
            self.assertEqual(stats["messageBlocks"], 6)
            self.assertEqual(stats["cards"], 6)
            self.assertEqual(stats["editedLabels"], 1)
            self.assertEqual(stats["labelsInsideBubble"], 0)
            self.assertEqual(stats["editedSummaries"], 0)
            self.assertEqual(stats["continuationLabels"], 0)
            self.assertEqual(stats["continuationMetas"], 0)
            self.assertEqual(stats["emptyPages"], 0)

    def _attachment_with_signature(
        self,
        source_rowid: int,
        filename: str,
        signature: bytes,
        total_bytes: int | None = None,
    ):
        detected = exporter.detect_media_type(signature)
        can_render = bool(detected and detected[0] in exporter.IMAGE_RENDER_MIME_TYPES)
        dimensions = exporter.detect_image_dimensions(signature)
        return exporter.AttachmentRecord(
            source_rowid=source_rowid,
            guid=f"att-{source_rowid}",
            original_filename=filename,
            resolved_path=None,
            export_filename=filename,
            mime_type=None,
            uti=None,
            transfer_name=filename,
            total_bytes=total_bytes if total_bytes is not None else len(signature),
            sha256="0" * 64,
            status="copied",
            detected_mime_type=detected[0] if detected else None,
            render_filename=filename if can_render else None,
            render_kind="image" if can_render else "metadata",
            width=dimensions[0] if dimensions else None,
            height=dimensions[1] if dimensions else None,
        )

    def _message(self, **overrides):
        data = {
            "sequence": 1,
            "source_rowid": 1,
            "guid": "synthetic-message",
            "text": "",
            "body_source": "none",
            "body_status": "empty",
            "timestamp_local": "2026-01-01T00:00:00-08:00",
            "timestamp_utc": "2026-01-01T08:00:00+00:00",
            "timestamp_raw": 1,
            "date_edited_local": None,
            "date_edited_utc": None,
            "date_edited_raw": 0,
            "direction": "incoming",
            "service": "iMessage",
            "handle": "+12025550102",
            "is_from_me": 0,
            "is_sent": 1,
            "is_delivered": 1,
            "is_read": 1,
            "error": 0,
            "item_type": 0,
            "balloon_bundle_id": None,
            "payload_data_bytes": 0,
            "payload_data_sha256": None,
            "payload_metadata": [],
            "share_direction": 0,
            "render_kind": "message",
            "associated_message_guid": None,
            "associated_message_type": 0,
            "reply_to_guid": None,
            "thread_originator_guid": None,
            "thread_originator_part": None,
            "sent_with_siri": False,
        }
        data.update(overrides)
        return exporter.MessageRecord(**data)

    def _png_bytes(self, width: int, height: int) -> bytes:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        raw = b"".join(b"\x00" + (b"\xf8\xf9\xfa" * width) for _ in range(height))
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk("IHDR".encode(), struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk("IDAT".encode(), zlib.compress(raw))
            + chunk("IEND".encode(), b"")
        )

    def _jpeg_bytes(self, width: int, height: int) -> bytes:
        return (
            b"\xff\xd8"
            + b"\xff\xe0"
            + struct.pack(">H", 16)
            + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\xff\xc0"
            + struct.pack(">H", 17)
            + b"\x08"
            + struct.pack(">HH", height, width)
            + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
            + b"\xff\xd9"
        )

    def _decode_with_fake_typedstream(self, unarchived):
        original_typedstream = exporter.typedstream

        class FakeTypedStream:
            @staticmethod
            def unarchive_from_data(_blob):
                return unarchived

        try:
            exporter.typedstream = FakeTypedStream()
            return exporter.decode_body(None, b"synthetic-archive")
        finally:
            exporter.typedstream = original_typedstream

    def _decode_with_failing_typedstream(self, blob: bytes):
        original_typedstream = exporter.typedstream

        class FailingTypedStream:
            @staticmethod
            def unarchive_from_data(_blob):
                raise ValueError("synthetic malformed typedstream")

        try:
            exporter.typedstream = FailingTypedStream()
            return exporter.decode_body(None, blob)
        finally:
            exporter.typedstream = original_typedstream

    def _fake_archived_object(self, class_name: str, contents=None):
        return type(
            "FakeArchivedObject",
            (),
            {
                "clazz": type("FakeClass", (), {"name": class_name.encode("utf-8")})(),
                "contents": contents or [],
            },
        )()

    def _fake_typed_value(self, values):
        return type("FakeTypedValue", (), {"values": values})()

    def _fake_nsstring(self, value: str):
        return type(
            "FakeNSString",
            (),
            {
                "clazz": type("FakeClass", (), {"name": b"NSString"})(),
                "value": value,
            },
        )()

    def _create_db(self, db: Path, attachment_file: Path):
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE chat (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              service_name TEXT,
              style INTEGER,
              display_name TEXT,
              chat_identifier TEXT,
              room_name TEXT
            );
            CREATE TABLE handle (
              ROWID INTEGER PRIMARY KEY,
              id TEXT,
              service TEXT
            );
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
            CREATE TABLE message (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              text TEXT,
              attributedBody BLOB,
              date INTEGER,
              is_from_me INTEGER,
              is_sent INTEGER,
              is_delivered INTEGER,
              is_read INTEGER,
              error INTEGER,
              item_type INTEGER,
              service TEXT,
              handle_id INTEGER,
              associated_message_guid TEXT,
              associated_message_type INTEGER,
              reply_to_guid TEXT
            );
            CREATE TABLE chat_message_join (
              chat_id INTEGER,
              message_id INTEGER,
              message_date INTEGER
            );
            CREATE TABLE attachment (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              filename TEXT,
              uti TEXT,
              mime_type TEXT,
              transfer_name TEXT,
              total_bytes INTEGER
            );
            CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
            """
        )
        conn.execute(
            "INSERT INTO chat VALUES (1, 'chat-guid', 'iMessage', 45, NULL, '+12025550101', NULL)"
        )
        conn.execute("INSERT INTO handle VALUES (1, '+12025550101', 'iMessage')")
        conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
        first = 1000 * 1_000_000_000
        second = 2000 * 1_000_000_000
        conn.execute(
            "INSERT INTO message VALUES (1, 'm1', 'hello', NULL, ?, 0, 1, 1, 1, 0, 0, 'iMessage', 1, NULL, 0, NULL)",
            (first,),
        )
        conn.execute(
            "INSERT INTO message VALUES (2, 'm2', 'photo', NULL, ?, 1, 1, 1, 1, 0, 0, 'iMessage', 1, NULL, 0, NULL)",
            (second,),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (1, 1, ?)", (first,))
        conn.execute("INSERT INTO chat_message_join VALUES (1, 2, ?)", (second,))
        db_filename = str(attachment_file).replace(str(Path.home()), "~", 1)
        conn.execute(
            "INSERT INTO attachment VALUES (1, 'att-guid', ?, 'public.jpeg', 'image/jpeg', 'photo.jpg', 9)",
            (db_filename,),
        )
        conn.execute("INSERT INTO message_attachment_join VALUES (2, 1)")
        conn.commit()
        conn.close()

    def test_timestamp_layout_and_header_year_audit(self):
        # 1. Test local timestamp formatting
        ts1 = "2026-05-23T14:36:21-08:00"
        formatted1 = exporter.format_human_timestamp(ts1)
        self.assertEqual(formatted1, "Sat, May 23 at 2:36 PM")
        
        # 2. Test UTC metadata timestamp and structure
        msg = self._message(
            sequence=995,
            source_rowid=11389,
            guid="some-message-guid",
            timestamp_local="2026-05-23T14:36:21-08:00",
            timestamp_utc="2026-05-23T22:36:21Z",
            text="Clean layout test message",
        )
        
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread.html",
                [msg],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2026-05-23T14:36:21-08:00",
                    "last_local": "2026-05-23T14:36:21-08:00",
                },
                "Contact Name",
                "2026-06-09T12:00:00-07:00",
                "0" * 64,
            )
            html = (root / "thread.html").read_text(encoding="utf-8")
            
        self.assertIn("Sat, May 23 at 2:36 PM", html)
        self.assertNotIn("Saturday, May 23", html)
        self.assertNotIn("2:36:21 PM", html)
        self.assertIn("UTC: 2026-05-23T22:36:21Z", html)
        self.assertIn("row: 11389", html)
        self.assertIn("guid: <span class=\"meta-guid\">some-message-guid</span>", html)

        # 3. Test year divider generation
        msg_2025 = self._message(
            sequence=1,
            timestamp_local="2025-12-31T23:59:00-08:00",
            timestamp_utc="2026-01-01T07:59:00Z",
            text="End of 2025",
        )
        msg_2026 = self._message(
            sequence=2,
            timestamp_local="2026-01-01T00:01:00-08:00",
            timestamp_utc="2026-01-01T08:01:00Z",
            text="Start of 2026",
        )
        
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exporter.write_html(
                root / "thread_divider.html",
                [msg_2025, msg_2026],
                {
                    "chat_id": 1,
                    "handles": ["+12025550102"],
                    "first_local": "2025-12-31T23:59:00-08:00",
                    "last_local": "2026-01-01T00:01:00-08:00",
                },
                "Contact Name",
                "2026-06-09T12:00:00-07:00",
                "0" * 64,
            )
            html_divider = (root / "thread_divider.html").read_text(encoding="utf-8")

        self.assertIn('class="message year-divider"', html_divider)
        self.assertIn("data-local-year=\"2026\"", html_divider)
        self.assertIn("2026", html_divider)


if __name__ == "__main__":
    unittest.main()
