# tests/AGENTS.md

This directory contains unit tests for the `imessage-legal-exporter` codebase.

---

## 1. Running Tests

To run the full test suite, execute:
```bash
python3 -m unittest discover -s tests
```

---

## 2. Test Guidelines & Synthetic Fixtures

* **Privacy Compliance**: Never run tests against real databases containing private message history in the automated test runs.
* **Synthetic Fixtures**: Always use synthetic, mock, or dynamically created SQLite test databases (like the in-memory or temp-file databases used in `tests/test_exporter.py` and `tests/test_call_history_exporter.py`).
* **Do Not Commit Test Outputs**: Any files generated during tests (e.g., temporary HTML exports, databases, pdfs) must be cleaned up during the test teardown or configured to write to temporary directories (e.g., `tempfile.TemporaryDirectory`).
* **Assertions**: Ensure assertions cover both standard layout output (timestamps, bubble contents, metadata blocks) and edge cases (reactions, edit revisions, empty/missing message text, FaceTime providers).
