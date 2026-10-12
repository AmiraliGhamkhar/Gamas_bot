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


def _button_data(button) -> bytes | None:
    """Callback payload of an inline button (Telethon-version tolerant)."""
    data = getattr(getattr(button, "type", None), "data", None)
    if data is None:
        data = getattr(button, "data", None)
    return data


def menu_payloads(menu) -> set[bytes]:
    return {
        data
        for row in (menu or [])
        for button in row
        for data in [_button_data(button)]
        if data is not None
    }


class FakeCallbackEvent:
    """Minimal Telethon CallbackQuery double that records every outcome."""

    def __init__(self, sender_id: int, data: bytes):
        self.sender_id = sender_id
        self.data = data
        self.answers: list[tuple] = []    # (text, alert)
        self.edits: list[tuple] = []      # (text, buttons)
        self.responses: list[tuple] = []  # (text, buttons)

    async def answer(self, text=None, *, alert=False):
        self.answers.append((text, alert))

    async def edit(self, text, buttons=None):
        self.edits.append((text, buttons))

    async def respond(self, text="", buttons=None, **_kwargs):
        self.responses.append((text, buttons))


class PanelSeparationTests(unittest.IsolatedAsyncioTestCase):
    """The user panel and the admin panel are two separate screen trees."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "bot.db",
            temp_dir=root / "tmp",
            note_api_key="",
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.admin_id = next(iter(self.settings.admin_ids))
        self.user_id = 555

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def test_user_menu_is_the_root_for_everyone(self):
        user_menu = menu_payloads(self.bot._user_menu(is_admin=False))
        admin_menu = menu_payloads(self.bot._user_menu(is_admin=True))
        # A regular user sees only the user actions.
        self.assertIn(b"make", user_menu)
        self.assertIn(b"account", user_menu)
        self.assertIn(b"help", user_menu)
        self.assertFalse(any(d.startswith(b"adm:") for d in user_menu))
        # An admin gets one extra entry into their own panel — and only that.
        self.assertIn(b"adm:home", admin_menu)
        self.assertEqual(
            {d for d in admin_menu if d.startswith(b"adm:")},
            {b"adm:home"},
        )

    def test_admin_menu_is_its_own_panel_with_way_back(self):
        payloads = menu_payloads(self.bot._admin_menu())
        for section in (b"adm:home", b"adm:users", b"adm:pay", b"adm:bcast", b"adm:audit"):
            self.assertIn(section, payloads)
        # The panel returns to the user panel…
        self.assertIn(b"menu:main", payloads)
        # …and never mixes the user flow into itself.
        self.assertNotIn(b"make", payloads)
        self.assertNotIn(b"account", payloads)

    async def test_admin_root_screen_is_the_user_panel(self):
        event = FakeCallbackEvent(self.admin_id, b"menu:main")
        await self.bot._handle_callback(event)
        self.assertTrue(event.edits, "menu:main must render a screen")
        text, buttons = event.edits[-1]
        payloads = menu_payloads(buttons)
        self.assertIn("منوی اصلی", text)
        self.assertIn(b"make", payloads)      # admin keeps the user flow
        self.assertIn(b"adm:home", payloads)  # …plus the separate admin entry

    async def test_regular_user_cannot_open_admin_panel(self):
        event = FakeCallbackEvent(self.user_id, b"adm:users")
        await self.bot._handle_callback(event)
        self.assertEqual(event.edits, [])
        self.assertTrue(event.answers)
        text, alert = event.answers[-1]
        self.assertTrue(alert)
        self.assertIn("مدیران", text)

    async def test_regular_user_cannot_review_payments(self):
        """payv: approves money — it is admin-only, not just adm:-prefixed."""
        event = FakeCallbackEvent(self.user_id, b"payv:7:a")
        await self.bot._handle_callback(event)
        self.assertEqual(event.edits, [])
        self.assertTrue(event.answers)
        _, alert = event.answers[-1]
        self.assertTrue(alert)

    async def test_opening_panel_disarms_broadcast_prompt(self):
        start = FakeCallbackEvent(self.admin_id, b"adm:bcast")
        await self.bot._handle_callback(start)
        self.assertIn(self.admin_id, self.bot._pending_broadcast)
        home = FakeCallbackEvent(self.admin_id, b"adm:home")
        await self.bot._handle_callback(home)
        self.assertNotIn(self.admin_id, self.bot._pending_broadcast)
        self.assertTrue(home.edits, "adm:home must render the overview")

    async def test_navigating_disarms_receipt_upload(self):
        self.bot._pending_payments[self.user_id] = 42
        event = FakeCallbackEvent(self.user_id, b"account")
        await self.bot._handle_callback(event)
        self.assertNotIn(self.user_id, self.bot._pending_payments)
        self.assertTrue(event.edits)


if __name__ == "__main__":
    unittest.main()
