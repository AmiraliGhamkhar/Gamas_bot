"""Regression tests for workflow, recovery, and transfer edge cases."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from tests.support import FakeCallbackEvent, FakeTelegramClient, make_settings
from tests.test_free_uses import cards_payload, quiz_payload

OWNER = 9101
OTHER = 9102


class LearningChargeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "charges.sqlite3")
        await self.db.connect()
        await self.db.ensure_user(OWNER)
        await self.db.ensure_user(OTHER)
        await self.db.add_credit(OWNER, 2_000, note="initial", validity_days=30)
        self.item_id = await self.db.library_add(OWNER, "booklet", OWNER, 1, title="درس")

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_restart_refunds_only_unsaved_paid_attempts_exactly_once(self):
        await self.db.spend_seconds(OWNER, 300, "fc-pending:1", operation_id="pending")
        await self.db.spend_seconds(OWNER, 200, "quiz-pending:1", operation_id="saved")
        quiz_id = await self.db.quiz_create_session(
            OWNER, self.item_id,
            [{"question": "پرسش", "options": ["a", "b", "c", "d"],
              "correct_index": 0, "explanation": "پاسخ"}],
            paid_charge_id="saved",
        )
        self.assertEqual(await self.db.available_seconds(OWNER), 1_500)
        await self.db.close()
        await self.db.connect()
        self.assertEqual(await self.db.recover_pending_learning_charges(), 1)
        self.assertEqual(await self.db.recover_pending_learning_charges(), 0)
        self.assertEqual(await self.db.available_seconds(OWNER), 1_800)
        self.assertIsNotNone(await self.db.quiz_get_session(quiz_id, OWNER))
        rows = await self.db.db.execute_fetchall(
            "SELECT operation_id, status FROM learning_charges ORDER BY operation_id"
        )
        self.assertEqual([(r[0], r[1]) for r in rows],
                         [("pending", "refunded"), ("saved", "finalized")])

    async def test_concurrent_refund_and_recovery_are_exactly_once_and_owner_scoped(self):
        await self.db.spend_seconds(OWNER, 300, "fc-pending:1", operation_id="one")
        self.assertFalse(await self.db.refund_learning_charge(OTHER, "one"))
        await asyncio.gather(
            self.db.refund_learning_charge(OWNER, "one"),
            self.db.refund_learning_charge(OWNER, "one"),
            self.db.recover_pending_learning_charges(),
        )
        self.assertEqual(await self.db.available_seconds(OWNER), 2_000)
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE user_id = ? AND note LIKE 'refund:%'", (OWNER,)
        )
        self.assertEqual(rows[0][0], 1)

    async def test_artifact_and_paid_finalization_commit_or_rollback_together(self):
        await self.db.spend_seconds(OWNER, 300, "fc-pending:1", operation_id="deck")
        with self.assertRaises(KeyError):
            await self.db.deck_create_with_cards(
                OWNER, self.item_id, "بدون پاسخ", [{"front": "پرسش"}], paid_charge_id="deck"
            )
        self.assertEqual(await self.db.deck_count(OWNER), 0)
        self.assertEqual(await self.db.recover_pending_learning_charges(), 1)
        with self.assertRaises(ValueError):
            await self.db.deck_create_with_cards(
                OWNER, self.item_id, "بازپرداخت‌شده", [{"front": "پ", "back": "ج"}],
                paid_charge_id="deck",
            )
        self.assertEqual(await self.db.deck_count(OWNER), 0)
        self.assertEqual(await self.db.available_seconds(OWNER), 2_000)

    async def test_duplicate_operation_id_never_creates_a_second_debit(self):
        await self.db.spend_seconds(OWNER, 300, "first", operation_id="same")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.spend_seconds(OWNER, 300, "second", operation_id="same")
        self.assertEqual(await self.db.available_seconds(OWNER), 1_700)
        rows = await self.db.db.execute_fetchall("SELECT COUNT(*) FROM ledger WHERE kind = 'debit'")
        self.assertEqual(rows[0][0], 1)

    async def test_bot_startup_runs_paid_recovery(self):
        await self.db.spend_seconds(OWNER, 300, "fc-pending:1", operation_id="interrupted")
        await self.db.close()
        settings = make_settings(db_path=self.root / "charges.sqlite3", temp_dir=self.root / "tmp")
        bot = GamasBot(settings)
        bot.client = SimpleNamespace(
            start=mock.AsyncMock(), run_until_disconnected=mock.AsyncMock(), disconnect=mock.AsyncMock()
        )
        try:
            await bot.run()
            self.assertEqual(await bot.db.available_seconds(OWNER), 2_000)
        finally:
            await bot.shutdown()
        await self.db.connect()


class CurationFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = make_settings(
            db_path=root / "bot.sqlite3", temp_dir=root / "tmp", flashcard_ai_deck_size=6
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(OWNER)
        await self.db.add_credit(OWNER, 1_000, note="credit")
        await self.db.consume_free_use(OWNER, "deck")
        await self.db.consume_free_use(OWNER, "quiz")
        self.item = await self.db.library_add(OWNER, "booklet", OWNER, 10, title="درس")
        await self.db.library_set_content(self.item, "تنفس سلولی در میتوکندری انجام می‌شود.")
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        self.bot.client = self.client

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self.temp.cleanup()

    def press(self, data: bytes):
        return FakeCallbackEvent(self.client, data=data, chat_id=OWNER)

class PaidCurationTests(CurationFixture):
    async def test_unlimited_change_between_checks_does_not_consume_a_free_try(self):
        await self.db.refund_free_use(OWNER, "deck")
        await self.db.set_unlimited(OWNER, True, admin_id=1)
        # Simulate the initial check racing with an admin's committed change;
        # the transaction must recheck the real account status.
        with mock.patch.object(self.db, "is_unlimited", new=mock.AsyncMock(return_value=False)):
            budget = await self.bot._claim_generation_budget(self.press(b"fc:go:1"), OWNER, "deck", 300, "test")
        self.assertEqual(budget, (False, False, None))
        self.assertTrue((await self.db.free_use_state(OWNER))["deck"])
        self.assertEqual(await self.db.available_seconds(OWNER), 1_000)

    async def test_refund_failure_is_reported_and_recovered_on_restart(self):
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=cards_payload(6))):
            await self.bot._handle_callback(self.press(f"fc:go:{self.item}".encode()))
        event = self.press(b"fc:cur:cancel")
        with mock.patch.object(
            self.db, "refund_learning_charge", side_effect=sqlite3.OperationalError("database unavailable")
        ):
            await self.bot._handle_callback(event)
        self.assertIn("فعلاً ناموفق", event.edits[-1]["text"])
        self.assertEqual(await self.db.available_seconds(OWNER), 700)
        self.assertEqual(await self.db.recover_pending_learning_charges(), 1)
        self.assertEqual(await self.db.available_seconds(OWNER), 1_000)

    async def test_unlimited_cancel_does_not_claim_a_gift_was_restored(self):
        await self.db.set_unlimited(OWNER, True, admin_id=1)
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=cards_payload(6))):
            await self.bot._handle_callback(self.press(f"fc:go:{self.item}".encode()))
        event = self.press(b"fc:cur:cancel")
        await self.bot._handle_callback(event)
        self.assertIn("هزینه‌ای کسر نشده", event.edits[-1]["text"])

    async def test_saved_paid_deck_and_quiz_are_not_refunded_on_restart(self):
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=cards_payload(6))):
            await self.bot._handle_callback(self.press(f"fc:go:{self.item}".encode()))
        await self.bot._handle_callback(self.press(b"fc:cur:save"))
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=quiz_payload(5))):
            await self.bot._handle_callback(self.press(f"quiz:go:{self.item}:5".encode()))
        await self.bot._handle_callback(self.press(b"quiz:cur:save"))
        self.assertEqual(await self.db.available_seconds(OWNER), 400)
        self.assertEqual(await self.db.recover_pending_learning_charges(), 0)
        self.assertEqual(await self.db.deck_count(OWNER), 1)
        self.assertEqual(len(await self.db.db.execute_fetchall("SELECT * FROM quiz_sessions")), 1)

    async def test_cancel_during_committed_save_never_refunds_paid_artifact(self):
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=cards_payload(6))):
            await self.bot._handle_callback(self.press(f"fc:go:{self.item}".encode()))
        committed = asyncio.Event()
        original = self.db.deck_create_with_cards

        async def save_then_wait(*args, **kwargs):
            result = await original(*args, **kwargs)
            committed.set()
            await asyncio.Event().wait()
            return result

        with mock.patch.object(self.db, "deck_create_with_cards", side_effect=save_then_wait):
            task = asyncio.create_task(self.bot._handle_callback(self.press(b"fc:cur:save")))
            await asyncio.wait_for(committed.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(await self.db.deck_count(OWNER), 1)
        self.assertEqual(await self.db.available_seconds(OWNER), 700)
        self.assertEqual(await self.db.recover_pending_learning_charges(), 0)


class OneShotCallback(FakeCallbackEvent):
    """Real Telethon sends only the first answer for each callback query."""

    def __init__(self, client, *, data: bytes, chat_id: int):
        super().__init__(client, data=data, chat_id=chat_id)
        self._answered = False

    async def answer(self, text=None, alert=False):
        if self._answered:
            return
        self._answered = True
        await super().answer(text, alert=alert)


class CallbackAnswerTests(CurationFixture):
    async def test_specific_stale_and_invalid_alerts_are_not_eaten_by_early_ack(self):
        for payload in (b"doc:show:999999", b"lib:it:999999", b"quiz:bad", b"fc:cur:save", b"sup:bad"):
            with self.subTest(payload=payload):
                event = OneShotCallback(self.client, data=payload, chat_id=OWNER)
                await self.bot._handle_callback(event)
                self.assertEqual(len(event.answers), 1)
                self.assertTrue(event.answers[0][0])
                self.assertTrue(event.answers[0][1])

    async def test_running_generation_answers_before_waiting_on_the_provider(self):
        entered = asyncio.Event()
        unblock = asyncio.Event()

        async def provider(*args, **kwargs):
            entered.set()
            await unblock.wait()
            return cards_payload(6)

        event = OneShotCallback(self.client, data=f"fc:go:{self.item}".encode(), chat_id=OWNER)
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            task = asyncio.create_task(self.bot._handle_callback(event))
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(event.answers, [(None, False)])
            unblock.set()
            await task


class AudioOnlyPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support import AUDIO_REL, FakeJobEvent, build_deck, wav_bytes

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            db_path=self.root / "presentations.sqlite3", temp_dir=self.root / "tmp",
            note_api_key="", note_api_base_url="", note_providers=(),
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(OWNER)
        await self.db.add_credit(OWNER, 2_000, note="credit")
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        self.bot.client = self.client
        path = build_deck(
            self.root / "audio-only.pptx",
            slides=[{"media": [("narration.wav", wav_bytes(1), AUDIO_REL)]}],
        )
        self.upload = FakeJobEvent(source=path, chat_id=OWNER, message_id=431)
        self.upload.message.client = self.client
        self.upload.message.attach_document(
            path.name,
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            size=path.stat().st_size,
        )

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self.temp.cleanup()

    async def test_textless_narrated_deck_can_be_selected_and_transcribed(self):
        from gamas_bot.transcript import RawTranscript

        await self.bot._handle_message(self.upload)
        self.assertEqual(await self.db.document_count(OWNER), 0)
        self.assertEqual(self.bot.queue.pending, 0)  # explicit user opt-in
        buttons = [button.type.data for record in self.client.texts
                   for row in (record.get("buttons") or []) for button in row]
        payload = f"doc:lecturemsg:{self.upload.message.id}".encode()
        self.assertIn(payload, buttons)
        self.client.get_messages = mock.AsyncMock(return_value=self.upload.message)
        event = OneShotCallback(self.client, data=payload, chat_id=OWNER)
        await self.bot._handle_callback(event)
        self.client.get_messages.assert_awaited_once_with(OWNER, ids=self.upload.message.id)
        self.assertEqual(self.bot.queue.pending, 1)
        job = self.bot.queue.pop_pending()
        with mock.patch("gamas_bot.bot.transcribe", new=mock.AsyncMock(
            return_value=RawTranscript(words=[], text="متن سخنرانی", engine="fake")
        )):
            await self.bot._run_job(job)
        self.assertEqual((await self.db.submission(job.submission_id)).status, "done")
        self.assertIn("متن سخنرانی", (await self.db.transcription(job.submission_id))["text"])

    async def test_callback_cannot_queue_an_outgoing_or_foreign_source(self):
        self.client.get_messages = mock.AsyncMock(return_value=self.upload.message)
        self.upload.message.out = True
        event = OneShotCallback(self.client, data=b"doc:lecturemsg:431", chat_id=OWNER)
        await self.bot._handle_callback(event)
        self.assertEqual(self.bot.queue.pending, 0)
        self.upload.message.out = False
        self.upload.message.sender_id = OTHER
        await self.bot._handle_callback(OneShotCallback(self.client, data=b"doc:lecturemsg:431", chat_id=OWNER))
        self.assertEqual(self.bot.queue.pending, 0)


class RangedTransferIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_incomplete_early_range_falls_back_instead_of_accepting_hole(self):
        from gamas_bot.bot import TELEGRAM_DOWNLOAD_CHUNK_BYTES, _download_media_limited
        from tests.test_bot import _RangedClient, _RangedMessage

        chunk = TELEGRAM_DOWNLOAD_CHUNK_BYTES
        payload = bytes(range(251)) * (chunk * 4 // 251 + 1)
        payload = payload[:chunk * 4]

        class IncompleteFirstRange(_RangedClient):
            def iter_download(self, media, *, offset=0, limit=None, **kwargs):
                parent = super().iter_download(media, offset=offset, limit=limit, **kwargs)

                async def generator():
                    async for part in parent:
                        yield part
                        if offset == 0:
                            return  # the later range still sets the full file size
                return generator()

        message = _RangedMessage(len(payload), payload)
        client = IncompleteFirstRange(payload)
        target = self.root / "gap.bin"
        await _download_media_limited(message, target, len(payload), client=client, connections=2)
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(len(message.download_calls), 1)

    async def test_short_pwrite_is_completed_without_corruption(self):
        import os

        from gamas_bot.bot import _download_media_limited
        from tests.test_bot import _RangedClient, _RangedMessage

        payload = b"0123456789" * 40_000
        message = _RangedMessage(len(payload), payload)
        client = _RangedClient(payload)
        target = self.root / "partial.bin"
        real_pwrite = os.pwrite

        def short_write(fd, data, offset):
            return real_pwrite(fd, data[:max(1, len(data) // 3)], offset)

        with mock.patch("gamas_bot.bot.os.pwrite", side_effect=short_write):
            await _download_media_limited(message, target, len(payload), client=client, connections=2)
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(message.download_calls, [])


class UploadAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            db_path=self.root / "uploads.sqlite3", temp_dir=self.root / "tmp",
            max_active_jobs_per_user=2,
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(OWNER)
        await self.db.add_credit(OWNER, 2_000, note="credit")
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        self.bot.client = self.client

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self.temp.cleanup()

    def voice_event(self, message_id, doc_id=4321):
        from tests.support import FakeJobEvent

        source = self.root / "voice.ogg"
        source.write_bytes(b"OggS" + b"x" * 100)
        event = FakeJobEvent(source=source, chat_id=OWNER, message_id=message_id)
        event.message.client = self.client
        event.message.attach_voice(duration=10)
        event.message.document = SimpleNamespace(id=doc_id, mime_type="audio/ogg", attributes=[])
        return event

    async def test_identical_file_is_suppressed_from_admission_through_queue(self):
        first, second = self.voice_event(1), self.voice_event(2)
        await asyncio.gather(self.bot._handle_message(first), self.bot._handle_message(second))
        self.assertEqual(self.bot.queue.pending, 1)
        self.assertEqual(len(await self.db.user_submissions(OWNER)), 1)
        self.assertEqual(len(self.bot._active_upload_keys), 1)
        self.assertTrue(any("صف پردازش" in str(text["text"]) for text in self.client.texts))

    async def test_failed_admission_releases_file_claim(self):
        poor = 9199
        await self.db.ensure_user(poor)
        first = self.voice_event(10)
        first.sender_id = poor
        first.message._chat_id = poor
        await self.bot._handle_message(first)
        self.assertFalse(self.bot._active_upload_keys)
        await self.db.add_credit(poor, 1000, note="credit")
        retry = self.voice_event(11)
        retry.sender_id = poor
        retry.message._chat_id = poor
        await self.bot._handle_message(retry)
        self.assertEqual(self.bot.queue.pending, 1)

    async def test_shutdown_closes_queued_progress_and_releases_claim(self):
        await self.bot._handle_message(self.voice_event(20))
        job = next(iter(self.bot.queue._queues[OWNER]))
        progress = job.meta["progress"]
        ticker = progress._ticker
        await self.bot.shutdown()
        self.assertTrue(progress._done)
        self.assertTrue(ticker.done())
        self.assertFalse(self.bot._active_upload_keys)
        await self.db.connect()
        row = await self.db.submission(job.submission_id)
        self.assertEqual(row.status, "failed")

    async def test_cancelled_progress_creation_fails_the_pending_row(self):
        entered = asyncio.Event()

        async def stalled(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with mock.patch("gamas_bot.bot.JobProgress.create", side_effect=stalled):
            task = asyncio.create_task(self.bot._handle_message(self.voice_event(30)))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        submissions = await self.db.user_submissions(OWNER)
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["status"], "failed")
        self.assertEqual(self.bot.queue.pending, 0)
        self.assertFalse(self.bot._active_upload_keys)

    async def test_cancel_during_submission_commit_closes_the_committed_row(self):
        committed, release = asyncio.Event(), asyncio.Event()
        original = self.db.create_submission

        async def delayed(*args, **kwargs):
            row_id = await original(*args, **kwargs)
            committed.set()
            await release.wait()
            return row_id

        with mock.patch.object(self.db, "create_submission", side_effect=delayed):
            task = asyncio.create_task(self.bot._handle_message(self.voice_event(31)))
            await asyncio.wait_for(committed.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        submissions = await self.db.user_submissions(OWNER)
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["status"], "failed")
        self.assertEqual(self.bot.queue.pending, 0)
        self.assertFalse(self.bot._active_upload_keys)

    async def test_shutdown_cancels_and_joins_admission_before_closing_database(self):
        entered = asyncio.Event()

        async def stalled(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with mock.patch("gamas_bot.bot.JobProgress.create", side_effect=stalled):
            task = asyncio.create_task(self.bot._handle_message(self.voice_event(32)))
            await asyncio.wait_for(entered.wait(), 2)
            await self.bot.shutdown()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.bot._admission_tasks)
        await self.db.connect()
        submissions = await self.db.user_submissions(OWNER)
        self.assertEqual(submissions[0]["status"], "failed")


class LearningProgressTests(CurationFixture):
    async def test_learning_chunk_concurrency_honors_the_configured_range(self):
        from dataclasses import replace

        from gamas_bot.doc_extract import DocChunk

        chunks = [DocChunk(i, f"صفحه {i}", (f"صفحه {i}",), f"بخش {i}") for i in range(8)]
        for limit in (1, 6):
            self.bot.settings = replace(self.bot.settings, learning_chunk_concurrency=limit)
            active = peak = 0

            async def provider(*args, **kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.01)
                    chunk = next(item for item in chunks if item.text == kwargs["user"])
                    return {"cards": [{"front": f"پرسش {chunk.index}", "back": "پاسخ", "source": chunk.locator}]}
                finally:
                    active -= 1

            with mock.patch.object(self.bot, "_progress_edit", new=mock.AsyncMock()), \
                 mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
                items, failed = await self.bot._generate_chunks(
                    kind="deck", chunks=chunks, counts=[1] * 8, placeholder=object()
                )
            self.assertEqual(len(items), 8)
            self.assertEqual(failed, 0)
            self.assertEqual(peak, limit)

    async def test_stalled_telegram_progress_does_not_hold_model_slots(self):
        from gamas_bot.doc_extract import DocChunk

        chunks = [DocChunk(i, f"صفحه {i}", (f"صفحه {i}",), f"بخش {i}") for i in range(8)]
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        calls = []

        async def progress(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def provider(*args, **kwargs):
            await asyncio.sleep(0.01)
            chunk = next(item for item in chunks if item.text == kwargs["user"])
            calls.append(chunk.index)
            return {"cards": [{"front": f"پرسش {chunk.index}", "back": "پاسخ", "source": chunk.locator}]}

        with mock.patch("gamas_bot.bot.LEARNING_PROGRESS_EDIT_TIMEOUT_SECONDS", 0.05), \
             mock.patch.object(self.bot, "_progress_edit", side_effect=progress), \
             mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            items, failed = await asyncio.wait_for(
                self.bot._generate_chunks(kind="deck", chunks=chunks, counts=[1] * 8, placeholder=object()),
                timeout=1,
            )
        self.assertTrue(entered.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(sorted(calls), list(range(8)))
        self.assertEqual(len(items), 8)
        self.assertEqual(failed, 0)


class DocumentChunkingRegressionTests(unittest.TestCase):
    def test_optimized_split_matches_original_on_varied_whitespace(self):
        import random

        from gamas_bot.doc_extract import _split_paragraphs

        def original(text, limit):
            text = text.strip()
            if not text:
                return []
            if len(text) <= limit:
                return [text]
            pieces = []
            while len(text) > limit:
                window = text[:limit]
                cut = window.rfind("\n\n")
                if cut < limit // 2:
                    cut = window.rfind("\n")
                if cut < limit // 2:
                    cut = window.rfind(" ")
                if cut < limit // 2:
                    cut = limit
                pieces.append(text[:cut].strip())
                text = text[cut:].strip()
            if text:
                pieces.append(text)
            return [piece for piece in pieces if piece]

        rng = random.Random(42)
        for _ in range(200):
            text = "".join(rng.choice("الف ب ج\n\t\u00a0") for _ in range(rng.randrange(1, 900)))
            limit = rng.randrange(64, 180)
            self.assertEqual(_split_paragraphs(text, limit), original(text, limit))

    def test_long_section_keeps_all_words_and_source_locators(self):
        from gamas_bot.doc_extract import DocSection, chunk_sections

        text = "آغاز " + "متن درسی " * 150_000 + "پایان"
        chunks = chunk_sections([DocSection("صفحهٔ ۱", "", text)], max_chars=6000)
        self.assertGreater(len(chunks), 100)
        self.assertTrue(all(len(chunk.text) <= 6000 and chunk.sources for chunk in chunks))
        bodies = " ".join(chunk.text.split("\n", 1)[1] for chunk in chunks)
        self.assertEqual(bodies.split(), text.split())


class DocumentConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            db_path=self.root / "docs.sqlite3", temp_dir=self.root / "tmp", media_worker_concurrency=1
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(OWNER)
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.bot.client = FakeTelegramClient()

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self.temp.cleanup()

    async def test_simultaneous_identical_uploads_parse_once(self):
        import threading

        from gamas_bot.doc_extract import DocSection, ExtractedDoc

        source = self.root / "document.pdf"
        source.write_bytes(b"the same source file")
        entered, release = threading.Event(), threading.Event()

        def extract(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timed out")
            return ExtractedDoc("pdf", 1, (DocSection("صفحهٔ ۱", "", "متن درس"),))

        async def ingest(reference):
            return await self.bot._ingest_document(
                OWNER, source, source_type="pdf", filename="document.pdf", file_msg_ref=reference
            )

        with mock.patch("gamas_bot.bot.extract_document", side_effect=extract) as parser:
            first = asyncio.create_task(ingest(f"{OWNER}:101"))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            second = asyncio.create_task(ingest(f"{OWNER}:102"))
            await asyncio.sleep(0.05)
            release.set()
            one, two = await asyncio.gather(first, second)
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(one[0]["id"], two[0]["id"])
        self.assertEqual([one[1], two[1]], [True, False])
        self.assertEqual(await self.db.document_count(OWNER), 1)
        self.assertEqual((await self.db.document_get(OWNER, one[0]["id"]))["file_msg_ref"], f"{OWNER}:102")

    async def test_distinct_document_parsers_obey_configured_concurrency(self):
        import threading
        import time

        from gamas_bot.doc_extract import DocSection, ExtractedDoc

        active = peak = 0
        guard = threading.Lock()

        def extract(*args, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.04)
                return ExtractedDoc("pdf", 1, (DocSection("صفحهٔ ۱", "", "متن درس"),))
            finally:
                with guard:
                    active -= 1

        paths = [self.root / f"doc-{i}.pdf" for i in range(3)]
        for index, path in enumerate(paths):
            path.write_text(f"different {index}")
        with mock.patch("gamas_bot.bot.extract_document", side_effect=extract):
            await asyncio.gather(*(self.bot._ingest_document(
                OWNER, path, source_type="pdf", filename=path.name, file_msg_ref=f"{OWNER}:{i + 1}"
            ) for i, path in enumerate(paths)))
        self.assertEqual(peak, 1)
        self.assertEqual(await self.db.document_count(OWNER), 3)

    async def test_shutdown_joins_document_intake_and_removes_its_temp_files(self):
        from tests.support import FakeJobEvent

        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        event = FakeJobEvent(source=source, chat_id=OWNER, message_id=123)
        event.message.attach_document("source.pdf", mime_type="application/pdf", size=source.stat().st_size)
        event.message.client = self.bot.client
        entered = asyncio.Event()

        async def download(_message, target, _limit):
            target.write_bytes(b"temporary document")
            return target

        async def stalled(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with mock.patch.object(self.bot, "_download_telegram_media", side_effect=download), \
             mock.patch.object(self.bot, "_ingest_document", side_effect=stalled):
            task = asyncio.create_task(self.bot._accept_document(event, OWNER, "pdf", "source.pdf"))
            await asyncio.wait_for(entered.wait(), 2)
            self.assertTrue(list(self.settings.temp_dir.glob("doc-*")))
            await self.bot.shutdown()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.bot._intake_tasks)
        self.assertEqual(list(self.settings.temp_dir.glob("doc-*")), [])
        await self.db.connect()
        self.assertEqual(await self.db.document_count(OWNER), 0)

    async def test_startup_sweeps_only_stale_known_work_directories(self):
        import os

        stale = self.settings.temp_dir / "doc-stale"
        stale.mkdir()
        (stale / "source.pdf").write_text("private")
        os.utime(stale, (self.bot._started_at - 20, self.bot._started_at - 20))
        fresh = self.settings.temp_dir / "doc-fresh"
        fresh.mkdir()
        os.utime(fresh, (self.bot._started_at + 10, self.bot._started_at + 10))
        unrelated = self.settings.temp_dir / "other-stale"
        unrelated.mkdir()
        os.utime(unrelated, (self.bot._started_at - 20, self.bot._started_at - 20))
        self.bot._sweep_stale_temp_dirs()
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unrelated.exists())


class ExtractedSizeLimitTests(unittest.TestCase):
    def test_separator_overhead_is_included_in_the_stored_text_limit(self):
        from gamas_bot.doc_extract import DocExtractError, DocSection, extract_document

        sections = (DocSection("1", "", "a" * 10), DocSection("2", "", "b" * 10))
        with mock.patch("gamas_bot.doc_extract.MAX_EXTRACTED_DOCUMENT_CHARS", 20), \
             mock.patch("gamas_bot.doc_extract._pdf_sections", return_value=(sections, 2)), \
             self.assertRaises(DocExtractError):
            extract_document(Path("unused.pdf"), "pdf")

    def test_pdf_word_and_presentations_reject_oversized_text(self):
        from gamas_bot.doc_extract import DocExtractError, extract_document
        from tests.test_doc_extract import make_docx_bytes, make_pdf_bytes, make_pptx_bytes

        with tempfile.TemporaryDirectory() as root, mock.patch(
            "gamas_bot.doc_extract.MAX_EXTRACTED_DOCUMENT_CHARS", 20
        ):
            for name, source_type, payload in (
                ("large.pdf", "pdf", make_pdf_bytes()),
                ("large.docx", "docx", make_docx_bytes()),
                ("large.pptx", "pptx", make_pptx_bytes()),
            ):
                path = Path(root) / name
                path.write_bytes(payload)
                with self.subTest(name=name), self.assertRaisesRegex(DocExtractError, "چند بخش"):
                    extract_document(path, source_type)


class RawTranscriptFallbackTests(unittest.TestCase):
    def test_splitting_preserves_all_leading_trailing_and_boundary_newlines(self):
        from gamas_bot.bot import raw_parts

        text = "\n\n" + ("کلمه " * 80 + "\n\n\n\n" + "ادامه " * 80) + "\n\n"
        parts = raw_parts(text, max_chars=200, max_parts=20)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 200 for part in parts))
        self.assertEqual("".join(parts), text)
