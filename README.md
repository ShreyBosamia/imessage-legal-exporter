# iMessage Legal Exporter

A local, open-source command-line tool that turns a copied Apple Messages
`chat.db` thread—and, optionally, matching `CallHistory.storedata` records—into
a readable PDF transcript plus structured metadata, copied attachments, reports,
and SHA-256 manifests.

The tool does not upload data. It is a technical evidence-preparation aid, not
legal advice, a forensic acquisition suite, or a guarantee of admissibility.
Have counsel or a qualified examiner review any production or filing.

## Compatibility at a glance

- Runtime: Python 3.10 or newer, Node.js, and Playwright Chromium.
- Acquisition: macOS is required for copying the local Messages database and
  attachments. The parsers and synthetic tests can run elsewhere.
- Messages input: a readable SQLite copy of macOS `~/Library/Messages/chat.db`.
- Calls input: a readable `CallHistory.storedata` SQLite database copied from a
  Mac call-history cache or extracted from an iPhone backup.
- iPhone backups: this repository does not decrypt or parse Finder backup
  containers. Extract the logical call-history file first.
- Version support: Apple does not publish these private SQLite schemas. The
  project has been exercised on the maintainer's source artifacts and synthetic
  fixtures, not every iOS or macOS release. Run the included preflight checker
  before relying on a new version.

See [Compatibility](docs/COMPATIBILITY.md) for the support policy and known
limitations.

## Privacy warning

Exports contain private communications, contact identifiers, call metadata, and
attachments. Keep `private_input/` and `exports/` out of Git, cloud-synced public
folders, issue attachments, and pull requests. The repository ignores common
evidence outputs, but you remain responsible for checking staged files before a
commit.

## 1. Install

Clone the repository, then install the Python and PDF-rendering dependencies:

```bash
git clone https://github.com/ShreyBosamia/imessage-legal-exporter.git
cd imessage-legal-exporter
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
npm install
npm run install-browsers
```

If Playwright is unavailable, the exporter can still generate HTML, CSV, JSONL,
reports, copied attachments, and hashes. Use `--no-pdf` or install Playwright and
rerun the export later.

## 2. Allow access on macOS

