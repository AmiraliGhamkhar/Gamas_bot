"""Regression tests for the audit hardening pass.

Every case here failed (or would have failed) before the corresponding fix:
blank rendered pages counted as a lost delivery, an unbounded per-user queue,
an unmetered Free Office loop, dangling receipt references, cooldown skips
reported as transcription failures, a hedged race that swallowed unexpected
exceptions, a renderer that raised on a wide table row, ignored tariff
overrides, and a tip cap that reordered the model's advice.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from gamas_bot import config, llm, stt
from gamas_bot.bot import GamasBot
from gamas_bot.db import Database, TooManyActiveJobsError
from gamas_bot.docx_export import DocumentMeta, build_notes_docx
from gamas_bot.jobs import Job
from gamas_bot.notes import (
    MAX_PRACTICAL_TIPS,
    NoteSection,
    NoteTable,
    PracticalTip,
    StructuredNotes,
    gate_notes,
)
from tests.support import FakeJobEvent, FakeTelegramClient, make_settings


class _BotHarness(unittest.IsolatedAsyncioTestCase):
    """A bot over a real temporary database, with Telegram faked."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "hardening.db",
            temp_dir=root / "tmp",
            receipt_dir=root / "receipts",
            **{"note_api_key": "", "note_api_base_url": ""},
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.settings.receipt_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.user_id = 909
        await self.db.ensure_user(self.user_id)
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db

    async def asyncTearDown(self) -> None:
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def wire(self, *, fail_file_delivery: bool = False):
        event = FakeJobEvent()
        if fail_file_delivery:
            event.client = FakeTelegramClient(event, fail_file_delivery=True)
        self.bot.client = event.client
        return event

    async def delivery_job(self, event, reference: str = "GMS-1") -> Job:
        submission = await self.db.create_submission(self.user_id, "audio", "a.wav")
        raw = Path(self._tmp.name) / "raw.txt"
        raw.write_text("متن", encoding="utf-8")
        job = Job(
            submission_id=submission,
            user_id=self.user_id,
            kind="audio",
            filename="a.wav",
            message=event.message,
            is_admin=False,
        )
        job.meta["raw_path"] = raw
        return job


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class RawFallbackTests(_BotHarness):
    async def test_blank_rendered_pages_do_not_fail_a_complete_delivery(self):
        """A whitespace-only page is filtered, not evidence of a lost page."""
        event = self.wire(fail_file_delivery=True)
        job = await self.delivery_job(event)
        with mock.patch(
            "gamas_bot.bot.render_pages",
            return_value=["صفحهٔ یک", "   ", "صفحهٔ دو"],
        ):
            await GamasBot._deliver_raw(
                self.bot, job, "متن", job.meta["raw_path"], "GMS-1"
            )
        self.assertEqual(
            [record["text"] for record in event.client.texts],
            ["صفحهٔ یک", "صفحهٔ دو"],
        )

    async def test_a_page_past_the_cap_is_still_a_failed_delivery(self):
        event = self.wire(fail_file_delivery=True)
        job = await self.delivery_job(event, reference="GMS-2")
        with mock.patch(
            "gamas_bot.bot.render_pages",
            return_value=["صفحهٔ یک", "صفحهٔ دو"],
        ), mock.patch("gamas_bot.bot.RAW_FALLBACK_MAX_PAGES", 1):
            with self.assertRaises(RuntimeError):
                await GamasBot._deliver_raw(
                    self.bot, job, "متن", job.meta["raw_path"], "GMS-2"
                )


# ---------------------------------------------------------------------------
# Admission and Free Office metering
# ---------------------------------------------------------------------------


class AdmissionTests(_BotHarness):
    async def test_create_submission_enforces_the_per_user_active_limit(self):
        first = await self.db.create_submission(
            self.user_id, "audio", "a.wav", active_limit=1
        )
        with self.assertRaises(TooManyActiveJobsError):
            await self.db.create_submission(
                self.user_id, "audio", "b.wav", active_limit=1
            )
        await self.db.set_submission_status(first, "done")
        second = await self.db.create_submission(
            self.user_id, "audio", "c.wav", active_limit=1
        )
        self.assertGreater(second, first)

    async def test_intake_refuses_a_second_job_with_a_plain_message(self):
        await self.db.add_credit(self.user_id, 3600, note="test")
        await self.db.create_submission(self.user_id, "audio", "a.wav")
        event = self.wire()
        self.bot.settings = replace(self.bot.settings, max_active_jobs_per_user=1)
        await self.bot._accept_media(event, self.user_id, "audio", "b.wav", 60.0)
        self.assertTrue(
            any("در حال پردازش" in reply for reply in event.replies), event.replies
        )


class OfficeThrottleTests(_BotHarness):
    async def test_two_rapid_office_requests_are_throttled(self):
        from gamas_bot.office import OfficeSession

        event = self.wire()
        self.bot.settings = replace(
            self.bot.settings,
            office_min_interval_seconds=60.0,
            office_session_ttl_minutes=30,
        )
        first = OfficeSession(kind="word", parts=["متن کوتاه برای ساخت سند."])
        await self.bot._start_office_job(event, self.user_id, first)
        second = OfficeSession(kind="word", parts=["متن دوم برای ساخت سند."])
        await self.bot._start_office_job(event, self.user_id, second)
        self.assertTrue(
            any("صبر کنید" in reply for reply in event.replies), event.replies
        )


# ---------------------------------------------------------------------------
# Receipt bookkeeping
# ---------------------------------------------------------------------------


