"""Admin AI-platform panel tests (callbacks + wizard through bot dispatch)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from gamas_bot.bot import StudyBot
from gamas_bot.database import Database
from gamas_bot.provider_credentials import ProviderCredentialManager

from support import make_settings


def _callback_event(telegram_id: int, data: str, *, answers=None):
    event = SimpleNamespace(
        is_private=True,
        data=data.encode("utf-8"),
        get_sender=AsyncMock(
            return_value=SimpleNamespace(id=telegram_id, username="admin", bot=False)
        ),
        answer=AsyncMock(),
        edit=AsyncMock(),
        message=SimpleNamespace(delete=AsyncMock()),
    )
    return event


def _message_event(telegram_id: int, text: str):
    return SimpleNamespace(
        is_private=True,
        raw_text=text,
        get_sender=AsyncMock(
            return_value=SimpleNamespace(id=telegram_id, username="admin", bot=False)
        ),
        reply=AsyncMock(),
        respond=AsyncMock(),
        message=SimpleNamespace(delete=AsyncMock(), media=None),
    )


class AIPanelCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "ai.sqlite3")
        await self.db.open()
        self.settings = make_settings(
            database_path=root / "ai.sqlite3",
            session_path=root / "session",
            temp_dir=root / "tmp",
            admin_ids=frozenset({7}),
            provider_credentials_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = self.db
        self.bot.credential_manager = ProviderCredentialManager(self.db, self.settings)
        self.bot._pending_admin_actions = {}
        self.bot._pending_credential_setup = {}
        from gamas_bot.admin_ai import AIPanels
        from gamas_bot.ai.models import ModelRegistry
        from gamas_bot.ai.routing import ProviderRouter
        from gamas_bot.ai.usage import AIUsageTracker
        from gamas_bot.provider_health import ProviderHealthChecker

        self.bot.ai_tracker = AIUsageTracker(self.db)
        self.bot.model_registry = ModelRegistry(self.db, self.settings)
        self.bot.provider_router = ProviderRouter(
            self.db, self.settings, self.bot.credential_manager,
            self.bot.ai_tracker, self.bot.model_registry,
        )
        self.bot.provider_health = ProviderHealthChecker(
            self.db, self.settings, self.bot.credential_manager
        )
        self.bot.ai_panels = AIPanels(self.bot)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    def _mock_bot_rendering(self):
        self.bot._edit_callback = AsyncMock()

    async def _callback(self, data: str):
        event = _callback_event(7, data)
        await self.bot._handle_callback(event)
        return event

    @property
    def last_text(self) -> str:
        call = self.bot._edit_callback.call_args
        return str(call.args[1] if call and len(call.args) > 1 else "")

    async def test_home_panel_shows_free_only_state(self):
        self._mock_bot_rendering()
        event = await self._callback("admin:ai")
        self.assertIn("FREE_ONLY", self.last_text)
        buttons = self.bot._edit_callback.call_args.args[2]
        flat = [b for row in buttons for b in row]
        names = [str(b) for b in flat]
        self.assertTrue(any("مسیرها" in n for n in names))

    async def test_provider_detail_shows_classification_and_policy(self):
        self._mock_bot_rendering()
        await self._callback("admin:ai:pv:groq")
        self.assertIn("Groq", self.last_text)
        self.assertIn("docs", self.last_text)

    async def test_unknown_provider_callback_is_rejected(self):
        event = await self._callback("admin:ai:pv:not-a-slug")
        event.answer.assert_awaited()

    async def test_provider_enable_toggle_persists_and_audits(self):
        self._mock_bot_rendering()
        await self._callback("admin:ai:ptgl:groq")
        row = await self.db.ai_provider_settings_get("groq")
        self.assertEqual(row["enabled"], 0)
        await self._callback("admin:ai:ptgl:groq")
        row = await self.db.ai_provider_settings_get("groq")
        self.assertEqual(row["enabled"], 1)

    async def test_route_reorder_and_disable_are_applied(self):
        self._mock_bot_rendering()
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [
                {"provider": "groq", "enabled": 1, "free_only": 1},
                {"provider": "nara", "enabled": 1, "free_only": 1},
            ],
            admin_id=7,
        )
        self.bot.provider_router.invalidate_cache()
        await self._callback("admin:ai:rtmv:0:d")
        rows = await self.db.ai_routes_list("notes", "chunk_structuring")
        self.assertEqual([r["provider"] for r in rows], ["nara", "groq"])
        await self._callback("admin:ai:rten:0")
        rows = await self.db.ai_routes_list("notes", "chunk_structuring")
        self.assertEqual(rows[0]["enabled"], 0)

    async def test_route_add_appends_free_flagged_provider(self):
        self._mock_bot_rendering()
        await self._callback("admin:ai:rtadd:mistral")
        rows = await self.db.ai_routes_list("notes", "chunk_structuring")
        self.assertEqual(rows[-1]["provider"], "mistral")
        self.assertEqual(rows[-1]["free_only"], 1)

    async def test_key_wizard_secret_flow_saves_credential(self):
        self._mock_bot_rendering()
        await self._callback("admin:ai:kadd:groq")
        # Pick the first static model (or "custom" when no static list).
        await self._callback("admin:ai:kmdl:groq:0")
        self.assertEqual(self.bot._pending_admin_actions[7], "aikey_label")
        event = _message_event(7, "کلید اصلی")
        await self.bot._handle_pending_admin_input(event, 7, "aikey_label", "کلید اصلی")
        self.assertEqual(self.bot._pending_admin_actions[7], "credential_secret")
        # Now deliver the secret through the message path (message gets deleted).
        self.bot.provider_health.invalidate()
        secret_event = _message_event(7, "gsk-wizard-test-9999")
        original = self.bot.credential_manager
        await self.bot._handle_pending_admin_input(
            secret_event, 7, "credential_secret", "gsk-wizard-test-9999"
        )
        secret_event.message.delete.assert_awaited()
        summaries = await original.list_summaries()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["provider"], "groq")
        self.assertEqual(summaries[0]["secret_last4"], "9999")

    async def test_usage_panel_and_reset(self):
        self._mock_bot_rendering()
        for _ in range(3):
            await self.db.ai_usage_insert(
                {"provider": "groq", "model": "m", "request_type": "chunk_structuring",
                 "result": "success"}
            )
        await self._callback("admin:ai:us")
        self.assertIn("groq", self.last_text)
        await self._callback("admin:ai:usrst:groq")
        count = await self.db.ai_usage_today_count("groq")
        self.assertEqual(count, 0)

    async def test_models_panel_marks_deprecated(self):
        self._mock_bot_rendering()
        await self.bot.model_registry.apply_discovery(
            "groq",
            [
                __import__("gamas_bot.ai.models", fromlist=["ModelInfo"]).ModelInfo(
                    provider="groq", model_id="openai/gpt-oss-120b", source="live"
                )
            ],
        )
        await self._callback("admin:ai:md:groq")
        models = await self.db.ai_models_list("groq")
        self.assertTrue(models)
        await self._callback(f"admin:ai:mdep:{models[0]['id']}")
        models = await self.db.ai_models_list("groq")
        self.assertEqual(models[0]["deprecated"], 1)

    async def test_dry_run_panel_never_shows_secret_or_prompt(self):
        self._mock_bot_rendering()
        await self._callback("admin:ai:dry:gemini")
        text = self.last_text
        self.assertIn("Dry-run", text)
        self.assertNotIn("این یک متن آزمایشی", text)  # content replaced by metadata
        self.assertNotIn("AIza", text)

    async def test_invalid_ai_callback_falls_through_safely(self):
        event = await self._callback("admin:ai:bogus:x")
        event.answer.assert_awaited()


if __name__ == "__main__":
    unittest.main()