Messages data is privacy-protected. Add the terminal application you will use to
**System Settings → Privacy & Security → Full Disk Access**, enable it, then
restart that terminal. Apple explains Full Disk Access in its
[macOS user guide](https://support.apple.com/guide/mac-help/allow-access-to-system-configuration-files-mchlccb25729/mac).

Work from copies, never the live databases. Create private working directories:

```bash
mkdir -p private_input exports
chmod 700 private_input exports
```

Create a consistent SQLite snapshot of Messages:

```bash
sqlite3 "$HOME/Library/Messages/chat.db" ".backup 'private_input/chat_copy.db'"
```

The default attachment source is
`~/Library/Messages/Attachments`. The exporter copies only attachments associated
with the selected conversation into the output package.

## 3. Optional: obtain call history

### From the Mac call-history cache

If this file exists on the Mac, snapshot it the same way:

```bash
sqlite3 "$HOME/Library/Application Support/CallHistoryDB/CallHistory.storedata" ".backup 'private_input/call_history_copy.db'"
```

This cache may be incomplete because retention and sync behavior are outside the
exporter's control. Describe the source accurately when exporting.

### From an iPhone Finder backup

Apple documents how to [create a local iPhone backup](https://support.apple.com/en-us/108796)
and where [Finder stores backups on a Mac](https://support.apple.com/en-us/108809).
Apple also notes that call history is included in encrypted backups. Preserve the
backup and its password, then use a trusted backup or forensic extraction tool to
extract the logical file:

```text
HomeDomain/Library/CallHistoryDB/CallHistory.storedata
```

Save the extracted, readable SQLite file as
`private_input/call_history_copy.db`. This project deliberately does not accept
backup passwords or implement backup decryption.

## 4. Run the private preflight check

The checker validates dependencies and required SQLite structure without printing
message bodies, handles, or call participants:

```bash
python3 check_compatibility.py \
  --chat-db private_input/chat_copy.db \
  --call-db private_input/call_history_copy.db \
  --attachments-root "$HOME/Library/Messages/Attachments"
```

Omit `--call-db` if you are exporting messages only. Resolve every `FAIL` before
continuing. A `WARN` describes an optional feature or an unverified environment.

## 5. Find the conversation

```bash
python3 imessage_legal_exporter.py list-threads \
  --db private_input/chat_copy.db \
  --timezone America/Los_Angeles \
  --limit 100
```

Replace the timezone with the IANA timezone that applied to the device or evidence
context. If omitted, the current system timezone is detected. The command emits
CSV and masks handles, but a stored display name can still be identifying; do not
paste its output into a public issue.

Choose the desired `chat_id` using message count, date range, display name, and
masked handles. Chat style values are private Apple implementation details and
can change; do not rely on a style number alone.

## 6. Export messages and calls

### Simple one-pass export

```bash
python3 imessage_legal_exporter.py export \
  --db private_input/chat_copy.db \
  --chat-id 123 \
  --label "Example Contact" \
  --owner-label "Example User" \
  --timezone America/Los_Angeles \
  --attachments-root "$HOME/Library/Messages/Attachments" \
  --call-db private_input/call_history_copy.db \
  --call-source-description "CallHistory.storedata extracted from an archived iPhone Finder backup" \
  --out exports/example-contact
```

Remove the two call-history options for a message-only package. `--label` and
`--owner-label` affect display text only; they do not alter the underlying source
rows preserved in structured outputs.

### Recommended evidence-oriented two-stage export

The two-stage workflow also creates a call-only report, schema, source database
copy, and call manifest:

```bash
python3 imessage_legal_exporter.py export \
  --db private_input/chat_copy.db \
  --chat-id 123 \
  --label "Example Contact" \
  --owner-label "Example User" \
  --timezone America/Los_Angeles \
  --out exports/example-contact

python3 call_history_exporter.py export \
  --call-db private_input/call_history_copy.db \
  --messages-jsonl exports/example-contact/messages.jsonl \
  --label "Example Contact" \
  --timezone America/Los_Angeles \
  --source-description "CallHistory.storedata extracted from an archived iPhone Finder backup" \
  --acquisition-command "Extracted with TOOL and preserved on YYYY-MM-DD by PERSON" \
  --out exports/example-contact-calls

python3 imessage_legal_exporter.py export \
  --db private_input/chat_copy.db \
  --chat-id 123 \
  --label "Example Contact" \
  --owner-label "Example User" \
  --timezone America/Los_Angeles \
  --calls-jsonl exports/example-contact-calls/call_records.jsonl \
  --call-source-description "CallHistory.storedata extracted from an archived iPhone Finder backup" \
  --out exports/example-contact
```

Replace every placeholder with a truthful value. The final command regenerates
the message package and adds the combined `thread_with_calls` views.

## 7. Review the result

Start with:

- `thread_with_calls.pdf` for the readable combined chronology, when calls were
  included.
- `thread.pdf` for the clean message-only transcript.
- `extraction_report.md` for counts, hashes, decode status, missing attachments,
  and limitations.
- `manifest.sha256` for integrity verification.
- `messages.jsonl`, `timeline.jsonl`, and the original source preservation copy
  when counsel or an examiner needs machine-readable support.

The PDF is a presentation view, not the sole evidence artifact. Read the complete
[Output and legal-use guide](docs/OUTPUT_GUIDE.md) before producing a package.
For a fictional end-to-end example that contains no real communications, see the
[synthetic walkthrough](docs/SYNTHETIC_EXAMPLE.md).

From inside an export directory, verify relative manifest entries with:

```bash
shasum -a 256 -c manifest.sha256
```

The manifest can include the original source database path. That entry verifies
only while the source remains at that location; preserve the source separately
with its recorded hash.

## 8. Quality-control checklist

- Record who acquired the data, when, from which device or backup, and how.
- Confirm the source hash did not change during export.
- Confirm message and call counts in the reports.
- Spot-check the first, last, and representative events against the source app or
  a qualified forensic viewer.
- Review `fallback` and `undecoded` message bodies.
- Review missing or metadata-only attachments.
- Review edited-message revision histories in CSV or JSONL.
- Confirm the chosen timezone and every human-facing label.
- Keep an untouched preservation copy; perform review on a duplicate.
- Have counsel decide what is relevant, privileged, redacted, and producible.

## Development

```bash
python3 -m unittest discover -s tests
```

Tests use synthetic SQLite fixtures. See [Contributing](CONTRIBUTING.md) for the
privacy rules and pull-request checklist.

## License

Released under the [MIT License](LICENSE).
