# Compatibility

## Support statement

Apple's Messages and Call History SQLite schemas are private implementation
details. This project cannot promise compatibility with every iOS or macOS
release. A successful parse also does not prove that an Apple database contains
all historical records.

The current release policy is:

- Python 3.10 through 3.14 is exercised by automated synthetic tests.
- PDF rendering is exercised with the locked Playwright dependency.
- Real-data acquisition is macOS-oriented.
- The maintainer has validated the workflow on their own source artifacts.
- Other OS versions are supported on a best-effort, schema-compatible basis.

## What the preflight checker proves

`check_compatibility.py` confirms that a supplied file is readable SQLite, passes
SQLite `quick_check`, and contains the minimum tables and columns used by the
exporters. It does not prove:

- that every newer Apple field is decoded;
- that deleted records were recovered;
- that the database is complete;
- that attachment paths are still available;
- that call-type private enum interpretations are correct; or
- that a resulting package is legally admissible.

## Source combinations

| Source | Status | Important limitation |
| --- | --- | --- |
| macOS Messages `chat.db` | Primary supported input | Requires Full Disk Access and a copied SQLite snapshot. |
| macOS Messages attachments | Supported | Missing, evicted, or cloud-only files cannot be recreated. |
| macOS CallHistoryDB cache | Best effort | May be incomplete or affected by sync and retention. |
| Extracted iPhone `CallHistory.storedata` | Best effort | Backup extraction and decryption are outside this project. |
| Raw Finder/iTunes backup folder | Not directly supported | Extract the logical database first. |
| Live iPhone connection | Not supported | The exporter does not talk to devices. |
| Windows or Linux acquisition | Not supported | Parsing and tests can run there after compatible inputs are supplied. |

## Timezones

Apple timestamps are preserved in UTC and rendered in a local timezone. Pass an
explicit IANA name such as `America/Los_Angeles` when the evidence context matters.
If `--timezone` is omitted, the exporter detects the current system timezone. A
different review machine timezone can otherwise produce different local display
times while the UTC value remains unchanged.

## Reporting a new Apple version

Run the privacy-safe checker and report only:

```bash
python3 check_compatibility.py --chat-db /path/to/chat_copy.db
python3 check_compatibility.py --call-db /path/to/call_history_copy.db
python3 --version
sw_vers
```

Do not attach databases, exports, screenshots, message text, handles, phone
numbers, or unredacted schema dumps to a public issue. State whether the input
came from a Mac database or an extracted iPhone backup and include the exact
error with sensitive paths removed.
