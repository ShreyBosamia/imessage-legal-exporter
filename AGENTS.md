# AGENTS.md

Welcome to the `imessage-legal-exporter` project directory! This file serves as a high-level router and directory index for AI coding agents. Subdirectory-specific context, setup procedures, and testing rules have been modularized to optimize token usage and context locality.

---

## 1. Project Overview

`imessage-legal-exporter` is a Python CLI tool combined with a Playwright PDF renderer. It extracts Apple Messages (`chat.db`) and Call History (`CallHistory.storedata`) records into structured metadata packages and highly polished, court-ready PDF transcripts.

### Key Entry Points:
* `imessage_legal_exporter.py`: Handles message database parsing, HTML transcript generation, dynamic browser-based pagination logic, report formatting, and cryptographic manifest generation.
* `call_history_exporter.py`: Handles Call History database extraction, categorization (FaceTime, telephony, data calls), timeline chronological integration, and reporting.
* `render_pdf.mjs`: Node.js Playwright renderer that translates the formatted HTML exports into clean PDF transcripts.
* `verify_witness_doc.py`: A query helper utility script for AI agents to load and verify witness document references (row numbers, call durations, sequence IDs) against the unified chronological timeline.
* `check_compatibility.py`: A privacy-safe runtime and SQLite schema preflight checker.

---

## 2. Privacy & Security Rules

To comply with privacy laws and secure sensitive personal communications, follow these rules strictly:
* **No Sensitive Commits**: Never commit `exports/`, `.env`, `chat.db`, `chat_copy.db`, `call_history_copy.db`, attachments, message contents, `node_modules/`, or temporary cache files.
* **No Plain Logs**: Avoid printing real message bodies, sender identities, or phone numbers in terminal outputs, console statements, or logs.
* **Prefer Synthetic Fixtures**: Always use mock or synthetic data for testing.

---

## 3. Subdirectory Documentation Index

Refer to these nested files for specific instructions and workflows:

| Directory | Documentation Link | Contents & Scope |
| :--- | :--- | :--- |
| **`exports/`** | **`exports/AGENTS.md`** | **[GIT-IGNORED]** Local evidence paths and visual-layout auditing notes. Never commit its contents. |
| **`tests/`** | **[tests/AGENTS.md](tests/AGENTS.md)** | Unit testing setup, synthetic database fixtures, and assertions. |

---

## 4. Quick Reference Commands

```bash
# Install dependencies
python3 -m pip install -r requirements.txt
npm install

# Run the unit tests
python3 -m unittest discover -s tests
```

---

## 5. AI Agent Verification & Querying

To enable automated verification of witness statements or external documents against the evidence transcripts:
* **Unified Chronological Timeline (`timeline.jsonl`)**: Each export directory contains `timeline.jsonl` which merges messages and call events, sorted chronologically. This file preserves sequence numbers (`sequence`) and database row IDs (`source_rowid`) exactly as printed in the human-facing PDF/HTML.
* **Helper Utility (`verify_witness_doc.py`)**: AI agents can import or execute `verify_witness_doc.py` to quickly lookup messages/calls by sequence ID, search text, or perform timeline queries.

---

## 6. Legal Caveat

This tool prepares evidence-style transcripts but does not constitute legal advice. Admissibility requirements vary across jurisdictions and court levels.
