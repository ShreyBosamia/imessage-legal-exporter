import sqlite3
import tempfile
import unittest
from pathlib import Path

import check_compatibility as compatibility


class CompatibilityCheckTests(unittest.TestCase):
    def test_accepts_minimal_supported_messages_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "chat.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE chat (ROWID INTEGER PRIMARY KEY);
                    CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, is_from_me INTEGER);
                    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                    CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
                    CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
                    """
                )

            result = compatibility.check_database(
                database,
                label="Messages database",
                required_tables=compatibility.CHAT_TABLES,
                required_columns=compatibility.CHAT_COLUMNS,
            )

        self.assertEqual(result.status, "PASS")

    def test_rejects_incompatible_call_schema_without_reading_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "CallHistory.storedata"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE unrelated (value TEXT)")

            result = compatibility.check_database(
                database,
                label="Call-history database",
                required_tables=compatibility.CALL_TABLES,
                required_columns=compatibility.CALL_COLUMNS,
            )

        self.assertEqual(result.status, "FAIL")
        self.assertIn("missing tables", result.detail)


if __name__ == "__main__":
    unittest.main()
