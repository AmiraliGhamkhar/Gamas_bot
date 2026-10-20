"""Payment/status/usage regression tests from the 2026-10-20 review."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest import mock
from dataclasses import replace

from gamas_bot.db import Database, InsufficientBalanceError, OpenPaymentError, UserBannedError
from gamas_bot.billing import format_toman
from gamas_bot.jobs import Job
from tests.test_db_billing import DatabaseTestCase
from tests.test_bot import BotPipelineTestCase, FakeCallbackEvent, USER_ID, menu_payloads


class CreditLotTests(DatabaseTestCase):
    async def spend(self, seconds):
        sid = await self.db.create_submission(100, "audio", "test.wav")
        await self.db.reserve(100, sid, seconds)
        await self.db.settle_hold(sid, seconds)
        return sid

    async def test_renewal_does_not_pay_for_old_usage_again(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with mock.patch("gamas_bot.db.utc_now", return_value=now):
            await self.grant(100, 3600, days=1)
            await self.spend(1800)
        with mock.patch("gamas_bot.db.utc_now", return_value=now + timedelta(days=2)):
            await self.grant(100, 3600, days=30)
            self.assertEqual(await self.db.available_seconds(100), 3600)
            await self.spend(3600)
            self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_earliest_expiry_consumed_before_lifetime_credit(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with mock.patch("gamas_bot.db.utc_now", return_value=now):
            await self.grant(100, 100)  # permanent, inserted first
            await self.grant(100, 100, days=2)
            await self.grant(100, 100, days=1)
            await self.spend(150)
        with mock.patch("gamas_bot.db.utc_now", return_value=now + timedelta(days=1, seconds=1)):
            self.assertEqual(await self.db.available_seconds(100), 150)
        with mock.patch("gamas_bot.db.utc_now", return_value=now + timedelta(days=3)):
            self.assertEqual(await self.db.available_seconds(100), 100)

    async def test_hold_survives_expiry_without_consuming_a_new_purchase(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with mock.patch("gamas_bot.db.utc_now", return_value=now):
            await self.grant(100, 100, days=1)
            sid = await self.db.create_submission(100, "audio", "a")
            await self.db.reserve(100, sid, 100)
        with mock.patch("gamas_bot.db.utc_now", return_value=now + timedelta(days=2)):
            await self.grant(100, 200, days=1)
            await self.db.settle_hold(sid, 80)
            self.assertEqual(await self.db.available_seconds(100), 200)

    async def test_releasing_expired_hold_does_not_revive_expired_credit(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with mock.patch("gamas_bot.db.utc_now", return_value=now):
            await self.grant(100, 100, days=1)
            sid = await self.db.create_submission(100, "audio", "a")
            await self.db.reserve(100, sid, 100)
        with mock.patch("gamas_bot.db.utc_now", return_value=now + timedelta(days=2)):
            await self.db.release_hold(sid, "failed")
            self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_partial_settlement_restores_unused_lot_remainders(self):
        await self.grant(100, 100, days=1)
        await self.grant(100, 200)
        sid = await self.db.create_submission(100, "audio", "a")
        await self.db.reserve(100, sid, 250)
        await self.db.settle_hold(sid, 150)
        self.assertEqual(await self.db.available_seconds(100), 150)
        allocations = await self.db.db.execute_fetchall("SELECT seconds FROM debit_allocations ORDER BY credit_id")
        self.assertEqual([r[0] for r in allocations], [100, 50])

    async def test_concurrent_settlement_charges_once(self):
        await self.grant(100, 1000)
        sid = await self.db.create_submission(100, "audio", "a")
        await self.db.reserve(100, sid, 100)
        await asyncio.gather(*(self.db.settle_hold(sid, 100) for _ in range(10)))
        self.assertEqual(await self.db.available_seconds(100), 900)
        rows = await self.db.db.execute_fetchall("SELECT COUNT(*) FROM ledger WHERE kind = 'debit'")
        self.assertEqual(rows[0][0], 1)

    async def test_concurrent_release_and_settle_have_one_winner(self):
        await self.grant(100, 1000)
        sid = await self.db.create_submission(100, "audio", "a")
        await self.db.reserve(100, sid, 100)
        released, _ = await asyncio.gather(self.db.release_hold(sid, "cancel"), self.db.settle_hold(sid, 100))
        self.assertEqual(await self.db.available_seconds(100), 1000 if released else 900)
        await self.db.settle_hold(sid, 100)
        self.assertEqual(await self.db.available_seconds(100), 1000 if released else 900)

    async def test_unlimited_toggle_releases_running_hold_permanently(self):
        await self.grant(100, 1000)
        sid = await self.db.create_submission(100, "audio", "a")
        await self.db.reserve(100, sid, 100)
        await self.db.set_unlimited(100, True, admin_id=1, reason="special")
        self.assertEqual(await self.db.available_seconds(100), 1000)
        await self.db.set_unlimited(100, False, admin_id=1)
        await self.db.settle_hold(sid, 100)
        self.assertEqual(await self.db.available_seconds(100), 1000)
        await self.db.set_submission_status(sid, "failed")
        self.assertEqual((await self.db.submission(sid)).status, "partial")

    async def test_two_connections_cannot_overreserve(self):
        await self.grant(100, 100)
        other = Database(self.path)
        await other.connect()
        try:
            a = await self.db.create_submission(100, "audio", "a")
            b = await self.db.create_submission(100, "audio", "b")
            results = await asyncio.gather(self.db.reserve(100, a, 100), other.reserve(100, b, 100), return_exceptions=True)
            self.assertEqual(sum(isinstance(r, InsufficientBalanceError) for r in results), 1)
            self.assertEqual(await self.db.available_seconds(100), 0)
        finally:
            await other.close()

    async def test_settlement_on_two_connections_charges_once(self):
        await self.grant(100, 1000)
        sid = await self.db.create_submission(100, "audio", "a")
        await self.db.reserve(100, sid, 100)
        other = Database(self.path)
        await other.connect()
        try:
            await asyncio.gather(self.db.settle_hold(sid, 100), other.settle_hold(sid, 100))
            self.assertEqual(await self.db.available_seconds(100), 900)
        finally:
            await other.close()

    async def test_unlimited_and_settlement_race_is_serialized(self):
        await self.grant(100, 1000)
        sid = await self.db.create_submission(100, "audio", "a")
        await self.db.reserve(100, sid, 100)
        await asyncio.gather(
            self.db.set_unlimited(100, True, admin_id=1, reason="special"),
            self.db.settle_hold(sid, 100),
        )
        # set_unlimited acquired the write lock first; no debit may follow it.
        self.assertEqual(await self.db.available_seconds(100), 1000)
        self.assertEqual((await self.db.db.execute_fetchall("SELECT COUNT(*) FROM ledger WHERE kind = 'debit'"))[0][0], 0)

    async def test_banned_unlimited_user_cannot_reserve(self):
        await self.db.set_unlimited(100, True, admin_id=1, reason="special")
        await self.db.set_banned(100, True)
        sid = await self.db.create_submission(100, "audio", "a")
        with self.assertRaises(UserBannedError):
            await self.db.reserve(100, sid, 100)


class TransactionAndPaymentTests(DatabaseTestCase):
    async def payment(self):
        return await self.db.create_payment(100, "paid_5h_30d")

    async def fail_inserts(self, table):
        await self.db._write(
            f"CREATE TEMP TRIGGER fail_insert BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )

    async def test_approval_on_two_connections_credits_once(self):
        pid = await self.payment()
        other = Database(self.path)
        await other.connect()
        try:
            results = await asyncio.gather(
                self.db.review_payment(pid, True, 1), other.review_payment(pid, True, 2)
            )
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(await self.db.available_seconds(100), 18000)
        finally:
            await other.close()

    async def test_approval_failure_rolls_back_status_credit_and_audit(self):
        pid = await self.payment()
        await self.fail_inserts("audit")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.review_payment(pid, True, 1)
        await self.db.ensure_user(200)
        self.assertEqual((await self.db.payment(pid))["status"], "pending")
        self.assertEqual(await self.db.available_seconds(100), 0)
        await self.db._write("DROP TRIGGER fail_insert")
        self.assertTrue(await self.db.review_payment(pid, True, 1))
        self.assertEqual(await self.db.available_seconds(100), 18000)

    async def test_approval_cancelled_mid_transaction_rolls_back(self):
        pid = await self.payment()
        execute = self.db.db.execute

        async def cancel_on_audit(sql, parameters=()):
            if sql.startswith("INSERT INTO audit"):
                raise asyncio.CancelledError
            return await execute(sql, parameters)

        with mock.patch.object(self.db.db, "execute", side_effect=cancel_on_audit):
            with self.assertRaises(asyncio.CancelledError):
                await self.db.review_payment(pid, True, 1)
        await self.db.ensure_user(200)
        self.assertEqual((await self.db.payment(pid))["status"], "pending")
        self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_settlement_allocation_failure_rolls_back_debit_and_hold(self):
        await self.grant(100, 1000)
        sid = await self.db.create_submission(100, "audio", "a")
        await self.db.reserve(100, sid, 100)
        await self.fail_inserts("debit_allocations")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.settle_hold(sid, 100)
        await self.db.ensure_user(200)
        self.assertEqual((await self.db.db.execute_fetchall("SELECT COUNT(*) FROM ledger WHERE kind = 'debit'"))[0][0], 0)
        self.assertEqual(await self.db.available_seconds(100), 900)
        await self.db._write("DROP TRIGGER fail_insert")
        await self.db.settle_hold(sid, 100)
        self.assertEqual(await self.db.available_seconds(100), 900)

    async def test_flag_and_grant_changes_rollback_when_audit_fails(self):
        await self.fail_inserts("audit")
        for operation in (
            self.db.set_banned(100, True, admin_id=1),
            self.db.set_unlimited(100, True, admin_id=1, reason="special"),
            self.db.add_credit(100, 100, note="admin grant", admin_id=1),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                await operation
        await self.db.ensure_user(200)
        self.assertFalse(await self.db.is_banned(100))
        self.assertFalse(await self.db.is_unlimited(100))
        self.assertEqual(await self.db.available_seconds(100), 0)

    async def test_cancellation_checks_owner_receipt_and_terminal_status(self):
        pid = await self.payment()
        self.assertFalse(await self.db.cancel_payment(pid, 999))
        self.assertTrue(await self.db.cancel_payment(pid, 100))
        self.assertFalse(await self.db.cancel_payment(pid, 100))
        self.assertFalse(await self.db.review_payment(pid, True, 1))
        self.assertFalse(await self.db.set_payment_receipt(pid, "late.jpg"))
        new = await self.payment()
        self.assertNotEqual(pid, new)
        self.assertTrue(await self.db.set_payment_receipt(new, "submitted.jpg"))
        self.assertFalse(await self.db.cancel_payment(new, 100))
        self.assertTrue(await self.db.review_payment(new, True, 1))
        self.assertFalse(await self.db.set_payment_receipt(new, "replacement.jpg"))

    async def test_receipt_upload_and_cancel_cannot_both_win(self):
        pid = await self.payment()
        cancelled, uploaded = await asyncio.gather(
            self.db.cancel_payment(pid, 100), self.db.set_payment_receipt(pid, "receipt.jpg")
        )
        self.assertNotEqual(cancelled, uploaded)

    async def test_existing_request_survives_plan_deactivation(self):
        pid = await self.payment()
        await self.db._write("UPDATE plans SET active = 0 WHERE code = 'paid_5h_30d'")
        self.assertEqual(await self.payment(), pid)
        await self.db.sync_plans([{"code": "other", "hours": 1, "price": 100, "validity_days": 30}])
        with self.assertRaises(OpenPaymentError):
            await self.db.create_payment(100, "other")
        await self.db.cancel_payment(pid, 100)
        self.assertIsInstance(await self.db.create_payment(100, "other"), int)

    async def test_restart_after_settlement_preserves_partial_status_and_usage(self):
        await self.grant(100, 1000)
        sid = await self.db.create_submission(100, "audio", "a")
        await self.db.set_submission_status(sid, "processing", duration_seconds=100)
        await self.db.reserve(100, sid, 100)
        await self.db.settle_hold(sid, 100)
        self.assertEqual(await self.db.recover_interrupted(), 1)
        self.assertEqual((await self.db.submission(sid)).status, "partial")
        self.assertEqual(await self.db.available_seconds(100), 900)
        stats = await self.db.stats()
        self.assertEqual(stats["seconds_transcribed"], 100)
        self.assertEqual(stats["submissions_partial"], 1)
        self.assertEqual(await self.db.recover_interrupted(), 0)


class WorkflowBotTests(BotPipelineTestCase):
    async def test_queued_banned_job_never_enters_pipeline(self):
        event = self.media_event()
        await self.db.set_banned(USER_ID, True)
        sid = await self.db.create_submission(USER_ID, "audio", "a")
        job = Job(sid, USER_ID, "audio", "a", message=event.message)
        with mock.patch.object(self.bot, "_process_media", new_callable=mock.AsyncMock) as process:
            await self.bot._run_job(job)
        process.assert_not_awaited()
        self.assertEqual((await self.db.submission(sid)).status, "rejected")

    async def test_exception_after_raw_delivery_preserves_billed_usage(self):
        event = self.media_event()
        with mock.patch.object(self.bot, "_deliver_booklet", side_effect=RuntimeError("booklet failed")):
            sid = await self.run_media_job(event, transcribe=self._raw())
        self.assertEqual((await self.db.submission(sid)).status, "partial")
        self.assertEqual(await self.db.available_seconds(USER_ID), 3500)
        self.assertEqual((await self.db.stats())["seconds_transcribed"], 100)

    async def test_reopened_quote_uses_snapshot_even_when_plan_disabled(self):
        self.bot.settings = replace(self.settings, payment_card_number="6037991234567890")
        pid = await self.db.create_payment(USER_ID, "paid_5h_30d")
        await self.db._write("UPDATE plans SET hours = 10, price = 75000, active = 0")
        event = FakeCallbackEvent(USER_ID, b"plan:paid_5h_30d")
        await self.bot._handle_callback(event)
        text, buttons = event.responses[-1]
        self.assertIn(format_toman(50000), text)
        self.assertNotIn(format_toman(75000), text)
        self.assertIn(f"payc:{pid}".encode(), menu_payloads(buttons))
        await self.db.review_payment(pid, True, 1)
        self.assertEqual(await self.db.available_seconds(USER_ID), 18000)

    async def test_resume_and_cancel_from_account_after_restart(self):
        self.bot.settings = replace(self.settings, payment_card_number="6037991234567890")
        pid = await self.db.create_payment(USER_ID, "paid_5h_30d")
        event = FakeCallbackEvent(USER_ID, b"account")
        await self.bot._handle_callback(event)
        self.assertIn(f"payopen:{pid}".encode(), menu_payloads(event.edits[-1][1]))
        event = FakeCallbackEvent(USER_ID, f"payc:{pid}".encode())
        await self.bot._handle_callback(event)
        self.assertEqual((await self.db.payment(pid))["status"], "cancelled")

    async def test_payment_callbacks_cannot_access_other_users_request(self):
        await self.db.ensure_user(222)
        pid = await self.db.create_payment(222, "paid_5h_30d")
        for action in ("payopen", "payc", "payr"):
            event = FakeCallbackEvent(USER_ID, f"{action}:{pid}".encode())
            await self.bot._handle_callback(event)
        self.assertEqual((await self.db.payment(pid))["status"], "pending")
        self.assertNotIn(USER_ID, self.bot._pending_payments)

    async def test_invalid_payment_callback_does_not_crash(self):
        for action in ("payopen", "payc", "payr"):
            await self.bot._handle_callback(FakeCallbackEvent(USER_ID, f"{action}:invalid".encode()))

    async def test_truncated_raw_fallback_does_not_charge(self):
        event = self.media_event()
        event.client.fail_file_delivery = True
        with mock.patch("gamas_bot.bot.render_pages", return_value=["page one", "page two"]), \
                mock.patch("gamas_bot.bot.RAW_FALLBACK_MAX_PAGES", 1):
            sid = await self.run_media_job(event, transcribe=self._raw())
        self.assertEqual((await self.db.submission(sid)).status, "failed")
        self.assertEqual(await self.db.available_seconds(USER_ID), 3600)
        self.assertEqual((await self.db.stats())["seconds_transcribed"], 0)

    async def test_cancel_button_available_without_payment_card(self):
        pid = await self.db.create_payment(USER_ID, "paid_5h_30d")
        self.bot.settings = replace(self.settings, payment_card_number="")
        event = FakeCallbackEvent(USER_ID, b"account")
        await self.bot._handle_callback(event)
        self.assertIn(f"payc:{pid}".encode(), menu_payloads(event.edits[-1][1]))
        await self.bot._handle_callback(FakeCallbackEvent(USER_ID, f"payc:{pid}".encode()))
        self.assertEqual((await self.db.payment(pid))["status"], "cancelled")

    async def test_submitted_receipt_screen_does_not_invite_duplicate_payment(self):
        self.bot.settings = replace(self.settings, payment_card_number="6037991234567890")
        pid = await self.db.create_payment(USER_ID, "paid_5h_30d")
        await self.db.set_payment_receipt(pid, "receipt.jpg")
        event = FakeCallbackEvent(USER_ID, f"payopen:{pid}".encode())
        await self.bot._handle_callback(event)
        text, buttons = event.responses[-1]
        self.assertIn("نیازی به واریز مجدد نیست", text)
        self.assertNotIn(f"payc:{pid}".encode(), menu_payloads(buttons))
        self.assertNotIn(f"payr:{pid}".encode(), menu_payloads(buttons))


class LegacyAllocationMigrationTests(DatabaseTestCase):
    async def legacy_database(self):
        """Construct a genuine pre-allocation database, not a new-schema mock."""
        from gamas_bot.db import MIGRATIONS_DIR
        await self.db.close()
        self.path.unlink()
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))[:3]:
            conn.executescript(path.read_text())
            conn.execute("INSERT INTO schema_migrations VALUES (?, '2026-01-01')", (path.name,))
        conn.execute("INSERT INTO users(telegram_id, joined_at) VALUES (100, '2026-01-01')")
        conn.commit()
        return conn

    async def test_legacy_usage_is_allocated_once_and_renewal_is_not_reduced(self):
        conn = await self.legacy_database()
        conn.executescript("""
            INSERT INTO ledger(user_id, seconds, kind, expires_at, created_at)
                VALUES(100, 3600, 'credit', '2026-01-02T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            INSERT INTO ledger(user_id, seconds, kind, created_at)
                VALUES(100, 1800, 'debit', '2026-01-01T12:00:00+00:00');
            INSERT INTO ledger(user_id, seconds, kind, created_at)
                VALUES(100, 3600, 'credit', '2026-01-03T00:00:00+00:00');
        """)
        conn.close()
        await self.db.connect()
        self.assertEqual(await self.db.available_seconds(100), 3600)
        await self.db.close()
        await self.db.connect()
        self.assertEqual(await self.db.available_seconds(100), 3600)
        self.assertEqual((await self.db.db.execute_fetchall("SELECT SUM(seconds) FROM debit_allocations"))[0][0], 1800)

    async def test_legacy_active_hold_is_allocated_and_recovered(self):
        conn = await self.legacy_database()
        conn.executescript("""
            INSERT INTO ledger(user_id, seconds, kind, created_at)
                VALUES(100, 1000, 'credit', '2026-01-01T00:00:00+00:00');
            INSERT INTO submissions(user_id, kind, status, created_at)
                VALUES(100, 'audio', 'processing', '2026-01-01T00:00:00+00:00');
            INSERT INTO holds(submission_id, user_id, seconds, created_at)
                VALUES(1, 100, 100, '2026-01-01T01:00:00+00:00');
        """)
        conn.close()
        await self.db.connect()
        self.assertEqual(await self.db.available_seconds(100), 900)
        await self.db.recover_interrupted()
        self.assertEqual(await self.db.available_seconds(100), 1000)

    async def test_legacy_settlement_after_expiry_uses_reservation_time(self):
        conn = await self.legacy_database()
        conn.executescript("""
            INSERT INTO ledger(user_id, seconds, kind, expires_at, created_at)
                VALUES(100, 1000, 'credit', '2026-01-02T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            INSERT INTO submissions(user_id, kind, status, duration_seconds, created_at)
                VALUES(100, 'audio', 'processing', 100, '2026-01-01T00:00:00+00:00');
            INSERT INTO holds(submission_id, user_id, seconds, created_at, released_at, settled)
                VALUES(1, 100, 100, '2026-01-01T01:00:00+00:00', '2026-01-03T00:00:00+00:00', 1);
            INSERT INTO ledger(user_id, seconds, kind, note, created_at)
                VALUES(100, 100, 'debit', 'submission:1', '2026-01-03T00:00:00+00:00');
            INSERT INTO ledger(user_id, seconds, kind, created_at)
                VALUES(100, 1000, 'credit', '2026-01-03T00:00:00+00:00');
        """)
        conn.close()
        await self.db.connect()
        self.assertEqual(await self.db.available_seconds(100), 1000)
        self.assertEqual((await self.db.db.execute_fetchall("SELECT credit_id, seconds FROM debit_allocations"))[0][:], (1, 100))
        await self.db.recover_interrupted()
        self.assertEqual((await self.db.submission(1)).status, "partial")
        self.assertEqual((await self.db.stats())["seconds_transcribed"], 100)

    async def test_unfunded_legacy_debit_does_not_consume_later_same_second_credit(self):
        conn = await self.legacy_database()
        conn.executescript("""
            INSERT INTO ledger(user_id, seconds, kind, created_at)
                VALUES(100, 100, 'debit', '2026-01-01T00:00:00+00:00');
            INSERT INTO ledger(user_id, seconds, kind, created_at)
                VALUES(100, 1000, 'credit', '2026-01-01T00:00:00+00:00');
        """)
        conn.close()
        with self.assertLogs("gamas_bot.db", level="WARNING"):
            await self.db.connect()
        self.assertEqual(await self.db.available_seconds(100), 1000)

    async def test_backfill_failure_rolls_back_and_is_retryable(self):
        conn = await self.legacy_database()
        conn.executescript("""
            INSERT INTO ledger(user_id, seconds, kind, created_at)
                VALUES(100, 1000, 'credit', '2026-01-01T00:00:00+00:00');
            INSERT INTO ledger(user_id, seconds, kind, created_at)
                VALUES(100, 100, 'debit', '2026-01-01T01:00:00+00:00');
        """)
        conn.close()
        # Fail after allocation but before the durable completion marker.
        original = self.db._credit_lots
        async def fail_after_read(*args, **kwargs):
            result = await original(*args, **kwargs)
            await self.db.db.execute("CREATE TEMP TRIGGER fail_marker BEFORE INSERT ON billing_migrations BEGIN SELECT RAISE(ABORT, 'test'); END")
            return result
        with mock.patch.object(self.db, "_credit_lots", side_effect=fail_after_read):
            with self.assertRaises(sqlite3.IntegrityError):
                await self.db.connect()
        self.assertEqual((await self.db.db.execute_fetchall("SELECT COUNT(*) FROM debit_allocations"))[0][0], 0)
        await self.db.close()
        await self.db.connect()
        self.assertEqual(await self.db.available_seconds(100), 900)
