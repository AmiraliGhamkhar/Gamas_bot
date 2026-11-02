"""Verification tests for the SOLID refactoring seams across ``gamas_bot``."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

import aiohttp

from gamas_bot import intake, llm, stt
from gamas_bot.bot import GamasBot
from gamas_bot.bot_deps import BotDependencies
from gamas_bot.capacity import CooldownRegistry, LoopCooldownRegistry
from gamas_bot.config import NoteRoute
from gamas_bot.conversation_state import ConversationStateStore
from gamas_bot.db import Database
from gamas_bot.media import MediaInfo
from gamas_bot.llm_profiles import (
    PROVIDER_PROFILES,
    ProviderCapabilityProfile,
    get_provider_profile,
    register_provider_profile,
)
from gamas_bot.protocols import (
    BillingStoreProtocol,
    DocumentStoreProtocol,
    LLMProviderDialect,
    LLMProviderProfileProtocol,
    LearningStoreProtocol,
    LibraryStoreProtocol,
    PaymentStoreProtocol,
    ReportingStoreProtocol,
    STTProviderAdapter,
    SupportStoreProtocol,
    UserStoreProtocol,
)
from gamas_bot.routing import CallbackRouter
from gamas_bot.transcript import RawTranscript
from tests.support import FakeTelegramClient, make_settings


class CooldownRegistryTests(unittest.TestCase):
    def test_cooldown_registry_tracks_and_clears_expiry(self) -> None:
        now = 100.0
        reg = CooldownRegistry(clock=lambda: now)
        self.assertEqual(reg.remaining("route-a"), 0.0)
        reg.set("route-a", 30.0)
        self.assertAlmostEqual(reg.remaining("route-a"), 30.0)
        now = 131.0
        self.assertEqual(reg.remaining("route-a"), 0.0)

    def test_loop_cooldown_registry_isolates_distinct_sessions_and_loops(self) -> None:
        reg = LoopCooldownRegistry()
        s1 = object()
        s2 = object()

        async def check_within_loop() -> tuple[float, float]:
            reg.set(s1, "note:0", 60.0)
            return reg.remaining(s1, "note:0"), reg.remaining(s2, "note:0")

        async def check_other_loop() -> float:
            return reg.remaining(s1, "note:0")

        rem_s1, rem_s2 = asyncio.run(check_within_loop())
        self.assertGreater(rem_s1, 0.0)
        self.assertEqual(rem_s2, 0.0)
        self.assertEqual(asyncio.run(check_other_loop()), 0.0)


class SegregatedRepositoryProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_exposes_isp_facets_conforming_to_protocols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "isp.db")
            await db.connect()
            try:
                self.assertIsInstance(db.users, UserStoreProtocol)
                self.assertIsInstance(db.library, LibraryStoreProtocol)
                self.assertIsInstance(db.documents, DocumentStoreProtocol)
                self.assertIsInstance(db.billing, BillingStoreProtocol)
                self.assertIsInstance(db.learning, LearningStoreProtocol)
                self.assertIsInstance(db.payments, PaymentStoreProtocol)
                self.assertIsInstance(db.support, SupportStoreProtocol)
                self.assertIsInstance(db.reporting, ReportingStoreProtocol)

                user = await db.users.ensure_user(42, first_name="Ali")
                self.assertEqual(user.telegram_id, 42)
                self.assertEqual(await db.billing.available_seconds(42), 0)
            finally:
                await db.close()


class OpenClosedRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_stt_provider_spec_registers_without_modifying_transcribe(self) -> None:
        async def _custom_run(
            _session: aiohttp.ClientSession, audio_path: Path, _settings
        ) -> RawTranscript:
            return RawTranscript.from_text(
                f"transcribed:{audio_path.name}",
                engine="custom_stt",
            )

        spec = stt.ProviderSpec(
            slug="custom_stt",
            label="Custom STT",
            configured=lambda _s: True,
            max_upload=10 * 1024 * 1024,
            run=_custom_run,
            model_getter=lambda _s: "custom-model-v1",
            base_url_getter=lambda _s: "https://stt.example.test/v1",
        )
        orig = dict(stt.PROVIDERS)
        try:
            stt.register_stt_provider(spec)
            self.assertIsInstance(spec, STTProviderAdapter)
            self.assertIn("custom_stt", stt.PROVIDERS)
            with tempfile.TemporaryDirectory() as tmp:
                audio = Path(tmp) / "lecture.ogg"
                audio.write_bytes(b"OggS-fake-audio")
                settings = make_settings(stt_providers=("custom_stt",))
                self.assertEqual(
                    stt.first_configured_stt_url(settings),
                    "https://stt.example.test/v1",
                )
                async with aiohttp.ClientSession() as session:
                    result = await stt.transcribe(audio, settings, session=session)
                self.assertEqual(result.engine, "custom_stt")
                self.assertEqual(result.text, "transcribed:lecture.ogg")
        finally:
            stt.PROVIDERS.clear()
            stt.PROVIDERS.update(orig)

    def test_custom_llm_dialect_registers_without_modifying_request_builders(self) -> None:
        class _CustomDialect(llm.OpenAIDialect):
            style = "custom_wire"

            def build_post_request(self, route, settings, system, user, *, schema=None):
                url, headers, body = super().build_post_request(
                    route, settings, system, user, schema=schema
                )
                headers["X-Custom-Wire"] = "enabled"
                return url, headers, body

        orig = dict(llm.DIALECTS)
        try:
            dialect = _CustomDialect()
            self.assertIsInstance(dialect, LLMProviderDialect)
            llm.register_llm_dialect(dialect)
            route = NoteRoute(
                slug="custom",
                label="Custom",
                style="custom_wire",
                base_url="https://llm.example.test/v1",
                model="custom-v1",
                api_key="secret-k",
                json_object_mode=True,
            )
            url, headers, body = llm._build_request_for(
                route, make_settings(), "sys prompt", "user prompt"
            )
            self.assertTrue(url.endswith("/chat/completions"))
            self.assertEqual(headers.get("X-Custom-Wire"), "enabled")
            self.assertEqual(body["model"], "custom-v1")
        finally:
            llm.DIALECTS.clear()
            llm.DIALECTS.update(orig)

    def test_custom_failure_rule_registers_with_precedence(self) -> None:
        class CustomDomainError(RuntimeError):
            pass

        orig = list(intake.FAILURE_CLASSIFICATION_RULES)
        try:
            intake.register_failure_rule(
                (CustomDomainError,),
                "custom_domain_kind",
            )
            self.assertEqual(
                intake.classify_failure("transcribing", CustomDomainError("x")),
                "custom_domain_kind",
            )
        finally:
            intake.FAILURE_CLASSIFICATION_RULES[:] = orig


class CallbackRouterAndStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_router_prefers_exact_then_longest_prefix(self) -> None:
        router = CallbackRouter()
        calls: list[str] = []

        async def _rec(tag: str, data: str) -> None:
            calls.append(f"{tag}:{data}")

        router.register_prefix("lib:", lambda _e, _u, d: _rec("short", d))
        router.register_prefix("lib:docs", lambda _e, _u, d: _rec("long", d))
        router.register_exact("lib:docs", lambda _e, _u, d: _rec("exact", d))

        self.assertTrue(await router.dispatch(None, 1, "lib:docs"))
        self.assertTrue(await router.dispatch(None, 1, "lib:docs:2"))
        self.assertTrue(await router.dispatch(None, 1, "lib:3"))
        self.assertFalse(await router.dispatch(None, 1, "unknown:1"))
        self.assertEqual(calls, ["exact:lib:docs", "long:lib:docs:2", "short:lib:3"])

    async def test_conversation_state_store_disarm_boundaries(self) -> None:
        store = ConversationStateStore()
        store.pending_payments[7] = 99
        store.support_states[7] = ("new", "technical")
        store.pending_flashcard[7] = (3, "front")
        store.pending_broadcast.add(7)

        store.on_callback_navigation(7, "library")
        self.assertNotIn(7, store.pending_payments)
        self.assertNotIn(7, store.support_states)
        self.assertNotIn(7, store.pending_flashcard)
        self.assertIn(7, store.pending_broadcast)

        store.on_callback_navigation(7, "adm:home")
        self.assertNotIn(7, store.pending_broadcast)

        store.support_admin_states[7] = 12
        store.pending_flashcard[7] = (4, "back")
        store.disarm_admin_command_prompts(7)
        self.assertNotIn(7, store.support_admin_states)
        self.assertNotIn(7, store.pending_flashcard)


class BotDependencyInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_gamas_bot_accepts_injected_collaborators_without_monkeypatching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "clip.mp3"
            audio.write_bytes(b"ID3-fake")
            probed_paths: list[Path] = []

            async def fake_probe(path: Path, _settings) -> MediaInfo:
                probed_paths.append(path)
                return MediaInfo(has_audio=True, duration=15.0, audio_codec="mp3")

            deps = BotDependencies(
                probe_media_fn=fake_probe,
                needs_transcode_fn=lambda _name, _info: False,
            )
            settings = make_settings(
                session_path=root / "gamas",
                db_path=root / "bot.db",
                temp_dir=root / "tmp",
                receipt_dir=root / "receipts",
            )
            db = Database(settings.db_path)
            client = FakeTelegramClient()
            bot = GamasBot(settings, db=db, client=client, deps=deps)

            result = await bot._prepare_audio(audio, root, "clip.mp3", "audio")
            self.assertEqual(result, audio)
            self.assertEqual(probed_paths, [audio])
            self.assertIs(bot.db, db)
            self.assertIs(bot.client, client)
            self.assertIs(bot.deps, deps)
            _ = asyncio.sleep


class ProviderProfileProtocolTests(unittest.TestCase):
    def test_all_builtin_profiles_satisfy_llm_provider_profile_protocol(self) -> None:
        for slug, profile in PROVIDER_PROFILES.items():
            with self.subTest(slug=slug):
                self.assertIsInstance(profile, LLMProviderProfileProtocol)

    def test_register_provider_profile_extends_registry_without_modifying_dialects(self) -> None:
        custom = ProviderCapabilityProfile(
            slug="custom_test_gateway",
            max_output_tokens_cap=4096,
            documented_stream_options=False,
        )
        try:
            register_provider_profile(custom)
            self.assertIs(get_provider_profile("custom_test_gateway"), custom)
            route = NoteRoute(
                slug="custom_test_gateway",
                label="Custom",
                base_url="https://custom.example/v1",
                api_key="k",
                model="custom-model",
                style="openai",
                json_object_mode=True,
            )
            settings = make_settings(note_max_output_tokens=16384, note_ttfb_timeout=0.0)
            _url, _headers, payload = llm._build_request_for(route, settings, "S", "U")
            self.assertEqual(payload["max_tokens"], 4096)
        finally:
            PROVIDER_PROFILES.pop("custom_test_gateway", None)


if __name__ == "__main__":
    unittest.main()
