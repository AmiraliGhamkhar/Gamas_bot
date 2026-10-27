"""The migration gate CI runs, executed locally as an ordinary test.

CI builds a fresh database, applies every ``migrations/v3/*.sql`` file and
asserts the resulting schema holds exactly the expected tables. That check used
to live only in the workflow YAML, so adding a table (migration 013 did) turned
the build red without any local test noticing. Both sides now read
``db.APPLICATION_TABLES``, and this module proves the invariant in-process.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from gamas_bot.db import APPLICATION_TABLES, MIGRATIONS_DIR, Database


class MigrationSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "gate.sqlite3")
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def test_every_migration_is_recorded(self):
        rows = await self.db.db.execute_fetchall(
            "SELECT name FROM schema_migrations ORDER BY name"
        )
        applied = [row["name"] for row in rows]
        expected = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))
        self.assertTrue(expected, "no v3 migrations were found")
        self.assertEqual(applied, expected)

    async def test_application_tables_are_exactly_the_declared_set(self):
        rows = await self.db.db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        )
        tables = {row["name"] for row in rows}
        self.assertEqual(
            tables ^ set(APPLICATION_TABLES),
            set(),
            "the schema and db.APPLICATION_TABLES disagree",
        )

    async def test_migration_013_tables_exist_with_their_guards(self):
        for table in ("free_uses", "documents"):
            self.assertIn(table, APPLICATION_TABLES)
            rows = await self.db.db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            self.assertEqual(len(rows), 1, table)
        # The free-use flags are booleans by construction, not by convention.
        await self.db.ensure_user(11)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db._write(
                "INSERT INTO free_uses(user_id, free_deck_used, free_quiz_used, "
                "updated_at) VALUES (?, 2, 0, '2026-01-01')",
                (11,),
            )
        # A document cannot claim a source type the extractor cannot produce.
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db._write(
                "INSERT INTO documents(user_id, file_msg_ref, title, source_type, "
                "page_count, sha256, text, chunks_json, created_at) "
                "VALUES (?, '', 'x', 'xlsx', 1, 'h', 't', '[]', '2026-01-01')",
                (11,),
            )


if __name__ == "__main__":
    unittest.main()