class ReceiptBookkeepingTests(_BotHarness):
    async def test_expired_receipt_is_unreferenced_and_audited(self):
        await self.db.sync_plans([
            {"code": "paid_5h_30d", "hours": 5, "price": 50000, "validity_days": 30},
        ])
        payment_id = await self.db.create_payment(self.user_id, "paid_5h_30d")
        path = str(self.settings.receipt_dir / "receipt-1-abc.jpg")
        Path(path).write_bytes(b"jpeg")
        self.assertTrue(await self.db.set_payment_receipt(payment_id, path))
        self.assertEqual(await self.db.forget_receipts([path]), 1)
        payment = await self.db.payment(payment_id)
        assert payment is not None
        self.assertIsNone(payment["receipt_path"])
        actions = [row["action"] for row in await self.db.recent_audit(10)]
        self.assertIn("receipt_expired", actions)

    async def test_retention_never_deletes_a_receipt_still_awaiting_review(self):
        await self.db.sync_plans([
            {"code": "paid_5h_30d", "hours": 5, "price": 50000, "validity_days": 30},
        ])
        payment_id = await self.db.create_payment(self.user_id, "paid_5h_30d")
        path = Path(self.settings.receipt_dir / "receipt-1-old.jpg")
        path.write_bytes(b"jpeg")
        os.utime(path, (1, 1))
        await self.db.set_payment_receipt(payment_id, str(path), user_id=self.user_id)
        await self.bot._cleanup_expired_receipts()
        self.assertTrue(path.exists())
        self.assertEqual((await self.db.payment(payment_id))["status"], "pending_review")

        await self.db.review_payment(payment_id, True, 1)
        await self.bot._cleanup_expired_receipts()
        self.assertFalse(path.exists())
        self.assertIsNone((await self.db.payment(payment_id))["receipt_path"])


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------


class SttSkipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        stt.reset_cooldowns()

    def tearDown(self) -> None:
        stt.reset_cooldowns()

    def test_largest_upload_is_the_biggest_configured_ceiling(self):
        settings = make_settings(
            stt_providers=("openai_compatible",),
            stt_openai_api_key="key",
            stt_openai_base_url="https://example.test/v1",
        )
        self.assertEqual(
            stt.largest_configured_upload(settings), settings.stt_openai_max_upload
        )
        both = make_settings(stt_providers=("openai_compatible", "deepgram"))
        self.assertEqual(stt.largest_configured_upload(both), config.DEEPGRAM_MAX_UPLOAD)

    async def test_a_cooldown_only_chain_is_reported_as_temporary(self):
        settings = make_settings(stt_providers=("speechmatics",))
        stt._set_cooldown("speechmatics", 60)
        audio = Path(tempfile.gettempdir()) / "gamas-hardening-audio.wav"
        audio.write_bytes(b"x")
        with self.assertRaises(stt.STTTransientError) as ctx:
            await stt.transcribe(audio, settings, session=object())
        self.assertIn("Speechmatics", str(ctx.exception))


# ---------------------------------------------------------------------------
# LLM hedging
# ---------------------------------------------------------------------------


class HedgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unexpected_exception_is_not_swallowed(self):
        async def boom():
            raise ValueError("local gateway returned nonsense")

        async def slow():
            await asyncio.sleep(0.05)
            return "never"

        with self.assertRaises(ValueError):
            await llm._hedge_two(boom, slow, 0.0)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class DocxRobustnessTests(unittest.TestCase):
    def test_a_row_wider_than_the_header_does_not_crash(self):
        section = NoteSection(
            heading="بخش",
            paragraphs=("متن",),
            table=NoteTable(("الف", "ب"), (("۱", "۲", "۳"),)),
        )
        payload = build_notes_docx(
            StructuredNotes(sections=(section,)), meta=DocumentMeta(reference="GMS-1")
        )
        self.assertTrue(payload.startswith(b"PK"))


# ---------------------------------------------------------------------------
# Configuration and note selection
# ---------------------------------------------------------------------------


class TariffOverrideTests(unittest.TestCase):
    def test_an_ignored_override_is_reported_at_startup(self):
        with mock.patch.dict(os.environ, {"PLAN_5_PRICE_TOMAN": "0"}, clear=False):
            settings = config.from_env(env_file="/nonexistent")
        problems = "\n".join(config.check(settings))
        self.assertIn("PLAN_5_PRICE_TOMAN=0", problems)


class ChunkCapTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_pathological_chunk_count_is_refused_instead_of_billed(self):
        from gamas_bot.notes import StructuringError, structure_transcript

        settings = make_settings(note_chunk_chars=200, note_context_tokens=200_000)
        document = "\n\n".join(f"جملهٔ شمارهٔ {index} دربارهٔ درس." for index in range(4000))
        with self.assertRaises(StructuringError):
            await structure_transcript(document, settings, session=object())


class TipSelectionTests(unittest.TestCase):
    def test_the_models_own_tip_order_survives_the_cap(self):
        source = (
            "متن منبع دربارهٔ مبحث اصلی. "
            "این توضیح طولانی‌تر دربارهٔ همان مبحث در متن منبع آمده است. "
        )
        short_basis = "متن منبع دربارهٔ مبحث اصلی"
        long_basis = "این توضیح طولانی‌تر دربارهٔ همان مبحث در متن منبع آمده است"
        tips = tuple(
            PracticalTip(f"نکتهٔ {index}", short_basis if index == 0 else long_basis, "کاربردی")
            for index in range(MAX_PRACTICAL_TIPS + 2)
        )
        notes = StructuredNotes(
            sections=(NoteSection("بخش", paragraphs=(source,)),),
            practical_tips=tips,
        )
        gated, _report = gate_notes(notes, source, strict=True)
        self.assertEqual(
            [tip.text for tip in gated.practical_tips],
            [f"نکتهٔ {index}" for index in range(MAX_PRACTICAL_TIPS)],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
