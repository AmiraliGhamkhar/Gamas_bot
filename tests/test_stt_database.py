from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gamas_bot.database import Database


class STTPlatformDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "gamas.sqlite3")
        await self.db.open()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_forward_migration_installs_platform_schema(self):
        async with self.db._lock:
            cursor = await self.db._db().execute(
                "SELECT name FROM schema_migrations WHERE name='012_stt_platform.sql'"
            )
            self.assertIsNotNone(await cursor.fetchone())
            cursor = await self.db._db().execute("PRAGMA table_info(provider_credentials)")
            credential_columns = {row["name"] for row in await cursor.fetchall()}
            self.assertIn("metadata_json", credential_columns)
            cursor = await self.db._db().execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stt_%'"
            )
            tables = {row["name"] for row in await cursor.fetchall()}
        self.assertTrue(
            {
                "stt_provider_registry",
                "stt_provider_settings",
                "stt_routes",
                "stt_model_registry",
                "stt_quota_snapshots",
                "stt_quota_budgets",
                "stt_quota_reservations",
                "stt_usage_records",
                "stt_provider_events",
            }.issubset(tables)
        )

    async def test_registry_metadata_settings_and_routes_round_trip(self):
        await self.db.stt_registry_sync(
            [
                {
                    "provider": "speechmatics",
                    "display_name": "Speechmatics",
                    "protocol": "batch_rest",
                    "classification": "free_credit",
                    "persian_batch": True,
                    "enabled": True,
                    "metadata": {"docs": "https://docs.example.invalid/stt"},
                }
            ]
        )
        registry = await self.db.stt_registry_list()
        self.assertEqual(registry[0]["metadata"]["docs"], "https://docs.example.invalid/stt")
        self.assertEqual(registry[0]["persian_batch"], 1)

        await self.db.stt_provider_settings_upsert(
            "speechmatics", enabled=True, billing_state="free", admin_id=77
        )
        settings = await self.db.stt_provider_settings_get("speechmatics")
        self.assertEqual(settings["billing_state"], "free")
        self.assertEqual(settings["billing_attested_by_admin_id"], 77)

        await self.db.stt_routes_replace(
            [{"provider": "speechmatics", "position": 0, "enabled": True}], admin_id=77
        )
        routes = await self.db.stt_routes_list(enabled_only=True)
        self.assertEqual([row["provider"] for row in routes], ["speechmatics"])

    async def test_quota_reservation_is_atomic_and_honors_safety_margin(self):
        await self.db.stt_quota_set_budget(
            "deepgram",
            "account-a",
            "minutes_month",
            limit=100,
            remaining=100,
            reset_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            admin_id=77,
        )

        async def reserve():
            return await self.db.stt_quota_reserve(
                "deepgram",
                "account-a",
                "minutes_month",
                needed=80,
                safety_margin=0.10,
            )

        attempts = await asyncio.gather(reserve(), reserve())
        allowed = [attempt for attempt in attempts if attempt[0] is not None]
        rejected = [attempt for attempt in attempts if attempt[0] is None]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(len(rejected), 1)
        self.assertIn(rejected[0][2], {"insufficient", "exhausted"})

        await self.db.stt_quota_reservation_finalize(allowed[0][0], commit=False)
        budget = await self.db.stt_quota_budget_get("deepgram", "account-a", "minutes_month")
        self.assertEqual(budget["remaining"], 100)


if __name__ == "__main__":
    unittest.main()
