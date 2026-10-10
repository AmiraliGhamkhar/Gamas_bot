"""Gamas Speech Platform admin panel tests (spec §36/§39/§60-§64/§73)."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from gamas_bot.admin_stt import SttPanels, handle_stt_callback
from gamas_bot.bot import StudyBot
from gamas_bot.database import Database
from gamas_bot.provider_credentials import ProviderCredentialManager
from gamas_bot.provider_health import ProviderHealthChecker
from gamas_bot.stt_platform.registry import STT_PROVIDER_REGISTRY

from support import make_settings

#: Anything that looks like a secret must never appear in panel output.
_SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9]{4,}|Bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)", re.I)


def _button_data(button) -> bytes:
    """Callback payload of a Telethon inline button (any 1.x layout)."""
    data = getattr(button, "data", None)
    if data:
        return bytes(data)
    inner = getattr(button, "type", None)
    data = getattr(inner, "data", None)
    return bytes(data) if data else b""


def _callback_event(telegram_id: int, data: str):
    return SimpleNamespace(
        is_private=True,
        data=data.encode("utf-8"),
        get_sender=AsyncMock(
            return_value=SimpleNamespace(id=telegram_id, username="admin", bot=False)
        ),
        answer=AsyncMock(),
        edit=AsyncMock(),
        reply=AsyncMock(),
        respond=AsyncMock(),
        message=SimpleNamespace(delete=AsyncMock(), media=None),
    )


class SttPanelCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "stt.sqlite3")
        await self.db.open()
        self.settings = make_settings(
            database_path=root / "stt.sqlite3",
            session_path=root / "session",
            temp_dir=root / "tmp",
            admin_ids=frozenset({7}),
            provider_credentials_encryption_key=Fernet.generate_key().decode("ascii"),
            stt_provider_api_keys=(("groq", "sk-super-secret-groq-key-1234"),),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = self.db
        self.bot.credential_manager = ProviderCredentialManager(self.db, self.settings)
        self.bot.provider_health = ProviderHealthChecker(
            self.db, self.settings, self.bot.credential_manager
        )
        self.bot._pending_admin_actions = {}
        self.bot._pending_credential_setup = {}
        self.bot.stt_panels = SttPanels(self.bot)
        self.bot._edit_callback = AsyncMock()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _callback(self, data: str):
        event = _callback_event(7, data)
        await handle_stt_callback(self.bot, event, data)
        return event

    @property
    def last_text(self) -> str:
        call = self.bot._edit_callback.call_args
        return str(call.args[1] if call and len(call.args) > 1 else "")

    def assert_no_secrets(self):
        self.assertIsNone(_SECRET_PATTERN.search(self.last_text), self.last_text)

    # ------------------------------------------------------------- home
    async def test_home_lists_every_panel_section(self):
        await self._callback("admin:stt")
        for marker in ("پلتفرم STT", "مسیر فعال", "زبان"):
            self.assertIn(marker, self.last_text)
        self.assert_no_secrets()

    async def test_policy_screen_reads_settings(self):
        await self._callback("admin:stt:pol")
        self.assertIn("STT_FREE_ONLY = روشن", self.last_text)
        self.assertIn("STT_QUOTA_SAFETY_MARGIN", self.last_text)

    # --------------------------------------------------------- providers
    async def test_provider_detail_shows_registry_metadata_and_privacy(self):
        await self._callback("admin:stt:pv:gemini_transcribe")
        self.assertIn("gemini-3.5-transcribe", self.last_text)
        self.assertIn("data_training_policy", self.last_text)
        self.assertIn("medical_compliance: none_claimed", self.last_text)
        # Spec §53: never claim HIPAA/GDPR/no-training without documentation.
        self.assertNotIn("HIPAA-safe", self.last_text)
        self.assertNotIn("GDPR-safe", self.last_text)
        self.assert_no_secrets()

    async def test_provider_enable_toggle_persists_and_audits(self):
        await self._callback("admin:stt:pen:groq")
        state = await self.db.stt_provider_settings_get("groq")
        self.assertEqual(state["enabled"], 0)
        await self._callback("admin:stt:pen:groq")
        state = await self.db.stt_provider_settings_get("groq")
        self.assertEqual(state["enabled"], 1)

    async def test_billing_attestation_is_recorded(self):
        await self._callback("admin:stt:pbl:groq:paid")
        state = await self.db.stt_provider_settings_get("groq")
        self.assertEqual(state["billing_state"], "paid")

    # ------------------------------------------------------------ models
    async def test_model_panel_marks_deprecated_without_deleting(self):
        await self._callback("admin:stt:mdep:groq:whisper-large-v3-turbo")
        rows = await self.db.stt_models_list("groq")
        deprecated = [row for row in rows if row["model"] == "whisper-large-v3-turbo"]
        self.assertTrue(deprecated)
        self.assertTrue(deprecated[0]["deprecated"])
        self.assertEqual(deprecated[0]["status"], "deprecated")
        self.assertTrue(any(row["model"] == "whisper-large-v3-turbo" for row in rows))

    # ------------------------------------------------------------ routes
    async def test_route_persist_move_toggle_and_delete(self):
        await self._callback("admin:stt:rsave")
        rows = await self.db.stt_routes_list()
        self.assertEqual(
            [row["provider"] for row in rows],
            ["speechmatics", "deepgram", "openai_compatible"],
        )
        await self._callback("admin:stt:rmv:deepgram:u")
        rows = await self.db.stt_routes_list()
        self.assertEqual([row["provider"] for row in rows][:2], ["deepgram", "speechmatics"])
        await self._callback("admin:stt:ren:deepgram")
        rows = await self.db.stt_routes_list()
        self.assertEqual(rows[0]["enabled"], 0)
        await self._callback("admin:stt:rdel:deepgram")
        rows = await self.db.stt_routes_list()
        self.assertNotIn("deepgram", [row["provider"] for row in rows])

    async def test_route_add_and_model_override(self):
        await self._callback("admin:stt:rsave")
        await self._callback("admin:stt:radd:groq")
        rows = await self.db.stt_routes_list()
        self.assertIn("groq", [row["provider"] for row in rows])
        await self._callback("admin:stt:rset:groq:whisper-large-v3-turbo")
        rows = await self.db.stt_routes_list()
        groq_row = next(row for row in rows if row["provider"] == "groq")
        self.assertEqual(groq_row["model_override"], "whisper-large-v3-turbo")
        await self._callback("admin:stt:rset:groq:-")
        rows = await self.db.stt_routes_list()
        groq_row = next(row for row in rows if row["provider"] == "groq")
        self.assertIsNone(groq_row["model_override"])

    async def test_route_summary_shows_free_only_policy(self):
        await self._callback("admin:stt:rt")
        self.assertIn("Free-only: روشن", self.last_text)
        self.assertIn("Paid fallback: خاموش", self.last_text)

    # ------------------------------------------------------------ quotas
    async def test_quota_panel_shows_unknown_without_manufacturing_values(self):
        await self._callback("admin:stt:qt")
        self.assertIn("Unknown", self.last_text)
        self.assert_no_secrets()

    async def test_budget_input_round_trip_and_validation(self):
        self.bot._pending_admin_actions[7] = "stt_budget:groq"
        event = _callback_event(7, "")
        await self.bot.stt_panels.handle_budget_input(
            event, "groq", "provider | audio_seconds_day | 28800 | 22400 | —"
        )
        budget = await self.db.stt_quota_budget_get("groq", "provider", "audio_seconds_day")
        self.assertEqual(budget["quota_limit"], 28800)
        self.assertEqual(budget["remaining"], 22400)
        self.assertNotIn(7, self.bot._pending_admin_actions)

        event = _callback_event(7, "")
        await self.bot.stt_panels.handle_budget_input(event, "groq", "garbage")
        event.reply.assert_awaited()
        # Invalid input keeps the pending action so the admin can retry.
        self.bot._pending_admin_actions[7] = "stt_budget:groq"

    async def test_budget_delete_removes_ceiling(self):
        await self.db.stt_quota_set_budget(
            "groq", "provider", "rpd", limit=2000, remaining=1817, reset_at=None, admin_id=7
        )
        await self._callback("admin:stt:qdel:groq:provider:rpd")
        self.assertIsNone(await self.db.stt_quota_budget_get("groq", "provider", "rpd"))

    # ------------------------------------------------------------- tests
    async def test_metadata_test_never_sends_audio_and_never_shows_keys(self):
        # The health probe is mocked: this test asserts panel behaviour, and a
        # metadata test must never reach the network from CI.
        self.bot.provider_health.check = AsyncMock(
            return_value=[
                SimpleNamespace(
                    status_fa="کلید تنظیم نشده",
                    status="not_configured",
                    http_status=None,
                    latency_ms=None,
                    detail=None,
                    check_type="read_only",
                )
            ]
        )
        await self._callback("admin:stt:trun:groq:meta")
        self.assertIn("PROVIDER: groq", self.last_text)
        self.assertIn("CHECK:", self.last_text)
        self.assert_no_secrets()

    async def test_sample_test_without_fixture_sends_nothing(self):
        await self._callback("admin:stt:trun:groq:fa")
        self.assertIn("نصب نشده", self.last_text)
        self.assertIn("هیچ صدایی ارسال نشد", self.last_text)
        self.assert_no_secrets()

    # ------------------------------------------------------------ dry-run
    async def test_dry_run_redacts_headers_and_keeps_request_shape(self):
        await self._callback("admin:stt:dry:groq")
        self.assertIn("<redacted>", self.last_text)
        self.assertIn("api.groq.com", self.last_text)
        self.assertIn("whisper-large-v3", self.last_text)
        self.assertNotIn("sk-super-secret-groq-key-1234", self.last_text)
        self.assert_no_secrets()

    async def test_dry_run_speechmatics_uses_legacy_request_shape(self):
        await self._callback("admin:stt:dry:speechmatics")
        self.assertIn("/jobs", self.last_text)
        self.assertIn("transcription_config", self.last_text)
        self.assertNotIn("sm-key", self.last_text)

    # --------------------------------------------------------------- logs
    async def test_logs_render_events_without_content(self):
        await self.db.stt_provider_event_insert(
            {
                "event": "stt_request_failed",
                "provider": "groq",
                "model": "whisper-large-v3",
                "http_status": 429,
                "error_category": "rate_limited",
                "level": "warning",
            }
        )
        await self._callback("admin:stt:lg")
        self.assertIn("stt_request_failed", self.last_text)
        self.assertIn("rate_limited", self.last_text)
        self.assert_no_secrets()

    # ---------------------------------------------------------- benchmarks
    async def test_benchmarks_panel_lists_all_profiles(self):
        await self._callback("admin:stt:bm")
        self.assertIn("medical_lecture", self.last_text)
        self.assertIn("numbers_and_units", self.last_text)

    # --------------------------------------------- wizard covers all STT
    async def test_key_wizard_offers_every_registry_provider(self):
        from gamas_bot.admin_ai import AIPanels

        self.bot.ai_panels = AIPanels(self.bot)
        event = _callback_event(7, "admin:ai:wiz:srv:stt")
        await self.bot.ai_panels.begin_wizard(event, service="stt")
        call = self.bot._edit_callback.call_args
        buttons = call.args[2]
        payloads = b"".join(_button_data(b) for row in buttons for b in row)
        for slug in ("gemini_transcribe", "groq", "assemblyai", "gladia", "ibm_watson_stt"):
            self.assertIn(f"admin:ai:wiz:pv:stt:{slug}".encode("ascii"), payloads)
        self.assertEqual(len(STT_PROVIDER_REGISTRY), 13)

    async def test_key_wizard_metadata_card_shows_limits_and_languages(self):
        from gamas_bot.admin_ai import AIPanels

        self.bot.ai_panels = AIPanels(self.bot)
        event = _callback_event(7, "admin:ai:wiz:pv:stt:groq")
        await self.bot.ai_panels.begin_wizard(event, service="stt", slug="groq")
        text = str(self.bot._edit_callback.call_args.args[1])
        self.assertIn("whisper-large-v3", text)
        self.assertIn("25 MB", text)
        self.assertIn("batch=بله", text)
        self.assertNotIn("sk-super-secret-groq-key-1234", text)


if __name__ == "__main__":
    unittest.main()
