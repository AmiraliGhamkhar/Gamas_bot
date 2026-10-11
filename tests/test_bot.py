"""End-to-end tests for the bot's media pipeline, over a real database."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from gamas_bot.jobs import Job
from gamas_bot.media import MediaInfo
from gamas_bot.progress import JobProgress
from gamas_bot.stt import STTError
from gamas_bot.transcript import RawTranscript, Word
from tests.support import FakeJobEvent, make_settings, wav_bytes

USER_ID = 555


class BotPipelineTestCase(unittest.IsolatedAsyncioTestCase):
    """A bot wired to a real temp database, with media/STT faked at the seams."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "bot.db",
            temp_dir=root / "tmp",
            note_api_key="",           # raw-only mode: no note API in tests
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(USER_ID)
        await self.db.sync_plans([
            {"code": "paid_5h_30d", "hours": 5, "price": 50000, "validity_days": 30},
        ])
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def media_event(self, name: str = "source.wav") -> FakeJobEvent:
        source = Path(self._tmp.name) / name
        source.write_bytes(wav_bytes(duration=0.2))
        return FakeJobEvent(source=source)

    async def run_media_job(
        self, event: FakeJobEvent, *, duration=100.0, credit=3600, transcribe=None
    ):
        await self.db.add_credit(USER_ID, credit, note="test")
        submission = await self.db.create_submission(USER_ID, "audio", "a.wav")
        progress = await JobProgress.create(event, submission_id=submission)
        job = Job(
            submission_id=submission,
            user_id=USER_ID,
            kind="audio",
            filename="a.wav",
            message=event.message,
            is_admin=False,
            duration_hint=duration,
            meta={"progress": progress},
        )
        audio = Path(self._tmp.name) / "in.wav"
        audio.write_bytes(wav_bytes(duration=0.2))

        async def fake_transcribe(path, settings, session=None):
            if isinstance(transcribe, BaseException):
                raise transcribe
            return transcribe

        with mock.patch(
            "gamas_bot.bot.probe_media", new=self._fake_probe(duration),
        ), mock.patch(
            "gamas_bot.bot.extract_audio_track", new=self._fake_extract(audio),
        ), mock.patch("gamas_bot.bot.transcribe", new=fake_transcribe):
            await self.bot._run_job(job)
        return submission

    @staticmethod
    def _fake_probe(duration: float):
        async def probe(_path, _settings):
            return MediaInfo(has_audio=True, duration=duration, audio_codec="pcm_s16le")
        return probe

    @staticmethod
    def _fake_extract(audio: Path):
        async def extract(source: Path, workdir: Path, _settings) -> Path:
            target = Path(workdir) / "audio.wav"
            target.write_bytes(Path(source).read_bytes())
            return target
        return extract

    @staticmethod
    def _raw() -> RawTranscript:
        words = [
            Word(text="سلام", start=0.0, end=0.5),
            Word(text="دوستان", start=0.6, end=1.0),
            Word(text=".", start=1.0, end=1.0),
            Word(text="امروز", start=1.2, end=1.6),
        ]
        return RawTranscript(words=words, text="سلام دوستان . امروز", engine="speechmatics")


class HappyPathTests(BotPipelineTestCase):
    async def test_raw_and_docx_delivered_and_ledger_settled(self):
        event = self.media_event()
        submission = await self.run_media_job(event, transcribe=self._raw())

        # The raw text file and the Word booklet were both delivered.
        raw_text = event.file_bytes(".txt").decode("utf-8")
        self.assertIn("سلام دوستان. امروز", raw_text)
        self.assertTrue(event.file_bytes(".docx").startswith(b"PK"))
        # A preview was sent to the chat.
        self.assertTrue(any("پیش‌نمایش" in r for r in event.replies))

        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")
        transcript = await self.db.transcription(submission)
        assert transcript is not None
        self.assertEqual(transcript["engine"], "speechmatics")
        self.assertIn("سلام دوستان. امروز", transcript["text"])
        # The measured duration (100s) is what got charged.
        self.assertEqual(await self.db.available_seconds(USER_ID), 3600 - 100)

    async def test_progressive_delivery_raw_before_notes(self):
        """Even when note generation fails, the raw text is already out."""
        event = self.media_event()
        submission = await self.run_media_job(event, transcribe=STTError("provider down"))

        self.assertEqual(event.files, [])  # nothing delivered
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "failed")
        # Credit fully refunded.
        self.assertEqual(await self.db.available_seconds(USER_ID), 3600)

    async def test_insufficient_balance_fails_with_notice(self):
        event = self.media_event()
        submission = await self.run_media_job(event, duration=7200.0, credit=600)
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "failed")
        self.assertTrue(any("اعتبار" in r for r in event.replies))
        # Nothing was consumed by the failed job.
        self.assertEqual(await self.db.available_seconds(USER_ID), 600)

    async def test_oversize_file_rejected_before_transcription(self):
        source = Path(self._tmp.name) / "source.wav"
        source.write_bytes(b"x" * (self.settings.max_file_size + 1))
        event = FakeJobEvent(source=source)
        submission = await self.run_media_job(event)
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "failed")
        self.assertTrue(any("سقف حجم" in r for r in event.replies))
        self.assertEqual(await self.db.available_seconds(USER_ID), 3600)

    async def test_no_audio_track_is_user_error(self):
        event = self.media_event()
        await self.db.add_credit(USER_ID, 3600, note="test")
        submission = await self.db.create_submission(USER_ID, "audio", "a.wav")
        progress = await JobProgress.create(event, submission_id=submission)
        job = Job(
            submission_id=submission,
            user_id=USER_ID,
            kind="audio",
            filename="a.gif",
            message=event.message,
            is_admin=False,
            meta={"progress": progress},
        )

        async def no_audio_probe(_path, _settings):
            return MediaInfo(has_audio=False, duration=None)

        with mock.patch("gamas_bot.bot.probe_media", new=no_audio_probe):
            await self.bot._run_job(job)
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "failed")
        self.assertTrue(any("شاخهٔ صوتی" in r for r in event.replies))

    # ------------------------------------------------------------- helpers


class ReserveEstimateTests(unittest.TestCase):
    def test_unknown_duration_gets_conservative_floor(self):
        self.assertEqual(GamasBot._reserve_estimate(None), 600)
        self.assertEqual(GamasBot._reserve_estimate(0), 600)

    def test_duration_rounds_up(self):
        self.assertEqual(GamasBot._reserve_estimate(10.2), 11)


class AdminDetectionTests(unittest.TestCase):
    def test_admin_membership(self):
        bot = GamasBot(make_settings(admin_ids=frozenset({1, 2})))
        self.assertTrue(bot._is_admin(1))
        self.assertFalse(bot._is_admin(3))
        self.assertFalse(bot._is_admin(None))


if __name__ == "__main__":
    unittest.main()
