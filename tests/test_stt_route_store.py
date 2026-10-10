"""Admin-edited STT route store tests (spec §28/§61/§66)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gamas_bot.database import Database
from gamas_bot.stt import _db_route_plan, _job_policy, _settings_with_model
from gamas_bot.stt_platform.policy import SttPolicy
from gamas_bot.stt_platform.router import (
    CandidateFacts,
    SttRequirements,
    plan_route,
    resolve_route,
)

from support import make_settings


class RouteStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "routes.sqlite3")
        await self.db.open()
        self.settings = make_settings()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_empty_table_falls_back_to_settings_route(self):
        self.assertIsNone(await _db_route_plan(self.db))
        self.assertEqual(
            resolve_route(self.settings),
            ("speechmatics", "deepgram", "openai_compatible"),
        )

    async def test_db_rows_supply_route_and_model_overrides(self):
        await self.db.stt_routes_replace(
            [
                {"provider": "gemini_transcribe", "position": 0, "enabled": True},
                {
                    "provider": "groq",
                    "position": 1,
                    "enabled": True,
                    "model_override": "whisper-large-v3-turbo",
                },
                {"provider": "deepgram", "position": 2, "enabled": False},
            ],
            admin_id=7,
        )
        route, overrides = await _db_route_plan(self.db)
        self.assertEqual(route, ("gemini_transcribe", "groq"))
        self.assertEqual(overrides, {"groq": "whisper-large-v3-turbo"})

    async def test_disabled_rows_keep_their_model_override_out_of_the_plan(self):
        await self.db.stt_routes_replace(
            [
                {"provider": "groq", "position": 0, "enabled": True},
                {"provider": "deepgram", "position": 1, "enabled": False, "model_override": "nova-2"},
            ],
            admin_id=7,
        )
        route, overrides = await _db_route_plan(self.db)
        self.assertEqual(route, ("groq",))
        self.assertEqual(overrides, {})

    def test_model_override_lands_in_the_provider_specific_field(self):
        settings = _settings_with_model(self.settings, "deepgram", "nova-2")
        self.assertEqual(settings.deepgram_model, "nova-2")
        self.assertEqual(self.settings.deepgram_model, "nova-3")

        settings = _settings_with_model(self.settings, "speechmatics", "standard")
        self.assertEqual(settings.speechmatics_operating_point, "standard")

        settings = _settings_with_model(self.settings, "openai_compatible", "whisper-1-ft")
        self.assertEqual(settings.stt_openai_model, "whisper-1-ft")

        settings = _settings_with_model(self.settings, "groq", "whisper-large-v3-turbo")
        self.assertEqual(settings.stt_model("groq"), "whisper-large-v3-turbo")

    def test_plan_route_honors_the_passed_route(self):
        req = SttRequirements.for_job(
            language="fa", file_bytes=1000, duration_seconds=10.0
        )
        facts = {
            name: CandidateFacts(has_credential=True, admin_enabled=True)
            for name in ("groq", "speechmatics", "deepgram")
        }
        plan = plan_route(
            self.settings,
            req,
            facts,
            policy=SttPolicy.from_settings(self.settings),
            route=("groq", "speechmatics"),
        )
        self.assertEqual(plan.route, ("groq", "speechmatics"))
        self.assertEqual(plan.execution, ("groq", "speechmatics"))

    def test_default_route_is_unchanged_without_a_store(self):
        req = SttRequirements.for_job(
            language="fa", file_bytes=1000, duration_seconds=10.0
        )
        plan = plan_route(self.settings, req, {})
        self.assertEqual(plan.route, ("speechmatics", "deepgram", "openai_compatible"))

    def test_free_plan_tier_narrows_policy_but_never_widens(self):
        permissive = make_settings(stt_free_only=False, stt_allow_paid_fallback=True)
        free_policy = _job_policy(permissive, "free")
        self.assertTrue(free_policy.free_only)
        paid_policy = _job_policy(permissive, "paid")
        self.assertFalse(paid_policy.free_only)
        self.assertTrue(paid_policy.allow_paid_fallback)
        # No tier loosens a deployment that is already free-only.
        strict = make_settings(stt_free_only=True, stt_allow_paid_fallback=False)
        self.assertTrue(_job_policy(strict, "paid").free_only)

    async def test_user_plan_tier_reflects_entitlements_and_special_users(self):
        self.assertEqual(await self.db.user_plan_tier(999), "free")
        user = await self.db.upsert_user(555, "vip")
        self.assertEqual(await self.db.user_plan_tier(int(user["id"])), "free")
        await self.db.set_user_unlimited(555, True, admin_id=1, reason="test grant")
        self.assertEqual(await self.db.user_plan_tier(int(user["id"])), "paid")
        await self.db.set_user_unlimited(555, False, admin_id=1, reason="test revoke")
        self.assertEqual(await self.db.user_plan_tier(int(user["id"])), "free")


if __name__ == "__main__":
    unittest.main()
