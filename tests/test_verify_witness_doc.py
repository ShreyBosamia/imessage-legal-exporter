import json
import tempfile
import unittest
from pathlib import Path

from verify_witness_doc import ExportTimeline, audit_witness_document


class VerifyWitnessDocumentTests(unittest.TestCase):
    def _write_export(self, root: Path) -> Path:
        export = root / "synthetic"
        export.mkdir()
        rows = [
            {
                "type": "message",
                "sequence": 7,
                "source_rowid": 70,
                "timestamp_local": "2026-02-14T10:00:00-08:00",
                "direction": "incoming",
                "text": "Synthetic confirmation phrase",
            },
            {
                "type": "call",
                "sequence": 3,
                "source_rowid": 30,
                "timestamp_local": "2026-02-14T11:00:00-08:00",
                "direction": "outgoing",
                "answered_label": "Answered",
                "call_type_label": "phone_or_audio_call",
                "duration_seconds": 42.0,
            },
        ]
        (export / "timeline.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        return export

    def test_audit_matches_date_quote_and_call_without_returning_body(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = self._write_export(root)
            document = root / "witness.md"
            document.write_text(
                "## Feb 14, 2026\n"
                "Message #7: \"Synthetic confirmation phrase\"\n"
                "Call event \\#3 was answered.\n",
                encoding="utf-8",
            )

            report = audit_witness_document(
                document, {"synthetic": ExportTimeline(export)}
            )

            self.assertEqual(report["reference_occurrences"], 2)
            self.assertEqual(report["status_counts"]["verified_by_quote"], 1)
            self.assertEqual(report["status_counts"]["verified_by_date"], 1)
            self.assertNotIn("Synthetic confirmation phrase", json.dumps(report))
            message_ref = report["references"][0]
            self.assertTrue(message_ref["candidates"][0]["quoted_text_matches"])
            call_ref = report["references"][1]
            self.assertEqual(call_ref["candidates"][0]["duration_seconds"], 42.0)

    def test_plain_call_time_range_is_not_a_call_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = self._write_export(root)
            document = root / "witness.md"
            document.write_text("I spoke to them on a call 10-15 minutes ago.\n", encoding="utf-8")

            report = audit_witness_document(
                document, {"synthetic": ExportTimeline(export)}
            )

            self.assertEqual(report["reference_occurrences"], 0)

    def test_call_event_list_keeps_following_numbers_as_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = self._write_export(root)
            document = root / "witness.md"
            document.write_text(
                "Call events \\#3, \\#4, and \\#5 occurred.\n", encoding="utf-8"
            )

            report = audit_witness_document(
                document, {"synthetic": ExportTimeline(export)}
            )

            self.assertEqual(
                [(x["kind"], x["sequence"]) for x in report["references"]],
                [("call", 3), ("call", 4), ("call", 5)],
            )

    def test_full_month_heading_matches_event_date(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = self._write_export(root)
            document = root / "witness.md"
            document.write_text(
                "### Wednesday, February 14, 2026\nMessage #7\n", encoding="utf-8"
            )

            report = audit_witness_document(
                document, {"synthetic": ExportTimeline(export)}
            )

            self.assertEqual(report["status_counts"], {"verified_by_date": 1})

    def test_date_range_heading_matches_intermediate_event_date(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = self._write_export(root)
            document = root / "witness.md"
            document.write_text(
                "### Wednesday, February 13, 2026, through Friday, February 15, 2026\n"
                "Message #7\n",
                encoding="utf-8",
            )

            report = audit_witness_document(
                document, {"synthetic": ExportTimeline(export)}
            )

            self.assertEqual(report["status_counts"], {"verified_by_date": 1})

    def test_large_embedded_image_line_is_not_treated_as_references(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = self._write_export(root)
            document = root / "witness.md"
            document.write_text(
                "## Feb 14, 2026\n"
                + "data:image/png;base64," + ("A" * 10_001) + "\n",
                encoding="utf-8",
            )

            report = audit_witness_document(
                document, {"synthetic": ExportTimeline(export)}
            )

            self.assertEqual(report["reference_occurrences"], 0)
            self.assertEqual(report["embedded_image_lines"], 1)

    def test_source_label_disambiguates_shared_call_sequence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            alpha = self._write_export(root)
            beta = root / "beta"
            beta.mkdir()
            rows = [
                {
                    "type": "call",
                    "sequence": 3,
                    "source_rowid": 300,
                    "timestamp_local": "2026-02-15T11:00:00-08:00",
                    "direction": "incoming",
                    "answered_label": "Answered",
                    "call_type_label": "phone_or_audio_call",
                    "duration_seconds": 10.0,
                }
            ]
            (beta / "timeline.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            document = root / "witness.md"
            document.write_text(
                "## February 14, 2026\n"
                "**Alpha export:** Call event #3 occurred.\n",
                encoding="utf-8",
            )

            report = audit_witness_document(
                document,
                {"alpha": ExportTimeline(alpha), "beta": ExportTimeline(beta)},
            )

            self.assertEqual(report["status_counts"], {"verified_by_date": 1})
            self.assertEqual(report["references"][0]["line_hints"], ["alpha"])

    def test_arbitrary_source_label_disambiguates_shared_sequence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self._write_export(root)
            second = root / "second"
            second.mkdir()
            (second / "timeline.jsonl").write_text(
                json.dumps(
                    {
                        "type": "message",
                        "sequence": 7,
                        "source_rowid": 700,
                        "timestamp_local": "2026-02-15T10:00:00-08:00",
                        "direction": "outgoing",
                        "text": "Entirely fictional alternative",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            document = root / "witness.md"
            document.write_text(
                "## February 14, 2026\n"
                "**Example One export:** Message #7 occurred.\n",
                encoding="utf-8",
            )

            report = audit_witness_document(
                document,
                {
                    "Example One": ExportTimeline(first),
                    "Example Two": ExportTimeline(second),
                },
            )

            self.assertEqual(report["status_counts"], {"verified_by_date": 1})
            self.assertEqual(
                report["references"][0]["line_hints"], ["Example One"]
            )


if __name__ == "__main__":
    unittest.main()
