"""The progress bar shown for short actions: quiz, flashcards, documents, Office.

Every one of these screens used to jump straight to a plain sentence («در حال
ساخت…») and then sit still for minutes. The bar exists *before* the first model
call, advances per step, keeps its last frame on failure, and its edits are
always decorative — a refused edit never costs the user work.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telethon.errors import FloodWaitError

from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from gamas_bot.progress import (
    FAILED_HEAD,
    WORK_DONE_HEADLINE,
    JobProgress,
    WorkProgress,
    completed_progress_text,
    failed_progress_text,
    progress_bar,
    progress_text,
)
from tests.support import FakeCallbackEvent, FakeTelegramClient, make_settings
from tests.test_learning import quiz_payload

OWNER = 555


class _Recorder:
    """A message double that records every frame it was edited to."""

    def __init__(self, *, fail_first: bool = False, error: Exception | None = None):
        self.frames: list[str] = []
        self.fail_first = fail_first
        self.error = error

    async def edit(self, text: str = "", buttons=None) -> None:
        if self.fail_first:
            self.fail_first = False
            raise self.error or RuntimeError("telegram refused the edit")
        self.frames.append(text)

    async def respond(self, text: str, **kwargs):
        return _Recorder()


class ProgressTextTests(unittest.TestCase):
    def test_a_headline_names_the_work_instead_of_always_saying_file(self):
        self.assertIn(
            "🧠 در حال ساخت محتوا",
            progress_text(20, "تولید", headline="🧠 در حال ساخت محتوا"),
        )
        self.assertIn("در حال پردازش فایل", progress_text(20, "تولید"))

    def test_bar_is_fixed_width_at_every_percent(self):
        for percent in (0, 1, 50, 96, 99, 100):
            self.assertEqual(len(progress_bar(percent)), 12, percent)

    def test_failure_frame_keeps_the_bar_and_the_stopped_percent(self):
        text = failed_progress_text(37, "دوباره تلاش کنید.")
        self.assertIn(FAILED_HEAD, text)
        self.assertIn("۳۷٪", text)
        self.assertIn("دوباره تلاش کنید.", text)

    def test_completed_frame_is_full_and_carries_the_outcome(self):
        text = completed_progress_text("فایل ساخته شد", headline="✅ فایل آماده شد")
        self.assertIn("✅ فایل آماده شد", text)
        self.assertIn("۱۰۰٪", text)
        self.assertIn("فایل ساخته شد", text)


class WorkProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_frames_are_monotonic_and_end_on_the_done_headline(self):
        recorder = _Recorder()
        progress = WorkProgress(recorder.edit, headline="🧠 ساخت", done_headline=WORK_DONE_HEADLINE)
        await progress.update(30, "تولید", "بخش ۱ از ۲")
        await progress.update(10, "تولید", "دیرتر ولی کوچک‌تر")  # never moves back
        await progress.complete("۴ مورد معتبر")
        self.assertIn("۳۰٪", recorder.frames[0])
        self.assertIn("۳۰٪", recorder.frames[1])
        self.assertIn(WORK_DONE_HEADLINE, recorder.frames[-1])
        self.assertEqual(progress.percent, 100)

    async def test_a_refused_edit_never_raises(self):
        recorder = _Recorder(fail_first=True)
        progress = WorkProgress(recorder.edit)
        await progress.update(40, "تولید")  # Telegram hiccup: swallowed
        await progress.update(60, "تولید")
        self.assertEqual(len(recorder.frames), 1)
        self.assertIn("۶۰٪", recorder.frames[0])

    async def test_flood_wait_is_skipped_not_awaited(self):
        class Flooding:
            def __init__(self):
                self.calls = 0

            async def edit(self, text: str = "", buttons=None) -> None:
                self.calls += 1
                raise FloodWaitError(request=None, capture=9)

        flooding = Flooding()
        progress = WorkProgress(flooding.edit)
        await asyncio.wait_for(progress.update(50, "تولید"), timeout=1)
        self.assertEqual(flooding.calls, 1)

    async def test_start_sends_the_zero_frame_before_any_work(self):
        event = FakeCallbackEvent(FakeTelegramClient(), data=b"quiz:go:1:5")
        progress = await WorkProgress.start(event, stage="آماده‌سازی سؤال")
        self.assertIsNotNone(progress.message)
        self.assertEqual(progress.percent, 0)
        await progress.update(50, "تولید")
        await progress.aclose()

    async def test_edit_passthrough_keeps_the_legacy_helper_working(self):
        recorder = _Recorder()
        progress = WorkProgress(recorder.edit, message=recorder)
        await progress.edit("متن نهایی", buttons=None)
        self.assertEqual(recorder.frames[-1], "متن نهایی")


class _LearningFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "learning.db",
            temp_dir=root / "tmp",
            flashcard_ai_cost_seconds=300,
            flashcard_ai_deck_size=6,
            quiz_cost_seconds_per_question=60,
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        self.bot.client = self.client
        await self.db.ensure_user(OWNER)
        await self.db.add_credit(OWNER, 3600, note="test", validity_days=30)
        self.item_id = await self.db.library_add(
            OWNER, "booklet", 10, 20, None, title="جزوهٔ غدد"
        )
        await self.db.library_set_content(self.item_id, "متن جزوهٔ غدد برای آزمون")

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def press(self, data: bytes) -> FakeCallbackEvent:
        event = FakeCallbackEvent(self.client, data=data, chat_id=OWNER)
        event.client = self.client
        return event


class LearningBarTests(_LearningFixture):
    async def test_the_bar_is_sent_before_the_first_model_call(self):
        observed: list[str] = []

        async def provider(*args, **kwargs):
            # Everything Telegram showed *before* the model was asked.
            observed.extend(text["text"] for text in self.client.texts)
            return quiz_payload(5, source="بخش ۱")

        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            await self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode()))

        self.assertTrue(observed, "no message was sent before the model call")
        first = observed[0]
        self.assertIn("░", first)
        self.assertIn("٪", first)
        self.assertIn("در حال ساخت محتوا", first)
        self.assertIn(OWNER, self.bot._curation)

    async def test_failed_generation_leaves_a_failure_bar_and_refunds(self):
        event = self.press(f"quiz:go:{self.item_id}:5".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value={"questions": []})
        ):
            await self.bot._handle_callback(event)
        self.assertNotIn(OWNER, self.bot._curation)
        self.assertEqual(await self.db.available_seconds(OWNER), 3600)
        frames = [text["text"] for text in self.client.texts]
        self.assertTrue(any("ساخت محتوا ناموفق" in frame for frame in frames), frames)

    async def test_progress_frames_report_the_bar_and_the_chunk_count(self):
        from gamas_bot.doc_extract import DocChunk

        chunks = [DocChunk(0, "بخش ۱", ("بخش ۱",), "متن الف"), DocChunk(1, "بخش ۲", ("بخش ۲",), "متن ب")]
        recorder = _Recorder()
        progress = WorkProgress(recorder.edit)

        async def provider(*args, **kwargs):
            await asyncio.sleep(0)
            return {"cards": [{"front": "پرسش", "back": "پاسخ", "source": "بخش ۱"}]}

        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            await self.bot._generate_chunks(
                kind="deck", chunks=chunks, counts=[1, 1], placeholder=progress
            )
        self.assertTrue(recorder.frames)
        self.assertTrue(all("٪" in frame for frame in recorder.frames))
        self.assertTrue(any("بخش ۲ از ۲" in frame for frame in recorder.frames))

    async def test_a_plain_message_still_receives_a_bar_frame(self):
        """Older call sites/tests pass a message, not a reporter."""
        from gamas_bot.doc_extract import DocChunk

        message = _Recorder()
        chunks = [DocChunk(0, "بخش ۱", ("بخش ۱",), "متن الف")]

        async def provider(*args, **kwargs):
            return {"cards": [{"front": "پرسش", "back": "پاسخ", "source": "بخش ۱"}]}

        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            await self.bot._generate_chunks(
                kind="deck", chunks=chunks, counts=[1], placeholder=message
            )
        self.assertTrue(message.frames)
        self.assertIn("٪", message.frames[0])


class FileJobHeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_progress_keeps_its_own_wording(self):
        event = FakeCallbackEvent(FakeTelegramClient(), data=b"menu:main")
        progress = await JobProgress.create(event, submission_id=1, animate=False)
        self.assertIn("در حال پردازش فایل", progress._last_text)
        await progress.update(40, "تبدیل گفتار به متن")
        self.assertIn("۴۰٪", progress._last_text)
        await progress.complete("متن ارسال شد.")
        self.assertIn("✅ پردازش تکمیل شد", progress._last_text)
        await progress.aclose()


if __name__ == "__main__":
    unittest.main()
