# Synthetic walkthrough

This walkthrough is fictional. Its labels, identifiers, dates, event numbers,
and message text do not describe a real person or matter.

## Scenario

An operator named `Example User` needs a transcript of a conversation labeled
`Example Contact`. The copied Messages database is stored at
`private_input/chat_copy.db`, the selected conversation has chat ID `123`, and
the optional copied call database is stored at
`private_input/call_history_copy.db`.

Run the privacy-safe compatibility check first:

```bash
python3 check_compatibility.py \
  --chat-db private_input/chat_copy.db \
  --call-db private_input/call_history_copy.db
```

Then create the combined package:

```bash
python3 imessage_legal_exporter.py export \
  --db private_input/chat_copy.db \
  --chat-id 123 \
  --label "Example Contact" \
  --owner-label "Example User" \
  --timezone America/Los_Angeles \
  --call-db private_input/call_history_copy.db \
  --call-source-description "Copied from an archived device backup for this fictional example" \
  --out exports/example-contact
```

## Review order

1. Read `extraction_report.md` and resolve warnings.
2. Verify `manifest.sha256` from inside the export directory.
3. Compare representative events with the source copy.
4. Review `thread_with_calls.pdf` for presentation and pagination.
5. Preserve the structured JSONL/CSV files, attachments, source hashes, and
   acquisition notes with the PDF.
6. Ask counsel or a qualified examiner which artifacts are relevant and how
   they should be authenticated, redacted, preserved, or produced.

The PDF is a readable derivative, not a substitute for the source database and
machine-readable records. See [Output and legal-use guide](OUTPUT_GUIDE.md) for
the purpose and sensitivity of every generated file.
