"""WSGI readiness/liveness checks are read-only and do not start the bot."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import mini_app_wsgi


class MiniAppWsgiProbeTests(unittest.TestCase):
    def test_health_only_checks_that_the_configured_db_file_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "empty.sqlite"
            db_path.touch()
            with patch.object(mini_app_wsgi, "_settings", return_value=SimpleNamespace(db_path=db_path)):
                self.assertEqual(
                    mini_app_wsgi._health(),
                    {"service": "gamas-mini-app", "ok": True, "database": True},
                )

    def test_readiness_requires_a_readable_db_and_core_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "gamas.sqlite"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                "CREATE TABLE users (telegram_id INTEGER PRIMARY KEY);"
                "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY);"
            )
            connection.close()
            with patch.object(mini_app_wsgi, "_settings", return_value=SimpleNamespace(db_path=db_path)):
                self.assertEqual(
                    mini_app_wsgi._readiness(),
                    {"service": "gamas-mini-app", "ok": True, "database": True, "schema": True},
                )

    def test_readiness_rejects_a_missing_or_schema_incomplete_db(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite"
            with patch.object(mini_app_wsgi, "_settings", return_value=SimpleNamespace(db_path=missing)):
                self.assertFalse(mini_app_wsgi._readiness()["ok"])

            incomplete = Path(directory) / "empty.sqlite"
            sqlite3.connect(incomplete).close()
            with patch.object(mini_app_wsgi, "_settings", return_value=SimpleNamespace(db_path=incomplete)):
                result = mini_app_wsgi._readiness()
            self.assertTrue(result["database"])
            self.assertFalse(result["schema"])
            self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
