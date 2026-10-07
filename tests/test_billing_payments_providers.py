"""Regressions for env-driven plans, migration 004, payments, admin credit,
Gemini invalid-key rotation, provider health checks and the bot UI wiring."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from gamas_bot import provider_health
from gamas_bot.billing import plan_catalog
from gamas_bot.bot import BUY_BUTTON_LABEL, _is_image_message, admin_menu, main_menu
from gamas_bot.config import Settings
from gamas_bot.database import Database, split_sql_statements
from gamas_bot.provider_credentials import (
    ProviderCredential,
    ProviderCredentialManager,
    use_provider_credentials,
)
from gamas_bot.structuring import ProviderHTTPError, _structure_chunk, is_invalid_key_error
from support import make_settings

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
GEMINI_INVALID_KEY = (
    b'{"error":{"code":400,"message":"API key not valid. Please pass a valid API key.",'
    b'"status":"INVALID_ARGUMENT","details":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo",'
    b'"reason":"API_KEY_INVALID","domain":"googleapis.com"}]}}'
)


def _labels(rows) -> list[str]:
    return [button.text for row in rows for button in row]


class PlanConfigTests(unittest.TestCase):
    def _env(self, **values):
        base = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "x", "TELEGRAM_BOT_TOKEN": "1:x"}
        base.update(values)
        return patch.dict(os.environ, base, clear=False)

    def test_defaults_match_the_canonical_catalogue(self):
        plans = {plan.code: plan for plan in plan_catalog(make_settings())}
        self.assertEqual(plans["free_lifetime_1h"].included_seconds, 3_600)
        self.assertIsNone(plans["free_lifetime_1h"].validity_days)
        self.assertEqual(
            (plans["paid_25h_30d"].included_seconds, plans["paid_25h_30d"].price_toman,
             plans["paid_25h_30d"].validity_days),
            (90_000, 150_000, 30),
        )
        self.assertEqual(
            (plans["paid_50h_30d"].included_seconds, plans["paid_50h_30d"].price_toman,
             plans["paid_50h_30d"].validity_days),
            (180_000, 250_000, 30),
        )

    def test_environment_resizes_plans_in_whole_hours(self):
        with self._env(FREE_PLAN_HOURS="2", PLAN_25_HOURS="30", PLAN_25_PRICE_TOMAN="175000",
                       PLAN_25_VALIDITY_DAYS="45"):
            settings = Settings.from_env()
        plans = {plan.code: plan for plan in plan_catalog(settings)}
        self.assertEqual(plans["free_lifetime_1h"].included_seconds, 7_200)
        self.assertEqual(plans["paid_25h_30d"].included_seconds, 108_000)
        self.assertEqual(plans["paid_25h_30d"].price_toman, 175_000)
        self.assertEqual(plans["paid_25h_30d"].validity_days, 45)
        for plan in plans.values():
            self.assertIsInstance(plan.included_seconds, int)

    def test_fractional_or_non_positive_plan_values_are_rejected(self):
        for name, value in (("FREE_PLAN_HOURS", "1.5"), ("PLAN_50_HOURS", "0"),
                            ("PLAN_25_PRICE_TOMAN", "-1"), ("PLAN_50_VALIDITY_DAYS", "abc")):
            with self.subTest(name=name, value=value), self._env(**{name: value}):
                with self.assertRaises(ValueError):
                    Settings.from_env()

    def test_card_is_displayed_through_the_single_grouping_helper(self):
        settings = make_settings()
        self.assertEqual(settings.payment_card_display, "5022 2913 3290 6625")
        self.assertEqual(settings.payment_card_holder, "امیرعلی غمخوار")
        self.assertEqual(settings.payment_bank_name, "بانک پاسارگاد")


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "billing.sqlite3")
        await self.db.open()
        await self.db.sync_plan_catalog([plan.as_record() for plan in plan_catalog()])

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _user(self, telegram_id: int = 501) -> dict:
        return await self.db.upsert_user(telegram_id, "student")

    async def _submission(self, user_id: int, name: str = "a.wav") -> int:
        return await self.db.create_submission(user_id, f"file-{name}", 60, name, "audio/wav")

    async def _approved(self, user_id: int, plan: str = "paid_25h_30d") -> dict:
        request = await self.db.create_payment_request(user_id, plan)
        self.assertTrue(await self.db.submit_payment_receipt(int(request["id"]), user_id, "/p/r.jpg", 1))
        approved = await self.db.approve_payment(int(request["id"]), 700)
        self.assertIsNotNone(approved)
        return approved

    async def _query(self, sql: str, params=()):
        cursor = await self.db._db().execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]


class Migration004Tests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_from_003_keeps_rows_foreign_keys_and_derives_states(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.execute("PRAGMA foreign_keys=ON")
            for name in ("001_initial.sql", "002_presentations.sql", "003_billing_and_credentials.sql"):
                for statement in split_sql_statements((MIGRATIONS / name).read_text(encoding="utf-8")):
                    legacy.execute(statement)
            legacy.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            for name in ("001_initial.sql", "002_presentations.sql", "003_billing_and_credentials.sql"):
                legacy.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (name,))
            now = "2026-01-01T00:00:00+00:00"
            legacy.execute("INSERT INTO users(telegram_id, username, first_seen) VALUES (1, 'a', ?)", (now,))
            plan_id = legacy.execute("SELECT id FROM plans WHERE code='free_lifetime_1h'").fetchone()[0]
            legacy.execute(
                "INSERT INTO entitlements (user_id, plan_id, granted_seconds, remaining_seconds, starts_at, "
                "status, source, created_at, updated_at) VALUES (1, ?, 3600, 0, ?, 'active', 'free_lifetime', ?, ?)",
                (plan_id, now, now, now),
            )
            legacy.execute(
                "INSERT INTO usage_ledger (entitlement_id, user_id, event_type, released_seconds, reason, created_at) "
                "VALUES (1, 1, 'adjustment', 10, 'Paid entitlement expired', ?)", (now,),
            )
            legacy.commit()
            legacy.close()

            db = Database(path)
            await db.open()
            try:
                cursor = await db._db().execute("SELECT status FROM entitlements WHERE id=1")
                self.assertEqual((await cursor.fetchone())[0], "exhausted")
                cursor = await db._db().execute("SELECT event_type FROM usage_ledger WHERE entitlement_id=1")
                self.assertEqual((await cursor.fetchone())[0], "expiration")
                cursor = await db._db().execute("PRAGMA foreign_key_check")
                self.assertEqual(await cursor.fetchall(), [])
                cursor = await db._db().execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND sql LIKE '%entitlements_v3%'"
                )
                self.assertEqual(await cursor.fetchall(), [])  # no dangling references
            finally:
                await db.close()
            # Re-opening is a no-op (idempotent runner).
            db = Database(path)
            await db.open()
            await db.close()


class EntitlementLifecycleTests(_DbCase):
    async def test_drained_entitlement_is_exhausted_and_reactivated_by_release(self):
        user = await self._user()
        submission = await self._submission(int(user["id"]))
        self.assertTrue((await self.db.reserve_usage(int(user["id"]), submission, 3_600))["ok"])
        rows = await self._query("SELECT status FROM entitlements WHERE user_id=?", (int(user["id"]),))
        self.assertEqual(rows[0]["status"], "exhausted")
        self.assertEqual(await self.db.release_usage(submission, "failed"), 3_600)
        rows = await self._query("SELECT status, remaining_seconds FROM entitlements WHERE user_id=?",
                                 (int(user["id"]),))
        self.assertEqual((rows[0]["status"], rows[0]["remaining_seconds"]), ("active", 3_600))

    async def test_expiry_writes_an_expiration_event_and_free_never_expires(self):
        user = await self._user()
        await self._approved(int(user["id"]))
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await self.db._db().execute(
            "UPDATE entitlements SET expires_at=? WHERE source='payment'", (past,)
        )
        await self.db._db().commit()
        balance = await self.db.user_balance(int(user["id"]))
        self.assertEqual(balance["available_seconds"], 3_600)  # only the free hour remains
        events = await self._query(
            "SELECT event_type, released_seconds FROM usage_ledger WHERE event_type='expiration'"
        )
        self.assertEqual(events, [{"event_type": "expiration", "released_seconds": 90_000}])
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db._db().execute(
                "UPDATE entitlements SET expires_at=? WHERE source='free_lifetime'", (past,)
            )
        await self.db._db().rollback()

    async def test_concurrent_reservations_never_overdraw_the_balance(self):
        user = await self._user()
        submissions = [await self._submission(int(user["id"]), f"{i}.wav") for i in range(6)]
        results = await asyncio.gather(
            *(self.db.reserve_usage(int(user["id"]), sid, 1_000) for sid in submissions)
        )
        self.assertEqual(sum(1 for r in results if r["ok"]), 3)  # 3600 // 1000
        balance = await self.db.user_balance(int(user["id"]))
        self.assertEqual(balance["available_seconds"], 600)
        rows = await self._query("SELECT MIN(remaining_seconds) AS low FROM entitlements")
        self.assertGreaterEqual(rows[0]["low"], 0)


class AdminCreditTests(_DbCase):
    async def test_debit_takes_earliest_expiring_first_and_never_goes_negative(self):
        user = await self._user(77)
        await self._approved(int(user["id"]))
        denied = await self.db.admin_debit(77, 10_000_000, 700, "too much")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["available_seconds"], 93_600)
        result = await self.db.admin_debit(77, 1_000, 700, "manual correction")
        self.assertTrue(result["ok"])
        rows = await self._query(
            "SELECT source, remaining_seconds FROM entitlements WHERE user_id=? ORDER BY id",
            (int(user["id"]),),
        )
        # The expiring paid plan is debited; the non-expiring free hour is kept.
        self.assertEqual({r["source"]: r["remaining_seconds"] for r in rows},
                         {"free_lifetime": 3_600, "payment": 89_000})
        audit = await self._query("SELECT action, details_json FROM admin_audit_log WHERE action='credit_debited'")
        self.assertEqual(len(audit), 1)
        self.assertIn("manual correction", audit[0]["details_json"])
        ledger = await self._query("SELECT event_type, consumed_seconds FROM usage_ledger WHERE event_type='debit'")
        self.assertEqual(ledger, [{"event_type": "debit", "consumed_seconds": 1_000}])

    async def test_adjustments_require_positive_seconds_and_a_reason(self):
        await self._user(78)
        for seconds, reason in ((0, "x"), (-5, "x"), (10, ""), (10, "   ")):
            with self.subTest(seconds=seconds, reason=reason):
                with self.assertRaises(ValueError):
                    await self.db.admin_debit(78, seconds, 700, reason)
                with self.assertRaises(ValueError):
                    await self.db.add_admin_credit(78, seconds, 700, reason)
        self.assertIsNone(await self.db.admin_debit(99999, 10, 700, "unknown user"))


class PaymentReviewTests(_DbCase):
    async def test_rejection_reason_is_optional_and_resubmission_is_allowed(self):
        user = await self._user()
        request = await self.db.create_payment_request(int(user["id"]), "paid_50h_30d")
        await self.db.submit_payment_receipt(int(request["id"]), int(user["id"]), "/p/r.jpg", 1)
        self.assertTrue(await self.db.reject_payment(int(request["id"]), 700, ""))
        detail = await self.db.payment_detail(int(request["id"]))
        self.assertEqual(detail["status"], "rejected")
        self.assertIsNone(detail["rejection_reason"])
        self.assertEqual((await self.db.user_balance(int(user["id"])))["available_seconds"], 3_600)
        again = await self.db.create_payment_request(int(user["id"]), "paid_50h_30d")
        self.assertNotEqual(again["id"], request["id"])

    async def test_status_lists_counts_and_double_approval(self):
        user = await self._user()
        approved = await self._approved(int(user["id"]))
        self.assertIsNone(await self.db.approve_payment(approved["payment_id"], 701))  # idempotent
        counts = await self.db.payment_status_counts()
        self.assertEqual(counts, {"pending": 0, "approved": 1, "rejected": 0})
        rows = await self.db.list_payments("approved")
        self.assertEqual([row["id"] for row in rows], [approved["payment_id"]])
        with self.assertRaises(ValueError):
            await self.db.list_payments("approved; DROP TABLE users")
        grants = await self._query(
            "SELECT COUNT(*) AS n FROM entitlements WHERE payment_id=?", (approved["payment_id"],)
        )
        self.assertEqual(grants[0]["n"], 1)


class _Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content = self

    async def read(self, _limit=-1):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, allow_redirects=True):
        self.calls.append((url, headers or {}))
        return self.response


class ProviderHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        provider_health.clear_cache()
        self.settings = make_settings()

    def _credential(self, service="notes", provider="gemini", secret="AIza-secret-key-9876", cid=1):
        return ProviderCredential(cid, service, provider, "k", secret, secret[-4:])

    def test_classification_covers_every_state_input(self):
        self.assertEqual(provider_health.classify(200, 100), "healthy")
        self.assertEqual(provider_health.classify(200, 9_000), "degraded")
        self.assertEqual(provider_health.classify(429, 50), "rate_limited")
        self.assertEqual(provider_health.classify(401, 50), "authentication_failed")
        self.assertEqual(provider_health.classify(400, 50, GEMINI_INVALID_KEY), "authentication_failed")
        self.assertEqual(provider_health.classify(400, 50, b'{"error":{"status":"INVALID_ARGUMENT"}}'), "degraded")
        self.assertEqual(provider_health.classify(503, 50), "unavailable")
        self.assertEqual(provider_health.classify(None, None), "unavailable")
        self.assertEqual(set(provider_health.STATE_LABELS), set(provider_health.STATES))

    async def test_check_uses_a_free_listing_endpoint_masks_key_and_is_cached(self):
        session = _FakeSession(_Response(200, b"{}"))
        credential = self._credential()
        first = await provider_health.health_check("gemini", credential, settings=self.settings, session=session)
        second = await provider_health.health_check("gemini", credential, settings=self.settings, session=session)
        self.assertEqual(first.state, "healthy")
        self.assertEqual(first.masked_key, "••••••••9876")
        self.assertTrue(second.cached)
        self.assertEqual(len(session.calls), 1)
        url, headers = session.calls[0]
        self.assertTrue(url.endswith("/models?pageSize=1"))
        self.assertNotIn("generateContent", url)
        self.assertNotIn(credential.secret, url)  # key travels in a header, not the URL

    async def test_endpoints_for_every_provider_are_read_only(self):
        expected = {
            ("stt", "speechmatics"): "/jobs?limit=1",
            ("stt", "deepgram"): "/v1/projects",
            ("stt", "openai_compatible"): "/models",
            ("notes", "anthropic"): "/models?limit=1",
            ("notes", "openai_compatible"): "/models",
        }
        for (service, provider), suffix in expected.items():
            with self.subTest(provider=provider):
                session = _FakeSession(_Response(200))
                cred = ProviderCredential(None, service, provider, "k", "secret-abcd", "abcd",
                                          base_url="https://example.test/v1")
                await provider_health.health_check(provider, cred, settings=self.settings,
                                                   session=session, use_cache=False)
                self.assertTrue(session.calls[0][0].endswith(suffix), session.calls[0][0])

    async def test_errors_are_sanitized_and_retry_after_is_reported(self):
        secret = "sk-very-secret-value-0000000000000000"
        body = ('{"error":{"type":"rate_limit","message":"key ' + secret + ' over quota"}}').encode()
        session = _FakeSession(_Response(429, body, {"Retry-After": "120"}))
        result = await provider_health.health_check(
            "openai_compatible", self._credential("notes", "openai_compatible", secret),
            settings=self.settings, session=session, use_cache=False,
        )
        self.assertEqual(result.state, "rate_limited")
        self.assertEqual(result.retry_after_seconds, 120)
        self.assertNotIn(secret, repr(result))
        self.assertEqual(result.detail, "rate_limit")

    async def test_unconfigured_provider_makes_no_request(self):
        session = _FakeSession(_Response(200))
        result = await provider_health.health_check(
            "deepgram", ProviderCredential(None, "stt", "deepgram", "k", "", ""),
            settings=self.settings, session=session,
        )
        self.assertEqual(result.state, "not_configured")
        self.assertEqual(session.calls, [])


class CredentialRotationAndAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "c.sqlite3")
        await self.db.open()
        self.key = Fernet.generate_key().decode("ascii")
        self.settings = make_settings(provider_credentials_encryption_key=self.key)
        self.manager = ProviderCredentialManager(self.db, self.settings)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _add(self, label, secret, priority):
        return await self.manager.add_credential(
            service="notes", provider="gemini", label=label, secret=secret, admin_id=700, priority=priority,
        )

    def test_gemini_invalid_key_is_recognised_only_from_structured_reasons(self):
        self.assertTrue(is_invalid_key_error(400, GEMINI_INVALID_KEY))
        self.assertFalse(is_invalid_key_error(400, b'{"error":{"status":"INVALID_ARGUMENT","message":"bad json"}}'))
        self.assertFalse(is_invalid_key_error(500, GEMINI_INVALID_KEY))
        self.assertFalse(is_invalid_key_error(400, b"<html>"))
        self.assertTrue(ProviderHTTPError("x", status=403).auth_failed)

    async def test_gemini_400_api_key_invalid_quarantines_and_rotates(self):
        first = await self._add("primary", "gemini-primary-1111", 1)
        second = await self._add("backup", "gemini-backup-2222", 2)
        settings = make_settings(note_api_provider="gemini", gemini_api_key=None, note_api_key=None,
                                 provider_credentials_encryption_key=self.key)
        seen = []

        async def fake_once(_chunk, request_settings, _session, *_a, **_k):
            seen.append(request_settings.effective_note_api_key)
            if request_settings.effective_note_api_key == "gemini-primary-1111":
                raise ProviderHTTPError("bad key", status=400, auth_failed=True)
            return "ok"

        class _S:
            pass

        with use_provider_credentials(self.manager), patch(
            "gamas_bot.structuring._structure_chunk_once", side_effect=fake_once
        ):
            self.assertEqual(await _structure_chunk("text", settings, _S()), "ok")
        self.assertEqual(seen, ["gemini-primary-1111", "gemini-backup-2222"])
        summaries = {row["id"]: row for row in await self.manager.list_summaries()}
        self.assertIsNotNone(summaries[first]["quarantined_at"])
        self.assertIsNone(summaries[second]["quarantined_at"])
        self.assertIn("API_KEY_INVALID", summaries[first]["last_error"])

    async def test_plain_400_still_does_not_rotate(self):
        await self._add("primary", "gemini-primary-1111", 1)
        await self._add("backup", "gemini-backup-2222", 2)
        settings = make_settings(note_api_provider="gemini", gemini_api_key=None, note_api_key=None,
                                 provider_credentials_encryption_key=self.key)
        calls = []

        async def fake_once(_chunk, request_settings, *_a, **_k):
            calls.append(request_settings.effective_note_api_key)
            raise ProviderHTTPError("bad request", status=400)

        with use_provider_credentials(self.manager), patch(
            "gamas_bot.structuring._structure_chunk_once", side_effect=fake_once
        ):
            with self.assertRaises(ProviderHTTPError):
                await _structure_chunk("text", settings, object())
        self.assertEqual(calls, ["gemini-primary-1111"])

    async def test_reorder_is_deterministic_and_audited_without_secrets(self):
        a = await self._add("a", "gemini-aaaa-1111", 1)
        b = await self._add("b", "gemini-bbbb-2222", 2)
        self.assertTrue(await self.manager.move(b, -1, 700))
        self.assertFalse(await self.manager.move(b, -1, 700))  # already first
        records = await self.db.provider_credential_records("notes", "gemini")
        self.assertEqual([row["id"] for row in records], [b, a])
        await self.db.record_provider_health(a, state="healthy", latency_ms=12, status_code=200,
                                             detail="ok", admin_id=700)
        cursor = await self.db._db().execute("SELECT details_json FROM admin_audit_log")
        audit = " ".join(row[0] for row in await cursor.fetchall())
        self.assertNotIn("gemini-aaaa-1111", audit)
        self.assertNotIn("gemini-bbbb-2222", audit)
        loaded, enabled = await self.manager.credential_for_check(a)
        self.assertTrue(enabled)
        self.assertEqual(loaded.masked, "••••••••1111")
        self.assertNotIn("gemini-aaaa-1111", repr(loaded))


class _Message:
    def __init__(self, photo=None, document=None, mime=None):
        self.photo = photo
        self.document = document
        self.file = type("F", (), {"mime_type": mime})()


class BotWiringTests(unittest.TestCase):
    def test_main_menu_order_and_labels(self):
        self.assertEqual(
            _labels(main_menu(False)),
            ["📎 ساخت جزوه", "⏱ اعتبار من", "💳 خرید اشتراک", "📚 راهنما", "🧰 قالب‌ها", "🔐 حریم خصوصی"],
        )
        self.assertEqual(BUY_BUTTON_LABEL, "💳 خرید اشتراک")

    def test_admin_menu_contains_every_required_screen(self):
        labels = _labels(admin_menu())
        for label in ("📊 آمار", "👥 کاربران", "💳 پرداخت‌ها", "⏱ اعتبار کاربران",
                      "🩺 وضعیت سرویس‌ها", "🔑 API Keys", "📣 پیام همگانی",
                      "🚫 مسدودسازی", "✅ رفع مسدودیت"):
            self.assertIn(label, labels)

    def test_only_images_are_treated_as_receipts(self):
        self.assertTrue(_is_image_message(_Message(photo=object())))
        self.assertTrue(_is_image_message(_Message(document=object(), mime="image/png")))
        for mime in ("audio/mpeg", "video/mp4", "application/vnd.ms-powerpoint", None):
            with self.subTest(mime=mime):
                self.assertFalse(_is_image_message(_Message(document=object(), mime=mime)))


if __name__ == "__main__":
    unittest.main()
