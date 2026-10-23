"""Admin reporting: accuracy against authoritative rows, safety and cost.

The dashboard answers operational questions, so every figure here is checked
against rows this test inserted itself with known timestamps — never against a
recomputed copy of the same SQL. The suite also pins the properties the screens
rely on:

* a window boundary is a UTC comparison on ``created_at``;
* a metric with no samples is ``None``/"not recorded", not zero;
* rows written before migration 009 (no ``started_at``) are counted in volume
  but excluded from timing, instead of silently skewing it;
* provider usage survives the transcript-retention sweep;
* the report costs a bounded, small number of statements (no N+1);
* only administrators can open any of the screens.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from gamas_bot.bot import GamasBot, classify_failure
from gamas_bot.db import MIGRATIONS_DIR, Database
from gamas_bot.jobs import Job, JobQueue
from gamas_bot.media import MediaToolError
from gamas_bot.office import OfficeError
from gamas_bot.stt import STTError
from gamas_bot.telegram_text import TELEGRAM_TEXT_LIMIT
from tests.support import FakeCallbackEvent, FakeTelegramClient, make_settings
from tests.test_bot import menu_payloads

USER_ID = 555
ADMIN_ID = 1

ISO = "%Y-%m-%dT%H:%M:%S+00:00"


def iso_at(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime(ISO)


class ReportingTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "report.db"
        self.db = Database(self.path)
        await self.db.connect()
        await self.db.ensure_user(USER_ID)
        await self.db.sync_plans([
            {"code": "paid_5h_30d", "hours": 5, "price": 50000, "validity_days": 30},
        ])

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def add_submission(
        self,
        *,
        user_id: int = USER_ID,
        status: str = "done",
        kind: str = "audio",
        created: timedelta = timedelta(minutes=-30),
        started: timedelta | None = timedelta(minutes=-29),
        finished: timedelta | None = timedelta(minutes=-25),
        error: str | None = None,
        error_kind: str | None = None,
        stt_engine: str | None = None,
        duration_seconds: float | None = 600.0,
    ) -> int:
        """Insert one row with explicit timestamps (authoritative, not faked)."""
        await self.db._write(
            "INSERT INTO submissions(user_id, kind, filename, status, error, "
            "duration_seconds, created_at, started_at, finished_at, error_kind, stt_engine) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, kind, "f.wav", status, error, duration_seconds,
                iso_at(created),
                iso_at(started) if started is not None else None,
                iso_at(finished) if finished is not None else None,
                error_kind, stt_engine,
            ),
        )
        rows = await self.db.read_db.execute_fetchall(
            "SELECT MAX(submission_id) FROM submissions"
        )
        return int(rows[0][0])

    async def report(self, period_start: str | None = None) -> dict:
        return await self.db.admin_report(period_start=period_start)

    def seven_days_ago(self) -> str:
        return iso_at(timedelta(days=-7))


class PeriodWindowTests(ReportingTestCase):
    async def test_window_uses_utc_created_at_boundaries(self):
        await self.add_submission(created=timedelta(days=-10), finished=timedelta(days=-10))
        await self.add_submission(created=timedelta(days=-3), finished=timedelta(days=-3))
        await self.add_submission(created=timedelta(minutes=-5), finished=timedelta(minutes=-4))

        lifetime = await self.report(None)
        self.assertEqual(sum(lifetime["processing"]["by_status"].values()), 3)

        week = await self.report(self.seven_days_ago())
        self.assertEqual(week["processing"]["by_status"], {"done": 2})

        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).strftime(ISO)
        today = await self.report(today_start)
        self.assertEqual(sum(today["processing"]["by_status"].values()), 1)

    async def test_period_start_that_matches_no_rows_reports_zero_not_none(self):
        await self.add_submission(created=timedelta(days=-30))
        report = await self.report(self.seven_days_ago())
        self.assertEqual(report["processing"]["by_status"], {})
        self.assertEqual(report["processing"]["delivered"], 0)
        self.assertIsNone(report["processing"]["success_rate"])
        self.assertEqual(report["processing"]["end_to_end"]["count"], 0)
        self.assertIsNone(report["processing"]["end_to_end"]["avg_seconds"])

    async def test_new_users_and_active_users_use_the_same_window(self):
        await self.db._write(
            "INSERT INTO users(telegram_id, joined_at) VALUES (?, ?)",
            (777, iso_at(timedelta(days=-40))),
        )
        await self.add_submission(user_id=777, created=timedelta(days=-30))
        await self.add_submission(user_id=USER_ID, created=timedelta(days=-1))

        lifetime = await self.report(None)
        self.assertEqual(lifetime["users"]["total"], 2)
        self.assertEqual(lifetime["users"]["active"], 2)

        week = await self.report(self.seven_days_ago())
        self.assertEqual(week["users"]["new"], 1)  # only USER_ID joined recently
        self.assertEqual(week["users"]["active"], 1)
        self.assertEqual(week["users"]["delivered"], 1)


class ProcessingReportTests(ReportingTestCase):
    async def test_success_failure_split_and_rate(self):
        await self.add_submission(status="done")
        await self.add_submission(status="partial", error="booklet failed", error_kind="notes")
        await self.add_submission(status="failed", error="stt down", error_kind="stt")
        await self.add_submission(status="rejected", error="queue full", error_kind="capacity")

        report = await self.report(None)
        processing = report["processing"]
        self.assertEqual(processing["delivered"], 2)
        self.assertEqual(processing["undelivered"], 2)
        self.assertEqual(processing["terminal"], 4)
        self.assertAlmostEqual(processing["success_rate"], 0.5)

    async def test_pending_work_is_counted_but_not_judged(self):
        await self.add_submission(status="processing", finished=None)
        report = await self.report(None)
        self.assertEqual(report["processing"]["by_status"], {"processing": 1})
        self.assertEqual(report["processing"]["terminal"], 0)
        self.assertIsNone(report["processing"]["success_rate"])

    async def test_durations_use_started_and_finished_timestamps(self):
        # Queue waits 10/20/30/40 s; processing takes exactly 60 s each.
        for wait in (10, 20, 30, 40):
            await self.add_submission(
                created=timedelta(hours=-2),
                started=timedelta(hours=-2, seconds=wait),
                finished=timedelta(hours=-2, seconds=wait + 60),
            )
        report = await self.report(None)
        queue_wait = report["processing"]["queue_wait"]
        processing = report["processing"]["processing_time"]

        self.assertEqual(queue_wait["count"], 4)
        self.assertAlmostEqual(queue_wait["avg_seconds"], 25.0, places=3)
        self.assertAlmostEqual(queue_wait["max_seconds"], 40.0, places=3)
        # Lower median for an even sample, matching the SQL rank definition.
        self.assertAlmostEqual(queue_wait["median_seconds"], 20.0, places=3)
        self.assertAlmostEqual(queue_wait["p95_seconds"], 40.0, places=3)

        self.assertEqual(processing["count"], 4)
        self.assertAlmostEqual(processing["median_seconds"], 60.0, places=3)

    async def test_rows_without_started_at_are_excluded_from_timing_not_zeroed(self):
        # A row written by pre-009 code: created/finished only.
        await self.add_submission(started=None, created=timedelta(hours=-3),
                                  finished=timedelta(hours=-2))
        await self.add_submission(created=timedelta(hours=-3),
                                  started=timedelta(hours=-3, seconds=5),
                                  finished=timedelta(hours=-3, seconds=65))
        report = await self.report(None)
        self.assertEqual(sum(report["processing"]["by_status"].values()), 2)
        self.assertEqual(report["processing"]["queue_wait"]["count"], 1)
        self.assertEqual(report["processing"]["processing_time"]["count"], 1)
        # End-to-end still has both rows; it needs no started_at.
        self.assertEqual(report["processing"]["end_to_end"]["count"], 2)

    async def test_error_categories_group_failed_rows_and_keep_unknown_values(self):
        await self.add_submission(status="failed", error="a", error_kind="stt")
        await self.add_submission(status="failed", error="b", error_kind="stt")
        await self.add_submission(status="failed", error="c", error_kind="delivery")
        await self.add_submission(status="failed", error="d")  # unclassified
        await self.add_submission(status="rejected", error="e", error_kind="capacity")

        report = await self.report(None)
        errors = {item["kind"]: item["count"] for item in report["processing"]["errors"]}
        self.assertEqual(errors["stt"], 2)
        self.assertEqual(errors["delivery"], 1)
        self.assertEqual(errors["unclassified"], 1)
        self.assertEqual(errors["capacity"], 1)
        # Partial deliveries are not failures; they are a separate status count.
        await self.add_submission(status="partial", error="x", error_kind="notes")
        report = await self.report(None)
        errors = {item["kind"]: item["count"] for item in report["processing"]["errors"]}
        self.assertNotIn("notes", errors)

    async def test_day_series_is_bounded_and_sorted(self):
        for days in range(12):
            await self.add_submission(created=timedelta(days=-days),
                                      finished=timedelta(days=-days))
        report = await self.report(None)
        series = report["processing"]["series"]
        self.assertLessEqual(len(series), Database.REPORT_SERIES_MAX_DAYS)
        self.assertEqual([row["day"] for row in series], sorted(row["day"] for row in series))

    async def test_kind_breakdown_separates_free_office_from_billable_work(self):
        await self.add_submission(kind="audio")
        await self.add_submission(kind="office")
        report = await self.report(None)
        self.assertEqual(report["processing"]["by_kind"], {"audio": 1, "office": 1})


class TimingOptOutTests(ReportingTestCase):
    """Screens that do not display timings must not pay for them."""

    async def test_unrequested_timings_are_none_not_zero(self):
        await self.add_submission()
        report = await self.db.admin_report(period_start=None, with_timings=False)
        self.assertIsNone(report["processing"]["queue_wait"])
        self.assertIsNone(report["processing"]["processing_time"])
        self.assertIsNone(report["processing"]["end_to_end"])
        # Every other figure is still computed from the same rows.
        self.assertEqual(report["processing"]["delivered"], 1)
        self.assertEqual(report["users"]["total"], 1)

    async def test_opt_out_issues_fewer_statements(self):
        await self.add_submission()
        connection = self.db.read_db
        original = connection.execute_fetchall
        counts: list[int] = []
        for with_timings in (True, False):
            statements: list[str] = []

            async def counting(sql, *args, _bucket=statements, **kwargs):
                _bucket.append(sql)
                return await original(sql, *args, **kwargs)

            with mock.patch.object(connection, "execute_fetchall", counting):
                await self.db.admin_report(period_start=None, with_timings=with_timings)
            counts.append(len(statements))
        self.assertEqual(counts[0] - counts[1], 3)


class RestartRecoveryTests(ReportingTestCase):
    async def test_interrupted_work_is_categorised_as_cancelled(self):
        await self.add_submission(status="processing", finished=None)
        recovered = await self.db.recover_interrupted()
        self.assertEqual(recovered, 1)
        report = await self.report(None)
        errors = {item["kind"]: item["count"] for item in report["processing"]["errors"]}
        self.assertEqual(errors.get("cancelled"), 1)
        self.assertNotIn("unclassified", errors)


class SttUsageTests(ReportingTestCase):
    async def test_engine_usage_survives_transcript_retention(self):
        first = await self.add_submission()
        second = await self.add_submission()
        await self.db.save_transcription(first, "speechmatics", "متن", "")
        await self.db.save_transcription(second, "deepgram", "متن", "")

        engines = {item["engine"]: item["count"] for item in
                   await self.db.admin_stt_engines(period_start=None)}
        self.assertEqual(engines, {"speechmatics": 1, "deepgram": 1})

        # Retention deletes the transcript rows; provider usage must remain.
        removed = await self.db.expire_transcriptions(older_than_days=0, batch=10)
        self.assertEqual(removed, 0)  # status is done, but rows are too fresh
        await self.db._write("UPDATE transcriptions SET created_at = ?", (iso_at(timedelta(days=-400)),))
        removed = await self.db.expire_transcriptions(older_than_days=90, batch=10)
        self.assertEqual(removed, 2)
        engines = {item["engine"]: item["count"] for item in
                   await self.db.admin_stt_engines(period_start=None)}
        self.assertEqual(engines, {"speechmatics": 1, "deepgram": 1})

    async def test_rows_without_an_engine_are_reported_as_unrecorded(self):
        await self.add_submission()
        engines = await self.db.admin_stt_engines(period_start=None)
        self.assertEqual(engines, [{"engine": "unrecorded", "count": 1}])


class PaymentReportTests(ReportingTestCase):
    async def test_revenue_counts_only_approved_orders_at_purchase_price(self):
        # Approved: 2 orders, 50,000 and 40,000 (an older price snapshot).
        for price, status in ((50_000, "approved"), (40_000, "approved")):
            await self.db._write(
                "INSERT INTO payments(user_id, plan_code, status, receipt_path, created_at, "
                "purchased_seconds, purchased_price, purchased_validity_days) "
                "VALUES (?, 'paid_5h_30d', ?, 'r.jpg', ?, 18000, ?, 30)",
                (USER_ID, status, iso_at(timedelta(days=-1)), price),
            )
        # Pending review and rejected must never be counted as revenue.
        for status in ("pending_review", "rejected"):
            await self.db._write(
                "INSERT INTO payments(user_id, plan_code, status, receipt_path, created_at, "
                "purchased_seconds, purchased_price, purchased_validity_days) "
                "VALUES (?, 'paid_5h_30d', ?, 'r.jpg', ?, 18000, 90000, 30)",
                (USER_ID, status, iso_at(timedelta(days=-1))),
            )
        # Outside the window: approved but 60 days old.
        await self.db._write(
            "INSERT INTO payments(user_id, plan_code, status, receipt_path, created_at, "
            "purchased_seconds, purchased_price, purchased_validity_days) "
            "VALUES (?, 'paid_5h_30d', 'approved', 'r.jpg', ?, 18000, 70000, 30)",
            (USER_ID, iso_at(timedelta(days=-60))),
        )

        week = await self.report(self.seven_days_ago())
        by_status = week["payments"]["by_status"]
        self.assertEqual(by_status["approved"], {"count": 2, "toman": 90_000})
        self.assertEqual(by_status["rejected"]["count"], 1)
        self.assertEqual(week["payments"]["pending_review"], 1)

        lifetime = await self.report(None)
        self.assertEqual(lifetime["payments"]["by_status"]["approved"]["toman"], 160_000)

    async def test_expiring_counts_only_dated_lots_inside_the_window(self):
        now = datetime.now(timezone.utc)
        await self.db._write(
            "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
            "VALUES (?, 3600, 'credit', 'soon', ?, ?)",
            (USER_ID, (now + timedelta(days=2)).strftime(ISO), iso_at(timedelta(days=-30))),
        )
        await self.db._write(
            "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
            "VALUES (?, 1800, 'credit', 'later', ?, ?)",
            (USER_ID, (now + timedelta(days=30)).strftime(ISO), iso_at(timedelta(days=-1))),
        )
        await self.db._write(
            "INSERT INTO ledger(user_id, seconds, kind, note, expires_at, created_at) "
            "VALUES (?, 7200, 'credit', 'never', NULL, ?)",
            (USER_ID, iso_at(timedelta(days=-1))),
        )
        report = await self.report(None)
        expiring = report["payments"]["expiring"]
        self.assertEqual(expiring["count"], 1)
        self.assertEqual(expiring["seconds"], 3600)

    async def test_health_reports_last_success_or_none(self):
        health = await self.db.admin_health()
        self.assertIsNone(health["last_success"])
        await self.add_submission(status="failed", created=timedelta(hours=-1),
                                  finished=timedelta(hours=-1))
        health = await self.db.admin_health()
        self.assertIsNone(health["last_success"])

        await self.add_submission(status="done", created=timedelta(minutes=-10),
                                  finished=timedelta(minutes=-5))
        health = await self.db.admin_health()
        self.assertTrue(health["last_success"])
        self.assertGreater(health["db_bytes"], 0)


class QueryBudgetTests(ReportingTestCase):
    async def test_full_report_issues_a_small_bounded_number_of_statements(self):
        for _ in range(20):
            await self.add_submission()
        connection = self.db.read_db
        original = connection.execute_fetchall
        statements: list[str] = []

        async def counting(sql, *args, **kwargs):
            statements.append(sql)
            return await original(sql, *args, **kwargs)

        with mock.patch.object(connection, "execute_fetchall", counting):
            await self.report(self.seven_days_ago())
        # 14 figures + health; anything approaching "one query per row" fails.
        self.assertLessEqual(len(statements), 16)
        self.assertGreaterEqual(len(statements), 8)

    async def test_query_count_does_not_grow_with_rows(self):
        connection = self.db.read_db
        original = connection.execute_fetchall
        counts = []
        for rows in (1, 40):
            for _ in range(rows):
                await self.add_submission()
            statements: list[str] = []

            async def counting(sql, *args, _bucket=statements, **kwargs):
                _bucket.append(sql)
                return await original(sql, *args, **kwargs)

            with mock.patch.object(connection, "execute_fetchall", counting):
                await self.report(None)
            counts.append(len(statements))
            await self.db._write("DELETE FROM submissions")
        self.assertEqual(counts[0], counts[1])


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_migration_009_preserves_rows_and_leaves_old_ones_unrecorded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration.name >= "009":
                    continue
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, '2026-01-01')",
                    (migration.name,),
                )
            connection.execute("INSERT INTO users(telegram_id, joined_at) VALUES (100, ?)",
                               (iso_at(timedelta(days=-1)),))
            connection.execute(
                "INSERT INTO submissions(user_id, kind, filename, status, created_at, finished_at) "
                "VALUES (100, 'audio', 'old.wav', 'done', ?, ?)",
                (iso_at(timedelta(days=-2)), iso_at(timedelta(days=-2))),
            )
            connection.commit()
            connection.close()

            db = Database(path)
            await db.connect()
            try:
                report = await db.admin_report(period_start=None)
                self.assertEqual(report["processing"]["delivered"], 1)
                # Pre-009 timing was never recorded: no samples, not a zero.
                self.assertEqual(report["processing"]["queue_wait"]["count"], 0)
                self.assertIsNone(report["processing"]["queue_wait"]["avg_seconds"])
                columns = {
                    row[1] for row in await db.read_db.execute_fetchall(
                        "PRAGMA table_info(submissions)"
                    )
                }
                self.assertTrue({"started_at", "error_kind", "stt_engine"} <= columns)
                indexes = {
                    row[0] for row in await db.read_db.execute_fetchall(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertIn("idx_submissions_created", indexes)
                self.assertIn("idx_ledger_kind_expiry", indexes)
            finally:
                await db.close()


class StatusStampTests(ReportingTestCase):
    async def test_processing_stamps_started_at_once(self):
        submission = await self.add_submission(status="pending", started=None, finished=None)
        await self.db.set_submission_status(submission, "processing")
        first = (await self.db.submission(submission)).created_at  # sanity: row still readable
        self.assertTrue(first)
        rows = await self.db.read_db.execute_fetchall(
            "SELECT started_at FROM submissions WHERE submission_id = ?", (submission,)
        )
        stamped = rows[0][0]
        self.assertTrue(stamped)
        await self.db.set_submission_status(submission, "processing")
        rows = await self.db.read_db.execute_fetchall(
            "SELECT started_at FROM submissions WHERE submission_id = ?", (submission,)
        )
        self.assertEqual(rows[0][0], stamped)

    async def test_error_kind_is_written_once_and_never_overwritten(self):
        submission = await self.add_submission(status="processing", error_kind=None)
        await self.db.set_submission_status(submission, "failed", error="x", error_kind="stt")
        await self.db.set_submission_status(submission, "failed", error="y", error_kind="delivery")
        rows = await self.db.read_db.execute_fetchall(
            "SELECT error_kind FROM submissions WHERE submission_id = ?", (submission,)
        )
        self.assertEqual(rows[0][0], "stt")

    async def test_failed_after_delivery_becomes_partial_with_its_category(self):
        submission = await self.add_submission(status="done")
        await self.db._write(
            "UPDATE submissions SET delivered_at = ? WHERE submission_id = ?",
            (iso_at(timedelta(minutes=-20)), submission),
        )
        await self.db.set_submission_status(
            submission, "failed", error="booklet", error_kind="notes"
        )
        row = await self.db.submission(submission)
        self.assertEqual(row.status, "partial")
        rows = await self.db.read_db.execute_fetchall(
            "SELECT error_kind FROM submissions WHERE submission_id = ?", (submission,)
        )
        self.assertEqual(rows[0][0], "notes")

    async def test_save_transcription_records_engine_on_the_submission(self):
        submission = await self.add_submission(status="processing")
        await self.db.save_transcription(submission, "deepgram", "متن", "{}")
        rows = await self.db.read_db.execute_fetchall(
            "SELECT stt_engine FROM submissions WHERE submission_id = ?", (submission,)
        )
        self.assertEqual(rows[0][0], "deepgram")
        # A later save (progressive notes pass) must not erase the engine.
        await self.db.save_transcription(submission, "", "متن", "{}")
        rows = await self.db.read_db.execute_fetchall(
            "SELECT stt_engine FROM submissions WHERE submission_id = ?", (submission,)
        )
        self.assertEqual(rows[0][0], "deepgram")


class FailureClassificationTests(unittest.TestCase):
    def test_explicit_types_win_over_the_stage(self):
        self.assertEqual(classify_failure("uploading", STTError("x")), "stt")
        self.assertEqual(classify_failure("transcribing", MediaToolError("x")), "media")
        self.assertEqual(classify_failure("transcribing", OfficeError("x")), "input")
        self.assertEqual(classify_failure("downloading", sqlite3.OperationalError("x")), "database")

    def test_stage_decides_for_generic_failures(self):
        self.assertEqual(classify_failure("downloading", RuntimeError("x")), "download")
        self.assertEqual(classify_failure("transcribing", RuntimeError("x")), "stt")
        self.assertEqual(classify_failure("generating_notes", RuntimeError("x")), "notes")
        self.assertEqual(classify_failure("queued", RuntimeError("x")), "internal")

    def test_user_visible_errors_are_input_problems(self):
        self.assertEqual(classify_failure("transcribing", ValueError("bad file")), "input")


class ProviderMetricTests(unittest.IsolatedAsyncioTestCase):
    """The health panel's timeout/rate-limit counts come from the error kind."""

    async def test_transient_kind_drives_the_metric_bucket(self):
        from gamas_bot import stt as stt_module
        from gamas_bot.metrics import METRICS

        stt_module.reset_cooldowns()
        before = METRICS.snapshot()["providers"].get("stt", {}).get("bench", {})
        before = {
            key: int(before.get(key, 0) or 0)
            for key in ("timeouts", "server_errors", "rate_limits")
        }

        async def timed_out(session, audio_path, settings):
            # The wording must not matter: only the declared kind does.
            raise stt_module.STTTransientError("مهلت سرویس تمام شد", kind="timeout")

        spec = stt_module.ProviderSpec(
            slug="bench",
            label="Bench",
            configured=lambda settings: True,
            max_upload=1_000,
            run=timed_out,
        )
        with self.assertRaises(stt_module.STTTransientError):
            await stt_module._attempt_provider(
                spec, object(), Path("unused.wav"), make_settings()
            )
        after = METRICS.snapshot()["providers"]["stt"]["bench"]
        self.assertEqual(after["timeouts"] - before["timeouts"], 1)
        self.assertEqual(after["server_errors"] - before["server_errors"], 0)
        self.assertGreater(stt_module._cooldown_remaining("bench"), 0)
        stt_module.reset_cooldowns()

    async def test_unknown_kind_falls_back_to_server_error(self):
        from gamas_bot import stt as stt_module

        error = stt_module.STTTransientError("x", kind="not-a-bucket")
        self.assertEqual(error.kind, "server_error")


class AdminScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "bot.db",
            temp_dir=root / "tmp",
            admin_ids=frozenset({ADMIN_ID}),
            note_api_key="",
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(ADMIN_ID)
        await self.db.ensure_user(USER_ID)
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def press(self, data: bytes, user_id: int = ADMIN_ID) -> FakeCallbackEvent:
        return FakeCallbackEvent(self.client, data=data, chat_id=user_id)

    async def test_every_screen_renders_within_telegram_limits(self):
        for data in (
            b"adm:home",
            b"adm:ov:today",
            b"adm:ov:30d",
            b"adm:ov:all",
            b"adm:ovu:7d",
            b"adm:ovp:7d",
            b"adm:ovpay:7d",
            b"adm:ovs:7d",
        ):
            event = self.press(data)
            await self.bot._handle_callback(event)
            self.assertTrue(event.edits, data)
            text = event.edits[-1]["text"]
            self.assertLessEqual(len(text), TELEGRAM_TEXT_LIMIT, data)
            self.assertNotIn("None", text, data)

    async def test_every_screen_carries_the_window_selector_and_a_way_back(self):
        for section in (b"ov", b"ovu", b"ovp", b"ovpay", b"ovs"):
            event = self.press(b"adm:" + section + b":7d")
            await self.bot._handle_callback(event)
            payloads = menu_payloads(event.edits[-1]["buttons"])
            self.assertIn(b"adm:home", payloads)
            self.assertIn(b"adm:" + section + b":today", payloads)
            self.assertIn(b"adm:" + section + b":all", payloads)

    async def test_non_admin_cannot_open_any_report_screen(self):
        for data in (b"adm:home", b"adm:ovp:7d", b"adm:ovpay:7d", b"adm:ovs:7d", b"adm:ovu:all"):
            event = self.press(data, user_id=USER_ID)
            await self.bot._handle_callback(event)
            self.assertEqual(event.edits, [], data)
            self.assertTrue(event.answers, data)
            self.assertTrue(event.answers[-1][1], data)

    async def test_forged_window_is_refused_without_rendering(self):
        event = self.press(b"adm:ovp:90d")
        await self.bot._handle_callback(event)
        self.assertEqual(event.edits, [])
        self.assertTrue(event.answers[-1][1])
        self.assertIn("نامعتبر", event.answers[-1][0])

    async def test_unknown_section_is_refused(self):
        event = self.press(b"adm:ovx:7d")
        await self.bot._handle_callback(event)
        self.assertEqual(event.edits, [])
        self.assertTrue(event.answers[-1][1])

    async def test_pending_review_and_failures_appear_in_attention(self):
        await self.db.sync_plans([
            {"code": "paid_5h_30d", "hours": 5, "price": 50000, "validity_days": 30},
        ])
        await self.db._write(
            "INSERT INTO payments(user_id, plan_code, status, receipt_path, created_at, "
            "purchased_seconds, purchased_price, purchased_validity_days) "
            "VALUES (?, 'paid_5h_30d', 'pending_review', 'r.jpg', ?, 18000, 50000, 30)",
            (USER_ID, iso_at(timedelta(minutes=-30))),
        )
        await self.db._write(
            "INSERT INTO submissions(user_id, kind, status, created_at, finished_at, error_kind) "
            "VALUES (?, 'audio', 'failed', ?, ?, 'stt')",
            (USER_ID, iso_at(timedelta(minutes=-30)), iso_at(timedelta(minutes=-29))),
        )
        event = self.press(b"adm:home")
        await self.bot._handle_callback(event)
        text = event.edits[-1]["text"]
        self.assertIn("در انتظار بررسی رسید", text)
        self.assertIn("ناموفق در این بازه", text)
        self.assertIn("تبدیل گفتار به متن", text)

        processing = self.press(b"adm:ovp:7d")
        await self.bot._handle_callback(processing)
        self.assertIn("خطاهای بازه", processing.edits[-1]["text"])
        self.assertIn("تبدیل گفتار به متن", processing.edits[-1]["text"])

    async def test_processing_screen_reports_unrecorded_when_no_data(self):
        event = self.press(b"adm:ovp:7d")
        await self.bot._handle_callback(event)
        text = event.edits[-1]["text"]
        self.assertIn("داده‌ای ثبت نشده", text)
        self.assertNotIn("نرخ موفقیت: ۰٪", text)

    async def test_queue_and_activity_sections_are_labelled_as_such(self):
        event = self.press(b"adm:ovs:7d")
        await self.bot._handle_callback(event)
        text = event.edits[-1]["text"]
        self.assertIn("سلامت سیستم", text)
        self.assertIn("خطاهای پایگاه‌داده", text)
        self.assertIn("آخرین پردازش موفق", text)


class QueueHealthTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard reports live queue state, not a cached copy of it."""

    async def test_job_queue_exposes_pending_and_active_counts(self):
        async def worker(job: Job) -> None:  # pragma: no cover - never drained
            return None

        queue = JobQueue(max_queued=4, max_active=1, worker=worker)
        self.assertEqual(queue.pending, 0)
        self.assertEqual(queue.active_count, 0)
        queue.submit(Job(submission_id=1, user_id=1, kind="audio", filename="a.wav"))
        self.assertEqual(queue.pending, 1)
        await queue.shutdown()
