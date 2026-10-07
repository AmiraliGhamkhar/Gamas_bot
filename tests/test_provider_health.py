"""Provider health panel: cheap probes, rotation state, masking and ordering."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from gamas_bot.bot import StudyBot, admin_menu, main_menu
from gamas_bot.database import Database
from gamas_bot.provider_credentials import ProviderCredentialManager
from gamas_bot.provider_health import (
    DEFAULT_CACHE_SECONDS,
    HEALTH_STATUSES,
    STATUS_LABELS_FA,
    ProviderHealthChecker,
    build_probe,
    classify_status,
    cooldown_remaining_seconds,
    parse_retry_after,
    sanitize_detail,
)
from support import make_settings


def _response(status: int, *, retry_after: str | None = None, text: str = "{}"):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return SimpleNamespace(status=status, headers=headers, text=text)


class HealthVocabularyTests(unittest.TestCase):
    def test_status_mapping_matches_the_documented_vocabulary(self):
        self.assertEqual(classify_status(200), "healthy")
        self.assertEqual(classify_status(204), "healthy")
        self.assertEqual(classify_status(401), "authentication_failed")
        self.assertEqual(classify_status(403), "authentication_failed")
        self.assertEqual(classify_status(429), "rate_limited")
        # A probe the deployment does not offer is not a broken credential.
        for status in (400, 404, 405, 422):
            self.assertEqual(classify_status(status), "configured", status)
        self.assertEqual(classify_status(503), "degraded")
        for status in {"disabled", "not_configured"}:
            self.assertIn(status, HEALTH_STATUSES)
            self.assertIn(status, STATUS_LABELS_FA)

    def test_retry_after_accepts_seconds_dates_and_garbage(self):
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertIsNone(parse_retry_after("not-a-date"))
        self.assertIsNone(parse_retry_after(None))
        self.assertEqual(parse_retry_after("-5"), 0.0)
        self.assertEqual(parse_retry_after("999999999"), 604_800.0)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        future = (now + timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertAlmostEqual(parse_retry_after(future, now=now), 90.0, places=0)

    def test_details_are_sanitized_and_bounded(self):
        cleaned = sanitize_detail("bad key sk-abcdefghijklmnop rejected", ("sk-abcdefghijklmnop",))
        self.assertNotIn("sk-abcdefghijklmnop", cleaned)
        self.assertNotIn("abcdefghijkl", cleaned)
        self.assertLessEqual(len(sanitize_detail("x" * 5000)), 240)
        self.assertIsNone(sanitize_detail("   "))

    def test_every_provider_probe_is_a_cheap_read_only_list(self):
        settings = make_settings()
        from gamas_bot.provider_credentials import ProviderCredential

        def credential(service, provider, secret="k" * 12, base=None):
            return ProviderCredential(
                id=1, service=service, provider=provider, label="l", secret=secret, base_url=base
            )

        speechmatics = build_probe("stt", "speechmatics", credential("stt", "speechmatics"), settings)
        self.assertEqual(speechmatics.method, "GET")
        self.assertTrue(speechmatics.url.endswith("/jobs?limit=1"))
        self.assertEqual(speechmatics.headers["Authorization"], "Bearer " + "k" * 12)

        deepgram = build_probe("stt", "deepgram", credential("stt", "deepgram"), settings)
        self.assertEqual(deepgram.url, "https://api.deepgram.com/v1/projects")
        self.assertTrue(deepgram.headers["Authorization"].startswith("Token "))

        gemini = build_probe("notes", "gemini", credential("notes", "gemini"), settings)
        self.assertTrue(gemini.url.endswith("/models"))
        self.assertEqual(gemini.headers["x-goog-api-key"], "k" * 12)
        self.assertNotIn("k" * 12, gemini.url)

        anthropic = build_probe("notes", "anthropic", credential("notes", "anthropic"), settings)
        self.assertTrue(anthropic.url.endswith("/models"))
        self.assertEqual(anthropic.headers["anthropic-version"], "2023-06-01")

        stt_endpoint_settings = make_settings(stt_openai_base_url="https://stt.example.test/v1")
        openai_stt = build_probe(
            "stt", "openai_compatible", credential("stt", "openai_compatible"), stt_endpoint_settings
        )
        self.assertEqual(openai_stt.url, "https://stt.example.test/v1/models")

        # An endpoint without a base URL cannot be probed at all: the panel says
        # so explicitly instead of guessing.
        no_base = build_probe(
            "stt",
            "openai_compatible",
            credential("stt", "openai_compatible", base=None),
            make_settings(stt_openai_base_url=None, note_api_base_url=None, note_api_provider="disabled"),
        )
        self.assertFalse(no_base.supported)
        self.assertTrue(no_base.unsupported_reason)

    def test_secret_never_appears_in_a_probe_repr(self):
        from gamas_bot.provider_credentials import ProviderCredential

        probe = build_probe(
            "notes",
            "gemini",
            ProviderCredential(
                id=1, service="notes", provider="gemini", label="l", secret="super-secret-key-1234"
            ),
            make_settings(),
        )
        self.assertNotIn("super-secret-key-1234", repr(probe))

    def test_cooldown_remaining_is_bounded_and_safe(self):
        self.assertEqual(cooldown_remaining_seconds(None), 0)
        self.assertEqual(cooldown_remaining_seconds("garbage"), 0)
        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        self.assertGreater(cooldown_remaining_seconds(future), 55)
        past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        self.assertEqual(cooldown_remaining_seconds(past), 0)


class ProviderHealthCheckerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "health.sqlite3")
        await self.db.open()
        self.master_key = Fernet.generate_key().decode("ascii")
        self.settings = make_settings(
            database_path=self.root / "health.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            provider_credentials_encryption_key=self.master_key,
            speechmatics_api_key=None,
            deepgram_api_key=None,
            gemini_api_key=None,
            note_api_key=None,
            stt_primary="deepgram",
            stt_fallback_enabled=False,
        )
        self.credentials = ProviderCredentialManager(self.db, self.settings)
        self.checker = ProviderHealthChecker(self.db, self.settings, self.credentials)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _add(self, label: str, secret: str, priority: int = 100, provider="deepgram") -> int:
        return await self.credentials.add_credential(
            service="stt",
            provider=provider,
            label=label,
            secret=secret,
            admin_id=700,
            priority=priority,
        )

    def _probe_with(self, status, *, retry_after=None, text="{}"):
        """Patch only the network call; keep all classification logic real."""
        calls: list[dict] = []

        async def fake_fetch(method, url, headers, timeout):
            calls.append({"method": method, "url": url, "headers": headers, "timeout": timeout})
            return _response(status, retry_after=retry_after, text=text)

        return patch("gamas_bot.provider_health._fetch", fake_fetch), calls

    async def test_multiple_keys_are_checked_individually_and_masked(self):
        first = await self._add("primary", "dg-secret-primary-1234", priority=1)
        second = await self._add("backup", "dg-secret-backup-5678", priority=2)
        patcher, calls = self._probe_with(200)
        with patcher:
            results = await self.checker.check("stt", "deepgram")
        self.assertEqual([row.credential_id for row in results], [first, second])
        self.assertTrue(all(row.status == "healthy" for row in results))
        self.assertEqual([row.masked for row in results], ["••••••••1234", "••••••••5678"])
        self.assertEqual(len(calls), 2)
        for row in results:
            payload = f"{row!r}{row.detail}{row.masked}"
            self.assertNotIn("dg-secret", payload)
        summaries = {row["id"]: row for row in await self.credentials.list_summaries()}
        self.assertIsNotNone(summaries[first]["last_success_at"])
        self.assertIsNone(summaries[first]["cooldown_until"])

    async def test_429_sets_a_cooldown_from_retry_after(self):
        first = await self._add("primary", "dg-secret-primary-1234", priority=1)
        second = await self._add("backup", "dg-secret-backup-5678", priority=2)
        patcher, _calls = self._probe_with(429, retry_after="120")
        with patcher:
            results = await self.checker.check("stt", "deepgram")
        self.assertEqual({row.status for row in results}, {"rate_limited"})
        summaries = {row["id"]: row for row in await self.credentials.list_summaries()}
        until = datetime.fromisoformat(summaries[first]["cooldown_until"])
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(remaining, 115)
        self.assertLessEqual(remaining, 121)
        self.assertEqual(summaries[first]["last_status_code"], 429)
        self.assertIsNotNone(summaries[second]["cooldown_until"])
        # A cooling-down key is excluded from rotation until the cooldown ends.
        self.assertEqual(await self.credentials.candidates("stt", "deepgram"), [])

    async def test_authentication_failure_quarantines_the_key(self):
        first = await self._add("primary", "dg-secret-primary-1234", priority=1)
        patcher, _calls = self._probe_with(401)
        with patcher:
            results = await self.checker.check("stt", "deepgram")
        self.assertEqual(results[0].status, "authentication_failed")
        self.assertEqual(results[0].http_status, 401)
        summaries = {row["id"]: row for row in await self.credentials.list_summaries()}
        self.assertIsNotNone(summaries[first]["quarantined_at"])
        self.assertFalse(await self.credentials.has_available("stt", "deepgram"))

    async def test_success_after_failure_restores_healthy_state(self):
        first = await self._add("primary", "dg-secret-primary-1234", priority=1)
        with self._probe_with(429, retry_after="30")[0]:
            await self.checker.check("stt", "deepgram")
        summaries = {row["id"]: row for row in await self.credentials.list_summaries()}
        self.assertIsNotNone(summaries[first]["cooldown_until"])
        self.checker.invalidate()
        with self._probe_with(200)[0]:
            results = await self.checker.check("stt", "deepgram", force=True)
        self.assertEqual(results[0].status, "healthy")
        summaries = {row["id"]: row for row in await self.credentials.list_summaries()}
        self.assertIsNone(summaries[first]["cooldown_until"])
        self.assertIsNone(summaries[first]["quarantined_at"])
        self.assertIsNotNone(summaries[first]["last_success_at"])

    async def test_results_are_cached_and_force_refreshes(self):
        await self._add("primary", "dg-secret-primary-1234", priority=1)
        patcher, calls = self._probe_with(200)
        with patcher:
            first = await self.checker.check("stt", "deepgram")
            second = await self.checker.check("stt", "deepgram")
            third = await self.checker.check("stt", "deepgram", force=True)
        self.assertEqual(len(calls), 2)
        self.assertFalse(first[0].cached)
        self.assertTrue(second[0].cached)
        self.assertFalse(third[0].cached)
        self.assertEqual(len(self.checker.cached()), 1)
        self.assertGreater(DEFAULT_CACHE_SECONDS, 0)

    async def test_disabled_and_unconfigured_providers_are_reported_not_probed(self):
        credential_id = await self._add("primary", "dg-secret-primary-1234")
        await self.credentials.disable(credential_id, 700)
        patcher, calls = self._probe_with(200)
        with patcher:
            results = await self.checker.check("stt", "deepgram")
        self.assertEqual([row.status for row in results], ["disabled"])
        self.assertEqual(calls, [])
        self.assertFalse(results[0].usable)
        self.assertEqual(
            [row.status for row in await self.checker.check("notes", "anthropic")],
            ["not_configured"],
        )

    async def test_unknown_probe_endpoint_is_configured_not_unavailable(self):
        await self._add("primary", "dg-secret-primary-1234")
        patcher, _calls = self._probe_with(404, text="not found")
        with patcher:
            results = await self.checker.check("stt", "deepgram")
        self.assertEqual(results[0].status, "configured")
        self.assertTrue(results[0].usable)

    async def test_network_failure_is_unavailable_with_a_safe_detail(self):
        await self._add("primary", "dg-secret-primary-1234")

        async def failing_fetch(*_args, **_kwargs):
            raise __import__("aiohttp").ClientConnectorError(
                SimpleNamespace(host="api.deepgram.com", port=443), OSError("down")
            )

        with patch("gamas_bot.provider_health._fetch", failing_fetch):
            results = await self.checker.check("stt", "deepgram")
        self.assertEqual(results[0].status, "unavailable")
        self.assertNotIn("dg-secret", str(results[0]))

    async def test_manual_test_ignores_cooldown_and_reports_masked_key(self):
        credential_id = await self._add("primary", "dg-secret-primary-1234")
        with self._probe_with(429, retry_after="600")[0]:
            await self.checker.check("stt", "deepgram", force=True)
        with self._probe_with(200)[0]:
            result = await self.checker.test_credential(credential_id)
        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.masked, "••••••••1234")
        self.assertNotIn("dg-secret", repr(result))

    async def test_reordering_is_deterministic_and_audited(self):
        first = await self._add("one", "dg-secret-aaaa-1111")
        second = await self._add("two", "dg-secret-bbbb-2222")
        third = await self._add("three", "dg-secret-cccc-3333")
        self.assertTrue(await self.credentials.reorder(second, "up", 700))
        summaries = {row["id"]: row for row in await self.credentials.list_summaries()}
        self.assertLess(summaries[second]["priority"], summaries[first]["priority"])
        ordered = await self.credentials.candidates("stt", "deepgram")
        self.assertEqual(
            [credential.id for credential in ordered], [second, first, third]
        )
        # Movement is bounded: the first row cannot move up any further.
        self.assertFalse(await self.credentials.reorder(second, "up", 700))
        entries = await self.db.audit_entries(limit=10)
        self.assertIn("provider_credential_priority", [row["action"] for row in entries])
        details = next(
            row["details"] for row in entries if row["action"] == "provider_credential_priority"
        )
        self.assertEqual(details["direction"], "up")
        self.assertNotIn("dg-secret", str(details))

    async def test_probes_run_concurrently_so_many_keys_answer_in_one_timeout(self):
        import asyncio

        for index in range(4):
            await self._add(f"key-{index}", f"dg-secret-key-{index}{index}{index}{index}", priority=index)
        started: list[float] = []

        async def slow_fetch(method, url, headers, timeout):
            started.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.15)
            return _response(200)

        loop = asyncio.get_running_loop()
        began = loop.time()
        with patch("gamas_bot.provider_health._fetch", slow_fetch):
            results = await self.checker.check("stt", "deepgram", force=True)
        elapsed = loop.time() - began
        self.assertEqual(len(results), 4)
        # Sequential probing would need 0.6s; concurrent stays near one probe.
        self.assertLess(elapsed, 0.45, elapsed)

    async def test_provider_pool_listing_covers_configured_services_only(self):
        await self._add("primary", "dg-secret-primary-1234")
        pairs = await self.checker.providers()
        self.assertIn(("stt", "deepgram"), pairs)
        self.assertNotIn(("stt", "speechmatics"), pairs)


class HealthPanelIsolationTests(unittest.IsolatedAsyncioTestCase):
    """The health panel and its tests are administrator-only."""

    def _bot_and_event(self, *, private: bool = True, admin: bool = True):
        bot = StudyBot.__new__(StudyBot)
        bot.settings = make_settings(admin_ids=frozenset({7}) if admin else frozenset())

        async def upsert(telegram_id, username):
            return _user_row(telegram_id, admin)

        bot.db = SimpleNamespace(upsert_user=upsert)
        bot._pending_admin_actions = {}
        bot.provider_health = SimpleNamespace(
            invalidate=lambda: None,
            check_all=lambda force=False: _results(),
        )
        bot._show_provider_health = _recorder(bot, "_health_calls")
        bot._show_provider_credentials = _recorder(bot, "_credential_calls")
        event = SimpleNamespace(
            is_private=private,
            data=b"admin:health",
            get_sender=_sender,
            answer=_async(),
            edit=_async(),
            respond=_async(),
            reply=_async(),
        )
        return bot, event

    async def test_health_callback_requires_a_private_admin_chat(self):
        bot, event = self._bot_and_event(private=False)
        await bot._handle_callback(event)
        self.assertEqual(getattr(bot, "_health_calls", 0), 0)

        bot, event = self._bot_and_event(admin=False)
        await bot._handle_callback(event)
        self.assertEqual(getattr(bot, "_health_calls", 0), 0)

        bot, event = self._bot_and_event()
        await bot._handle_callback(event)
        self.assertEqual(getattr(bot, "_health_calls", 1), 1)

    def test_health_menu_entries_are_admin_only(self):
        user_callbacks = {button.type.data for row in main_menu(False) for button in row}
        admin_callbacks = {button.type.data for row in admin_menu() for button in row}
        self.assertNotIn(b"admin:health", user_callbacks)
        self.assertIn(b"admin:health", admin_callbacks)
        self.assertIn(b"admin:credits", admin_callbacks)
        self.assertIn(b"admin:credentials", admin_callbacks)
        self.assertIn(b"billing:balance", user_callbacks)
        self.assertIn(b"billing:plans", user_callbacks)

    def test_menu_labels_match_the_approved_ux(self):
        labels = [button.text for row in main_menu(False) for button in row]
        self.assertIn("⏱ اعتبار من", labels)
        self.assertIn("💳 خرید اشتراک", labels)
        seen = {button.text: button.type.data for row in admin_menu() for button in row}
        self.assertEqual(seen["🩺 وضعیت سرویس‌ها"], b"admin:health")
        self.assertEqual(seen["🔑 API Keys"], b"admin:credentials")
        self.assertEqual(seen["💳 پرداخت‌ها"], b"admin:payments")
        self.assertEqual(seen["⏱ اعتبار کاربران"], b"admin:credits")


class CredentialAdminActionTests(unittest.IsolatedAsyncioTestCase):
    """API-key admin actions: reorder, test, audit — all secret-free."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "admin.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({7}),
            provider_credentials_encryption_key=Fernet.generate_key().decode("ascii"),
            speechmatics_api_key=None,
            deepgram_api_key=None,
            stt_primary="deepgram",
            stt_fallback_enabled=False,
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()
        self.bot.credential_manager = ProviderCredentialManager(self.bot.db, self.settings)
        self.bot.provider_health = ProviderHealthChecker(
            self.bot.db, self.settings, self.bot.credential_manager
        )
        self.bot._pending_admin_actions = {}
        self.bot._pending_credential_setup = {}
        self.first = await self.bot.credential_manager.add_credential(
            service="stt", provider="deepgram", label="one",
            secret="dg-secret-one-1111", admin_id=7, priority=100,
        )
        self.second = await self.bot.credential_manager.add_credential(
            service="stt", provider="deepgram", label="two",
            secret="dg-secret-two-2222", admin_id=7, priority=200,
        )

    async def asyncTearDown(self):
        await self.bot.db.close()
        self.temp.cleanup()

    async def test_reorder_button_moves_a_key_and_invalidates_health_cache(self):
        invalidations: list[int] = []
        self.bot.provider_health = SimpleNamespace(invalidate=lambda: invalidations.append(1))
        event = SimpleNamespace(
            data=f"admin:credential:up:{self.second}".encode(),
            is_private=True,
            get_sender=AsyncMock(return_value=SimpleNamespace(id=7, username="admin", bot=False)),
            answer=AsyncMock(),
            edit=AsyncMock(),
            respond=AsyncMock(),
        )
        await self.bot._handle_callback(event)
        summaries = {row["id"]: row for row in await self.bot.credential_manager.list_summaries()}
        self.assertLess(summaries[self.second]["priority"], summaries[self.first]["priority"])
        self.assertEqual(invalidations, [1])
        entries = await self.bot.db.audit_entries(limit=5)
        self.assertIn("provider_credential_priority", [row["action"] for row in entries])

    async def test_credential_test_is_reported_masked_and_audited(self):
        from gamas_bot.provider_health import HealthResult

        result = HealthResult(
            service="stt",
            provider="deepgram",
            credential_id=self.first,
            source="database",
            label="one",
            masked="••••••••1111",
            status="healthy",
            http_status=200,
            latency_ms=42,
            checked_at="2026-10-07T00:00:00+00:00",
        )
        async def fake_test(credential_id, force=True):
            self.assertEqual(int(credential_id), self.first)
            return result

        self.bot.provider_health = SimpleNamespace(test_credential=fake_test)
        event = SimpleNamespace(answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        await self.bot._test_credential_health(event, self.first, 7)
        shown = event.edit.await_args.args[0]
        self.assertIn("••••••••1111", shown)
        self.assertNotIn("dg-secret-one-1111", shown)
        entries = await self.bot.db.audit_entries(limit=5)
        tested = next(row for row in entries if row["action"] == "provider_credential_tested")
        self.assertEqual(tested["details"]["status"], "healthy")
        self.assertEqual(tested["details"]["masked"], "••••••••1111")
        self.assertNotIn("dg-secret", str(tested["details"]))

    async def test_health_panel_text_reports_every_state_without_a_key(self):
        async def fake_check_all(force=False):
            from gamas_bot.provider_health import HealthResult

            return [
                HealthResult(
                    service="stt", provider="deepgram", credential_id=self.first,
                    source="database", label="one", masked="••••••••1111",
                    status="rate_limited", http_status=429, latency_ms=12,
                    checked_at="2026-10-07T00:00:00+00:00",
                    cooldown_until="2099-01-01T00:00:00+00:00", detail="too many requests",
                ),
                HealthResult(
                    service="notes", provider="gemini", credential_id=None,
                    source="none", label="—", masked="—", status="not_configured",
                    detail="کلیدی برای این سرویس تنظیم نشده است.",
                ),
            ]

        self.bot.provider_health = SimpleNamespace(check_all=fake_check_all)
        event = SimpleNamespace(answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        await self.bot._show_provider_health(event)
        shown = event.edit.await_args.args[0]
        self.assertIn("سقف درخواست", shown)
        self.assertIn("HTTP 429", shown)
        self.assertIn("cooldown", shown)
        self.assertIn("تنظیم‌نشده", shown)
        self.assertIn("••••••••1111", shown)
        self.assertNotIn("dg-secret", shown)
        buttons = event.edit.await_args.kwargs.get("buttons") or []
        callbacks = [button.type.data for row in buttons for button in row]
        self.assertIn(f"admin:health:test:{self.first}".encode(), callbacks)
        self.assertIn(b"admin:health:refresh", callbacks)


def _user_row(telegram_id: int, admin: bool) -> dict:
    return {"id": 1, "telegram_id": telegram_id, "is_banned": False, "is_admin": admin}


async def _sender():
    return SimpleNamespace(id=7, username="admin", bot=False)


def _async():
    async def call(*_args, **_kwargs):
        return None

    return call


async def _results():
    return []


def _recorder(bot, attribute: str):
    async def call(*_args, **_kwargs):
        setattr(bot, attribute, getattr(bot, attribute, 0) + 1)

    return call


if __name__ == "__main__":
    unittest.main()
