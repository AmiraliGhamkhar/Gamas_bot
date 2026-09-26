from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gamas_bot.bot import _utf16_length, markdown_to_telegram_html, split_message
from gamas_bot.database import Database
from gamas_bot.stt import Transcript, _deepgram_transcript, _speechmatics_confidence, transcribe
from gamas_bot.structuring import split_transcript
from scripts.benchmark_stt import normalize_words, word_error_rate

from support import make_settings


class TextHelpersTests(unittest.TestCase):
    def test_split_message_preserves_content(self):
        text = ("بخش اول و دوم. " * 1200).strip()
        pages = split_message(text, 500)
        self.assertGreater(len(pages), 1)
        self.assertEqual(" ".join(pages).replace("  ", " "), text.replace("  ", " "))
        self.assertTrue(all(len(page) <= 500 for page in pages))

    def test_split_message_obeys_telegram_utf16_limit(self):
        pages = split_message("😀" * 1200, limit=1000)
        self.assertTrue(all(_utf16_length(page) <= 1000 for page in pages))
        self.assertEqual(sum(len(page) for page in pages), 1200)

    def test_markdown_is_converted_to_safe_telegram_html(self):
        rendered = markdown_to_telegram_html(
            "# عنوان\n\n**نکته مهم** <script>\n- مورد اول\n\n| درس | زمان |\n|---|---|\n| ریاضی | ۲ ساعت |"
        )
        self.assertIn("<b>عنوان</b>", rendered)
        self.assertIn("<b>نکته مهم</b>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("• مورد اول", rendered)
        self.assertIn("درس:</b> ریاضی", rendered)

    def test_long_transcript_splits_under_limit(self):
        text = "این یک جملهٔ فارسی است. " * 1500
        chunks = split_transcript(text, max_chars=1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        self.assertEqual(" ".join(chunks).replace("  ", " "), text.strip().replace("  ", " "))

    def test_persian_benchmark_normalization_and_wer(self):
        self.assertEqual(normalize_words("كِتاب، يک درس!"), ["کتاب", "یک", "درس"])
        self.assertEqual(word_error_rate("یک کتاب خوب", "یک کتاب خوب"), 0.0)
        self.assertAlmostEqual(word_error_rate("یک کتاب خوب", "یک دفتر خوب"), 1 / 3)

    def test_stt_response_parsers(self):
        deepgram = _deepgram_transcript({
            "results": {"channels": [{"alternatives": [{"transcript": "سلام دنیا", "confidence": 0.91}]}]}
        })
        self.assertEqual(deepgram.engine, "deepgram")
        self.assertEqual(deepgram.text, "سلام دنیا")
        self.assertAlmostEqual(deepgram.confidence, 0.91)
        self.assertAlmostEqual(
            _speechmatics_confidence({
                "results": [
                    {"alternatives": [{"confidence": 0.8}]},
                    {"alternatives": [{"confidence": 1.0}]},
                ]
            }),
            0.9,
        )


class STTRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_low_confidence_routes_to_fallback(self):
        settings = make_settings(gemini_api_key=None)
        with tempfile.NamedTemporaryFile() as audio:
            with patch(
                "gamas_bot.stt._speechmatics",
                new=AsyncMock(return_value=Transcript("speechmatics", "ضعیف", 0.4)),
            ), patch(
                "gamas_bot.stt._deepgram",
                new=AsyncMock(return_value=Transcript("deepgram", "بهتر", 0.9)),
            ) as fallback:
                result = await transcribe(Path(audio.name), settings)
        self.assertEqual(result.engine, "deepgram")
        fallback.assert_awaited_once()


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "bot.sqlite3")
        await self.db.open()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_user_submission_transcription_and_ban(self):
        user = await self.db.upsert_user(987654, "student")
        self.assertFalse(user["is_banned"])
        submission_id = await self.db.create_submission(
            user["id"], "telegram-file", 12.5, "class.mp3", "audio/mpeg"
        )
        await self.db.set_submission_status(submission_id, "processing")
        await self.db.save_transcription(submission_id, "deepgram", "متن خام", "جزوه")
        await self.db.set_submission_status(submission_id, "done")
        stats = await self.db.stats()
        self.assertEqual(stats["users"], 1)
        self.assertEqual(stats["presentations"], 0)
        self.assertEqual(stats["submissions"], 1)
        self.assertEqual(stats["done"], 1)
        rows = await self.db.user_summaries()
        self.assertEqual(rows[0]["submission_count"], 1)
        self.assertEqual(await self.db.user_ids(), [987654])
        self.assertTrue(await self.db.set_banned(987654, True))
        self.assertEqual(await self.db.user_ids(), [])
        self.assertFalse(await self.db.set_banned(111111, True))

    async def test_presentation_details_are_stored_and_counted(self):
        user = await self.db.upsert_user(222333, "presenter")
        submission_id = await self.db.create_submission(
            user["id"], "deck-file", None, "lecture.pptx", None, source_type="pptx"
        )
        await self.db.save_presentation_details(
            submission_id,
            slide_count=12,
            clips=[
                {
                    "slide_number": 1,
                    "part_name": "ppt/media/media1.m4a",
                    "kind": "audio",
                    "duration": 30.5,
                    "included": True,
                },
                {
                    "slide_number": None,
                    "part_name": "ppt/media/media2.mp4",
                    "kind": "video",
                    "duration": None,
                    "included": False,
                    "skip_reason": "بدون شاخهٔ صوتی",
                },
            ],
            media_duration=30.5,
        )
        clips = await self.db.presentation_clips(submission_id)
        self.assertEqual(len(clips), 2)
        self.assertEqual(clips[0]["included"], 1)
        self.assertEqual(clips[1]["skip_reason"], "بدون شاخهٔ صوتی")
        stats = await self.db.stats()
        self.assertEqual(stats["presentations"], 1)
        self.assertEqual(stats["presentation_clips"], 2)

    async def test_migrations_are_applied_once(self):
        await self.db.close()
        await self.db.open()
        async with self.db._lock:
            cursor = await self.db._db().execute("SELECT name FROM schema_migrations ORDER BY name")
            names = [row[0] for row in await cursor.fetchall()]
        self.assertIn("001_initial.sql", names)
        self.assertIn("002_presentations.sql", names)
        self.assertEqual(len(names), len(set(names)))

    async def test_interrupted_jobs_are_marked_failed_on_reopen(self):
        user = await self.db.upsert_user(123, None)
        submission_id = await self.db.create_submission(user["id"], "file", None, None, None)
        await self.db.close()
        await self.db.open()
        rows = await self.db.user_summaries()
        self.assertEqual(rows[0]["submission_count"], 1)
        async with self.db._lock:
            cursor = await self.db._db().execute(
                "SELECT status FROM audio_submissions WHERE id=?", (submission_id,)
            )
            self.assertEqual((await cursor.fetchone())[0], "failed")


if __name__ == "__main__":
    unittest.main()
