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

    async def test_office_exports_use_the_existing_owner_scoped_library(self):
        await self.db.ensure_user(12)
        item_id = await self.db.library_add(
            12, "office", 12, 44, None, title="سند آزاد — Word"
        )
        item = await self.db.library_item(12, item_id)
        self.assertIsNotNone(item)
        self.assertEqual(item["kind"], "office")
        self.assertIsNone(await self.db.library_item(13, item_id))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.library_add(12, "unknown", 12, 45, None, title="نامعتبر")

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


class PopulatedUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_and_interrupted_replay_preserve_all_references(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "before-014.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration.name >= "014":
                    continue
                conn.executescript(migration.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations VALUES (?, '2026-01-01')", (migration.name,))
            owner, missing_owner = 770000000123456789, 770000000123456790
            conn.execute("INSERT INTO users(telegram_id, joined_at) VALUES (?, '2026-01-01')", (owner,))
            conn.execute(
                "INSERT INTO library_items(item_id,user_id,kind,chat_id,message_id,title,created_at,content_text) "
                "VALUES (100,?,'booklet',1,2,'درس','2026-01-01','متن')", (owner,)
            )
            # A legacy row without an owner was possible before the FK existed.
            conn.execute(
                "INSERT INTO library_items(item_id,user_id,kind,chat_id,message_id,title,created_at) "
                "VALUES (101,?,'booklet',1,3,'قدیمی','2026-01-01')", (missing_owner,)
            )
            conn.execute(
                "INSERT INTO flashcard_decks(deck_id,user_id,item_id,title,source,created_at) "
                "VALUES (10,?,100,'کارت','ai','2026-01-01')", (owner,)
            )
            conn.execute("INSERT INTO flashcards(deck_id,idx,front,back,source) VALUES (10,0,'پ','ج','صفحهٔ ۱')")
            conn.execute(
                "INSERT INTO quiz_sessions(session_id,user_id,item_id,question_count,created_at,updated_at) "
                "VALUES (20,?,100,1,'2026-01-01','2026-01-01')", (owner,)
            )
            conn.execute(
                "INSERT INTO quiz_questions(question_id,session_id,idx,question,options_json,correct_index,source) "
                "VALUES (30,20,0,'پرسش','[\"a\",\"b\",\"c\",\"d\"]',0,'صفحهٔ ۱')"
            )
            conn.execute(
                "INSERT INTO quiz_answers(session_id,question_id,chosen_index,is_correct,answered_at) "
                "VALUES (20,30,0,1,'2026-01-01')"
            )
            conn.execute(
                "INSERT INTO documents(user_id,file_msg_ref,title,source_type,page_count,sha256,text,chunks_json,"
                "library_item_id,created_at) VALUES (?, '1:2','سند','pdf',1,'hash','متن','[]',100,'2026-01-01')", (owner,)
            )
            conn.execute("INSERT INTO free_uses(user_id,free_deck_used,updated_at) VALUES (?,1,'2026-01-01')", (owner,))
            conn.commit()
            conn.close()

            for replay in (False, True):
                db = Database(path)
                await db.connect()
                await db.assert_user_reference_integrity()
                self.assertEqual((await db.user_identity(missing_owner))["telegram_id"], missing_owner)
                self.assertEqual((await db.library_item_detail(owner, 100))["content_text"], "متن")
                self.assertEqual((await db.deck_cards(10))[0]["source"], "صفحهٔ ۱")
                self.assertEqual((await db.db.execute_fetchall("SELECT COUNT(*) FROM quiz_answers"))[0][0], 1)
                self.assertEqual((await db.document_get(owner, 1))["library_item_id"], 100)
                self.assertFalse((await db.free_use_state(owner))["deck"])
                self.assertEqual((await db.db.execute_fetchall("PRAGMA foreign_keys"))[0][0], 1)
                if not replay:
                    # Simulate a crash after the rebuild COMMIT but before the
                    # migration bookkeeping row was durable.
                    await db._write("DELETE FROM schema_migrations WHERE name='014_free_use_recovery.sql'")
                await db.close()
            db = Database(path)
            await db.connect()
            new_id = await db.library_add(owner, "booklet", 1, 4, None, title="جدید")
            self.assertGreater(new_id, 101)
            await db.assert_user_reference_integrity()
            await db.close()


class OfficeLibraryUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_populated_015_library_survives_office_kind_migration_and_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "before-office.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration.name >= "016_office_library_kind.sql":
                    break
                conn.executescript(migration.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations VALUES (?, '2026-01-01')", (migration.name,))
            conn.execute("INSERT INTO users(telegram_id, joined_at) VALUES (123, '2026-01-01')")
            conn.execute(
                "INSERT INTO library_items(item_id,user_id,kind,chat_id,message_id,title,created_at,content_text) "
                "VALUES (10,123,'booklet',123,44,'جزوه','2026-01-01','متن قدیمی')"
            )
            conn.execute(
                "INSERT INTO library_items(item_id,user_id,kind,chat_id,message_id,title,created_at) "
                "VALUES (11,123,'transcript',123,45,'گفتار','2026-01-02')"
            )
            conn.commit()
            conn.close()

            for replay in (False, True):
                db = Database(path)
                try:
                    await db.connect()
                    await db.assert_user_reference_integrity()
                    first = await db.library_item_detail(123, 10)
                    second = await db.library_item(123, 11)
                    self.assertEqual(
                        (first["kind"], first["title"], first["content_text"]),
                        ("booklet", "جزوه", "متن قدیمی"),
                    )
                    self.assertEqual((second["kind"], second["title"]), ("transcript", "گفتار"))
                    office_id = await db.library_add(123, "office", 123, 46, None, title="آزاد")
                    self.assertEqual((await db.library_item(123, office_id))["kind"], "office")
                    if not replay:
                        # Simulate interruption after the table rebuild committed
                        # but before its migration marker was recorded.
                        await db._write(
                            "DELETE FROM schema_migrations WHERE name='016_office_library_kind.sql'"
                        )
                finally:
                    await db.close()


class LearningChargeUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_populated_014_upgrade_and_interrupted_015_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "before-015.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration.name >= "015":
                    break
                conn.executescript(migration.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations VALUES (?, '2026-01-01')", (migration.name,))
            conn.execute("INSERT INTO users(telegram_id, joined_at) VALUES (111, '2026-01-01')")
            conn.execute(
                "INSERT INTO ledger(user_id,seconds,kind,note,created_at) "
                "VALUES (111,1000,'credit','old credit','2026-01-01')"
            )
            conn.execute(
                "INSERT INTO ledger(user_id,seconds,kind,note,created_at) "
                "VALUES (111,300,'debit','fc-pending:legacy','2026-01-02')"
            )
            conn.commit()
            conn.close()

            db = Database(path)
            await db.connect()
            await db.assert_user_reference_integrity()
            self.assertEqual(await db.available_seconds(111), 700)
            # An old debit cannot be safely identified as an interrupted job.
            self.assertEqual(await db.recover_pending_learning_charges(), 0)
            await db.spend_seconds(111, 100, "new generation", operation_id="new")
            # Simulate a crash after migration DDL but before its bookkeeping.
            await db._write("DELETE FROM schema_migrations WHERE name='015_learning_charge_recovery.sql'")
            await db.close()

            db = Database(path)
            await db.connect()
            await db.assert_user_reference_integrity()
            self.assertEqual(await db.recover_pending_learning_charges(), 1)
            self.assertEqual(await db.recover_pending_learning_charges(), 0)
            self.assertEqual(await db.available_seconds(111), 700)
            self.assertEqual(
                (await db.db.execute_fetchall("SELECT status FROM learning_charges WHERE operation_id='new'"))[0][0],
                "refunded",
            )
            await db.close()


if __name__ == "__main__":
    unittest.main()
