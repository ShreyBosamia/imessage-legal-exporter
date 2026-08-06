# Output and legal-use guide

This guide explains the generated files; it is not legal advice. Relevance,
authentication, hearsay, completeness, privilege, redaction, and production rules
depend on the matter and jurisdiction.

## Message package

| File | Purpose | Typical use |
| --- | --- | --- |
| `thread.pdf` | Paginated message-only presentation | Human review, counsel review, and potential exhibit preparation. |
| `thread_with_calls.pdf` | Combined message and matched-call chronology | Human review when neutral call context matters. |
| `thread.html` / `thread_with_calls.html` | Source views used to render PDFs | Re-rendering and visual inspection. Keep `paginate.js` beside them. |
| `messages.csv` | Spreadsheet-friendly message metadata | Filtering, review, and reconciliation. UTF-8 with BOM for spreadsheet compatibility. |
| `messages.jsonl` | Highest-detail message export | Machine review, edit histories, attachment metadata, and audit support. |
| `timeline.jsonl` | Chronological message/call event stream | Cross-reference checking and downstream tooling. |
| `call_records.jsonl` | Matched call records copied into a combined package | Machine support for combined call cards. Contains sensitive raw metadata. |
| `attachments/` | Selected copied attachment payloads | Native-file review and hash comparison. |
| `derived_media/` | Generated previews from otherwise difficult payloads | Convenience review; these are derived, not original payloads. |
| `extraction_report.md` | Counts, provenance, decode status, and warnings | Quality control and examiner/counsel review. |
| `manifest.sha256` | Source and output hashes | Integrity checking. It is not a digital signature or proof of who acquired the data. |
| `certification_template.md` | Blank factual worksheet | Complete only with facts personally known to the signer. |
| `pdf_render_error.txt` | Renderer failure details | Troubleshooting; the HTML and structured exports may still be usable. |

## Standalone call package

| File | Purpose |
| --- | --- |
| `call_history.pdf` / `.html` | Readable matched-call presentation. |
| `call_records.csv` / `.jsonl` | Structured matched calls, including private raw values. |
| `call_history_report.md` | Acquisition statement, matching method, counts, and limitations. |
| `schema.sql` | Schema captured from the supplied call database. |
| `source/call_history_copy.db` | Preservation copy used by the standalone call exporter. Highly sensitive. |
| `source_manifest.sha256` | Hashes the copied call database and generated call outputs. |

## What to preserve

For an evidence-oriented workflow, preserve at least:

1. the original copied source databases and their hashes;
2. the complete generated package, not only the PDF;
3. the original attachment files copied by the exporter;
4. the acquisition notes, date, operator, device/backup identity, and tools used;
5. the exact software version or Git commit; and
6. a read-only or otherwise controlled preservation copy.

The PDF should be traceable back to structured rows and source identifiers. Avoid
editing a generated PDF or CSV in place. If redaction is required, preserve the
unredacted package and create a separately named derivative with a documented
process.

## Sensitive files

Every output should be treated as confidential. The most sensitive are source
databases, JSONL/CSV files, attachments, HTML, schema, and reports containing
paths or acquisition descriptions. A visually redacted PDF does not redact its
companion files.

## Limitations to disclose or review

- `fallback` or `undecoded` bodies may need manual verification.
- Missing attachments can reflect deletion, cloud eviction, inaccessible source
  paths, or an incomplete acquisition.
- Active Call History rows may not represent all historical calls.
- Private Apple enums are preserved but cautiously labeled.
- Matching calls to a conversation is handle-based and should be reviewed.
- A hash establishes file consistency, not authorship, completeness, or truth.
- Human-facing labels are operator-supplied display aids.
- The tool does not recover deleted SQLite records, WAL-only history, or free-page
  content as a forensic suite might.

## Before sharing with counsel or another party

- Verify hashes and counts.
- Review every warning in the reports.
- Confirm labels and timezone.
- Check for unrelated, privileged, sealed, or protected content.
- Agree on whether to provide the PDF, native attachments, structured metadata,
  source database, or a narrower production set.
- Transfer the package through an approved secure channel.
