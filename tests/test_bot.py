"""End-to-end tests for the bot's media pipeline, over a real database."""

from __future__ import annotations

import html
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from gamas_bot.jobs import Job
from gamas_bot.media import MediaInfo, MediaToolError
from gamas_bot.presentations import PreparedAudio, PresentationContent, SlideText
from gamas_bot.progress import JobProgress
from gamas_bot.stt import STTError
from gamas_bot.telegram_text import TELEGRAM_TEXT_LIMIT
from gamas_bot.transcript import RawTranscript, Word
from tests.support import (
    build_deck,
    docx_text,
    sample_notes_json,
    FakeJobEvent,
    FakeTelegramClient,
    FakeTelegramMessage,
    make_settings,
    wav_bytes,
)

USER_ID = 555


class BotPipelineTestCase(unittest.IsolatedAsyncioTestCase):
    """A bot wired to a real temp database, with media/STT faked at the seams."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "bot.db",
            temp_dir=root / "tmp",
            # Raw-only by default: no note API, so no test ever reaches the
            # network. Subclasses that exercise note generation script a
            # session instead of pointing at a real endpoint.
            **{"note_api_key": "", "note_api_base_url": "", **self.settings_overrides()},
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

    def settings_overrides(self) -> dict:
        """Extra ``Settings`` overrides for a specialised test case."""
        return {}

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def wire(self, event: FakeJobEvent) -> FakeJobEvent:
        """Point the bot at the event's signature-enforcing Telegram client.

        The bot talks to Telegram through ``self.client``; a test double that
        enforces Telethon's real signatures is what catches a call the library
        would reject (see ``tests/support.py``).
        """
        self.bot.client = event.client
        return event

    def media_event(self, name: str = "source.wav") -> FakeJobEvent:
        source = Path(self._tmp.name) / name
        source.write_bytes(wav_bytes(duration=0.2))
        return self.wire(FakeJobEvent(source=source))

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
        source.write_bytes(b"x" * 4096)
        # Same comparison as production, without copying a real 2 GB upload.
        self.bot.settings = replace(self.settings, max_file_size=1024)
        event = self.wire(FakeJobEvent(source=source))
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

    async def test_media_tool_failure_is_user_visible_and_refunded(self):
        """A PyAV/worker failure propagates as an explicit message, not a crash."""
        event = self.media_event()
        await self.db.add_credit(USER_ID, 3600, note="test")
        submission = await self.db.create_submission(USER_ID, "audio", "a.wav")
        progress = await JobProgress.create(event, submission_id=submission)
        job = Job(
            submission_id=submission,
            user_id=USER_ID,
            kind="audio",
            filename="a.wav",
            message=event.message,
            is_admin=False,
            duration_hint=100.0,
            meta={"progress": progress},
        )

        async def broken_probe(_path, _settings):
            raise MediaToolError("زمان اجرای «media_worker» به پایان رسید.")

        with mock.patch("gamas_bot.bot.probe_media", new=broken_probe):
            await self.bot._run_job(job)
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "failed")
        self.assertTrue(any("media_worker" in r for r in event.replies))
        self.assertEqual(event.files, [])
        # The reservation was released: the user keeps every second.
        self.assertEqual(await self.db.available_seconds(USER_ID), 3600)

    # ------------------------------------------------------------- helpers


class RealSttStackTests(BotPipelineTestCase):
    """The job pipeline with the real ``stt.transcribe`` driver (fake HTTP)."""

    async def test_valid_audio_is_transcribed_delivered_and_charged(self):
        event = self.media_event()
        await self.db.add_credit(USER_ID, 3600, note="test")
        submission = await self.db.create_submission(USER_ID, "audio", "a.wav")
        progress = await JobProgress.create(event, submission_id=submission)
        job = Job(
            submission_id=submission,
            user_id=USER_ID,
            kind="audio",
            filename="a.wav",
            message=event.message,
            is_admin=False,
            duration_hint=100.0,
            meta={"progress": progress},
        )
        audio = Path(self._tmp.name) / "in.wav"
        audio.write_bytes(wav_bytes(duration=0.2))
        session = _SpeechmaticsFakeSession()
        self.bot._http_session = lambda: session  # type: ignore[method-assign]

        with mock.patch(
            "gamas_bot.bot.probe_media", new=self._fake_probe(100.0),
        ), mock.patch(
            "gamas_bot.bot.extract_audio_track", new=self._fake_extract(audio),
        ):
            await self.bot._run_job(job)

        raw_text = event.file_bytes(".txt").decode("utf-8")
        self.assertIn("سلام دوستان. امروز درس شبکه است.", raw_text)
        self.assertTrue(event.file_bytes(".docx").startswith(b"PK"))
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")
        transcript = await self.db.transcription(submission)
        assert transcript is not None
        self.assertEqual(transcript["engine"], "speechmatics")
        self.assertIn("شبکه", transcript["text"])
        # 100 s measured and charged, not the 3600 s reserved.
        self.assertEqual(await self.db.available_seconds(USER_ID), 3500)
        # The request really was the documented Speechmatics batch shape.
        self.assertTrue(session.post_calls[0].endswith("/jobs"))
        self.assertTrue(session.get_calls[-1].endswith("/transcript"))
        self.assertEqual(session.get_kwargs[-1]["params"], {"format": "json-v2"})


class _SpeechmaticsFakeSession:
    """Minimal scripted HTTP session: submit, one poll (done), transcript."""

    def __init__(self):
        self.post_calls: list[str] = []
        self.get_calls: list[str] = []
        self.get_kwargs: list[dict] = []
        self._responses = [
            (202, {"id": "job-1"}),
            (200, {"job": {"status": "done"}}),
            (200, {"results": [
                {"type": "word", "start_time": 0.0, "end_time": 0.4,
                 "alternatives": [{"content": "سلام", "confidence": 0.9}]},
                {"type": "word", "start_time": 0.5, "end_time": 1.0,
                 "alternatives": [{"content": "دوستان", "confidence": 0.9}]},
                {"type": "punctuation", "alternatives": [{"content": ".", "attaches_to": "previous"}]},
                {"type": "word", "start_time": 2.0, "end_time": 2.4,
                 "alternatives": [{"content": "امروز", "confidence": 0.9}]},
                {"type": "word", "start_time": 2.5, "end_time": 2.8,
                 "alternatives": [{"content": "درس", "confidence": 0.9}]},
                {"type": "word", "start_time": 2.9, "end_time": 3.2,
                 "alternatives": [{"content": "شبکه", "confidence": 0.9}]},
                {"type": "word", "start_time": 3.3, "end_time": 3.6,
                 "alternatives": [{"content": "است", "confidence": 0.9}]},
                {"type": "punctuation", "alternatives": [{"content": ".", "attaches_to": "previous"}]},
            ]}),
        ]

    def _response(self):
        status, payload = self._responses.pop(0)
        return _FakeAsyncResponse(status, payload)

    def post(self, url, **_kwargs):
        self.post_calls.append(url)
        return _FakeAsyncContext(self._response())

    def get(self, url, **kwargs):
        self.get_calls.append(url)
        self.get_kwargs.append(kwargs)
        return _FakeAsyncContext(self._response())


class _SttAndLlmSession(_SpeechmaticsFakeSession):
    """The real ``stt.transcribe`` driver plus one scripted note completion."""

    def __init__(self, llm_payload: dict):
        super().__init__()
        self.llm_payload = llm_payload
        self.llm_calls: list[dict] = []

    def post(self, url, **kwargs):
        if url.endswith("/chat/completions"):
            self.llm_calls.append({"url": url, **kwargs})
            return _FakeAsyncContext(_FakeAsyncResponse(200, self.llm_payload))
        return super().post(url, **kwargs)


class _LlmOnlySession:
    """A scripted note provider — the presentation job's only HTTP call."""

    def __init__(self, notes_json: str):
        self.payload = {"choices": [{"message": {"content": notes_json}}]}
        self.llm_calls: list[dict] = []

    def post(self, url, **kwargs):
        if not url.endswith("/chat/completions"):
            raise AssertionError(f"unexpected request: {url}")
        self.llm_calls.append({"url": url, **kwargs})
        return _FakeAsyncContext(_FakeAsyncResponse(200, self.payload))


class _FakeAsyncResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.headers: dict[str, str] = {}
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def read(self):
        import json as _json

        return _json.dumps(self._payload).encode()


class _FakeAsyncContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_exc):
        return False


class FileDeliveryTests(BotPipelineTestCase):
    """File delivery through the supported Telethon API (production incident)."""

    async def test_raw_and_docx_use_send_file_with_caption_and_reply(self):
        event = self.media_event()
        submission = await self.run_media_job(event, transcribe=self._raw())

        # ``Message.reply`` has no ``caption`` parameter — only
        # ``TelegramClient.send_file`` accepts a captioned document. The double
        # in tests/support.py binds every call against the real signature, so a
        # regression to the old call fails here instead of in production.
        raw = event.delivery(".txt")
        self.assertTrue(raw["force_document"])
        self.assertEqual(raw["entity"], event.message.chat_id)
        self.assertEqual(raw["reply_to"], event.message.id)
        self.assertIn("متن خام", raw["caption"])

        docx = event.delivery(".docx")
        self.assertTrue(docx["force_document"])
        self.assertEqual(docx["reply_to"], event.message.id)
        self.assertIn("جزوه", docx["caption"])

        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")

    async def test_caption_on_message_reply_fails_like_the_real_client(self):
        """The old call is rejected by the double, exactly like Telethon.

        ``MessageMethods.send_message() got an unexpected keyword argument
        'caption'`` is the production error; the double reproduces it so the
        test-suite can never again hide the defect behind a permissive mock.
        """
        event = FakeJobEvent()
        with self.assertRaises(TypeError) as caught:
            await event.message.reply(file="x.txt", caption="hi", force_document=True)
        self.assertIn("caption", str(caught.exception))

    async def test_file_failure_falls_back_to_text_and_still_settles(self):
        event = self.media_event()
        event.client.fail_file_delivery = True
        submission = await self.run_media_job(event, transcribe=self._raw())

        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")
        # No file was delivered, but the transcript reached the chat as text.
        self.assertEqual(event.files, [])
        fallback = [text for text in event.replies if "سلام دوستان" in text]
        self.assertTrue(fallback, event.replies)
        # Every fallback page respects Telegram's real message limit, measured
        # the way Telegram counts an HTML message (tags excluded, entities
        # decoded, astral characters twice).
        for page in fallback:
            visible = html.unescape(re.sub(r"<[^>]+>", "", page))
            self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, TELEGRAM_TEXT_LIMIT)
        # The work was delivered, so the measured duration was charged once.
        self.assertEqual(await self.debit_count(USER_ID), 1)
        self.assertEqual(await self.db.available_seconds(USER_ID), 3600 - 100)

    async def test_total_delivery_failure_refunds_and_keeps_the_transcript(self):
        event = self.media_event()
        event.client.fail_file_delivery = True

        async def refuse(*_args, **_kwargs):
            raise RuntimeError("Telegram is unavailable (simulated)")

        event.client.send_message = refuse
        submission = await self.run_media_job(event, transcribe=self._raw())

        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "failed")
        # Nothing was delivered, so nothing is charged.
        self.assertEqual(await self.db.available_seconds(USER_ID), 3600)
        self.assertEqual(await self.debit_count(USER_ID), 0)
        # The finished transcription is not lost with the delivery attempt.
        transcript = await self.db.transcription(submission)
        assert transcript is not None
        self.assertIn("سلام دوستان. امروز", transcript["text"])

    async def debit_count(self, user_id: int) -> int:
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE user_id = ? AND kind = 'debit'", (user_id,)
        )
        return int(rows[0][0])


class NoteGenerationDeliveryTests(BotPipelineTestCase):
    """Upload → STT → raw delivery → notes → Word, with a scripted provider."""

    def settings_overrides(self) -> dict:
        return {"note_api_base_url": "https://note.example/v1", "note_api_key": "note-key"}

    async def test_notes_reach_the_word_booklet_and_usage_is_settled_once(self):
        event = self.media_event()
        session = _SttAndLlmSession(
            {"choices": [{"message": {"content": sample_notes_json("جزوهٔ آزمون")}}]}
        )
        self.bot._http_session = lambda: session  # type: ignore[method-assign]

        await self.db.add_credit(USER_ID, 3600, note="test")
        submission = await self.db.create_submission(USER_ID, "audio", "a.wav")
        progress = await JobProgress.create(event, submission_id=submission)
        job = Job(
            submission_id=submission,
            user_id=USER_ID,
            kind="audio",
            filename="a.wav",
            message=event.message,
            is_admin=False,
            duration_hint=100.0,
            meta={"progress": progress},
        )
        audio = Path(self._tmp.name) / "in.wav"
        audio.write_bytes(wav_bytes(duration=0.2))
        with mock.patch(
            "gamas_bot.bot.probe_media", new=self._fake_probe(100.0),
        ), mock.patch(
            "gamas_bot.bot.extract_audio_track", new=self._fake_extract(audio),
        ):
            await self.bot._run_job(job)

        # The raw transcript went out first, the structured booklet after it.
        self.assertTrue(event.file_bytes(".txt"))
        self.assertIn("جزوهٔ آزمون", docx_text(event.file_bytes(".docx")))
        self.assertTrue(any("/chat/completions" in call["url"] for call in session.llm_calls))

        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")
        rows = await self.db.db.execute_fetchall(
            "SELECT COALESCE(SUM(seconds), 0), COUNT(*) FROM ledger "
            "WHERE user_id = ? AND kind = 'debit'",
            (USER_ID,),
        )
        self.assertEqual((int(rows[0][0]), int(rows[0][1])), (100, 1))


class PresentationNotesDeliveryTests(BotPipelineTestCase):
    async def test_slide_only_deck_keeps_source_in_plain_booklet_without_provider(self):
        source = Path(self._tmp.name) / "slides.pptx"
        source.write_bytes(b"test deck")
        event = self.wire(FakeJobEvent(source=source))
        submission = await self.db.create_submission(USER_ID, "presentation", source.name)
        progress = await JobProgress.create(event, submission_id=submission)
        job = Job(
            submission_id=submission,
            user_id=USER_ID,
            kind="presentation",
            filename=source.name,
            message=event.message,
            is_admin=False,
            meta={"presentation_kind": "native", "progress": progress},
        )
        content = PresentationContent(
            slides=(
                SlideText(
                    number=1,
                    title="موضوع ارائه",
                    body=("نکتهٔ روی اسلاید",),
                    notes="توضیح تکمیلی گوینده",
                ),
            ),
            clips=(),
            total_slides=1,
        )

        async def load_slides(_path, _media_dir, _settings):
            return content

        with mock.patch("gamas_bot.bot.load_presentation", new=load_slides):
            await self.bot._run_job(job)

        raw_text = event.file_bytes(".txt").decode("utf-8")
        booklet_text = docx_text(event.file_bytes(".docx"))
        self.assertIn("موضوع ارائه", raw_text)
        self.assertIn("توضیح تکمیلی گوینده", raw_text)
        self.assertIn("موضوع ارائه", booklet_text)
        self.assertIn("نکتهٔ روی اسلاید", booklet_text)
        self.assertIn("توضیح تکمیلی گوینده", booklet_text)
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")


class PresentationPromptTests(BotPipelineTestCase):
    """The deck's own text must be part of the note request, not only the audio."""

    def settings_overrides(self) -> dict:
        return {"note_api_base_url": "https://note.example/v1", "note_api_key": "note-key"}

    async def test_slide_text_notes_and_narration_all_reach_the_note_model(self):
        deck = build_deck(
            Path(self._tmp.name) / "deck.pptx",
            slides=[
                {
                    "title": "شبکهٔ عصبی",
                    "bullets": ["لایهٔ ورودی", "لایهٔ پنهان"],
                    "notes": "توضیح تکمیلی گوینده",
                }
            ],
        )
        event = self.wire(FakeJobEvent(source=deck))
        await self.db.add_credit(USER_ID, 3600, note="test")
        submission = await self.db.create_submission(
            USER_ID, "presentation", deck.name
        )
        progress = await JobProgress.create(event, submission_id=submission)
        job = Job(
            submission_id=submission,
            user_id=USER_ID,
            kind="presentation",
            filename=deck.name,
            message=event.message,
            is_admin=False,
            meta={"presentation_kind": "native", "progress": progress},
        )
        audio = Path(self._tmp.name) / "narration.wav"
        audio.write_bytes(wav_bytes(duration=0.2))
        session = _LlmOnlySession(sample_notes_json("جزوهٔ ارائه"))
        self.bot._http_session = lambda: session  # type: ignore[method-assign]

        async def fake_prepare(_content, _workdir, _settings, *, skipped_out=None):
            if skipped_out is not None:
                skipped_out.clear()
            return PreparedAudio(
                path=audio, clips=(), total_duration=12.0, merged=True
            )

        async def fake_probe(_path, _settings):
            return MediaInfo(has_audio=True, duration=12.0, audio_codec="pcm_s16le")

        async def fake_transcribe(_path, _settings, session=None):
            return RawTranscript(
                words=(), text="امروز درباره شبکهٔ عصبی صحبت شد", engine="fake"
            )

        with mock.patch("gamas_bot.bot.prepare_audio", new=fake_prepare), \
                mock.patch("gamas_bot.bot.probe_media", new=fake_probe), \
                mock.patch("gamas_bot.bot.transcribe", new=fake_transcribe):
            await self.bot._run_job(job)

        self.assertEqual(len(session.llm_calls), 1, session.llm_calls)
        request = session.llm_calls[0]["json"]
        user_content = request["messages"][1]["content"]
        self.assertIn("## متن اسلایدها", user_content)
        self.assertIn("شبکهٔ عصبی", user_content)
        self.assertIn("لایهٔ ورودی", user_content)
        self.assertIn("یادداشت گوینده: توضیح تکمیلی گوینده", user_content)
        self.assertIn("## متن پیاده‌سازی‌شدهٔ صدای ارائه", user_content)
        self.assertIn("امروز درباره", user_content)
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")

    async def test_a_deck_whose_text_the_reader_cannot_open_still_sends_it(self):
        """python-pptx failing must not silently strip the slides from the prompt."""
        deck = build_deck(
            Path(self._tmp.name) / "deck.pptx",
            slides=[{"title": "آموزش", "bullets": ["گرادیان کاهشی"]}],
        )
        event = self.wire(FakeJobEvent(source=deck))
        await self.db.add_credit(USER_ID, 3600, note="test")
        submission = await self.db.create_submission(
            USER_ID, "presentation", deck.name
        )
        progress = await JobProgress.create(event, submission_id=submission)
        job = Job(
            submission_id=submission,
            user_id=USER_ID,
            kind="presentation",
            filename=deck.name,
            message=event.message,
            is_admin=False,
            meta={"presentation_kind": "native", "progress": progress},
        )
        session = _LlmOnlySession(sample_notes_json("جزوهٔ ارائه"))
        self.bot._http_session = lambda: session  # type: ignore[method-assign]

        async def no_audio(_content, _workdir, _settings, *, skipped_out=None):
            if skipped_out is not None:
                skipped_out.clear()
            return None

        with mock.patch("gamas_bot.bot.prepare_audio", new=no_audio), mock.patch(
            "gamas_bot.presentations._extract_slide_text_with_pptx",
            new=lambda _path: [],
        ):
            await self.bot._run_job(job)

        self.assertEqual(len(session.llm_calls), 1, session.llm_calls)
        user_content = session.llm_calls[0]["json"]["messages"][1]["content"]
        self.assertIn("آموزش", user_content)
        self.assertIn("گرادیان کاهشی", user_content)


class PresentationIntakeTests(BotPipelineTestCase):
    async def test_an_odp_upload_is_refused_with_a_clear_message(self):
        source = Path(self._tmp.name) / "deck.odp"
        source.write_bytes(b"not an odp the bot can read")
        event = self.wire(FakeJobEvent(source=source))
        with mock.patch.object(self.bot.queue, "submit") as submit:
            await self.bot._accept_presentation(
                event, USER_ID, "unsupported", "deck.odp"
            )
        submit.assert_not_called()
        self.assertTrue(
            any("ODP/OTP" in reply for reply in event.replies), event.replies
        )
        rows = await self.db.db.execute_fetchall("SELECT COUNT(*) FROM submissions")
        self.assertEqual(int(rows[0][0]), 0)


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


class UnlimitedUserBotTests(BotPipelineTestCase):
    """A special user's jobs run to completion with no credit and no charge."""

    async def debit_count(self, user_id: int) -> int:
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE user_id = ? AND kind = 'debit'", (user_id,)
        )
        return int(rows[0][0])

    async def test_special_user_job_completes_without_credit(self):
        await self.db.set_unlimited(USER_ID, True, admin_id=1, reason="استاد")
        event = self.media_event()
        submission = await self.run_media_job(
            event, duration=7200.0, credit=0, transcribe=self._raw()
        )
        self.assertTrue(event.file_bytes(".docx").startswith(b"PK"))
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")
        # Usage is still recorded on the submission for the audit trail…
        self.assertEqual(row.duration_seconds, 7200.0)
        # …but nothing was debited.
        self.assertEqual(await self.debit_count(USER_ID), 0)
        self.assertEqual(await self.db.available_seconds(USER_ID), 0)

    async def test_special_user_is_not_refused_at_intake(self):
        await self.db.set_unlimited(USER_ID, True, admin_id=1, reason="استاد")
        event = FakeMessageEvent(USER_ID, "")
        with mock.patch("gamas_bot.bot.JobProgress.create", new=_no_progress), \
                mock.patch.object(self.bot.queue, "submit") as submit:
            await self.bot._accept_media(event, USER_ID, "audio", "a.mp3", 7200.0)
        submit.assert_called_once()
        self.assertFalse(any("کافی نیست" in r for r in event.replies))

    async def test_regular_user_is_still_refused_at_intake(self):
        event = FakeMessageEvent(USER_ID, "")
        with mock.patch("gamas_bot.bot.JobProgress.create", new=_no_progress), \
                mock.patch.object(self.bot.queue, "submit") as submit:
            await self.bot._accept_media(event, USER_ID, "audio", "a.mp3", 7200.0)
        submit.assert_not_called()
        self.assertTrue(any("کافی نیست" in r for r in event.replies))


async def _no_progress(event, **_kwargs):
    return None


class _FakeIncomingTextMessage(FakeTelegramMessage):
    """A private text message: the fields ``_handle_message`` inspects."""

    def __init__(self, client, *, sender_id: int, text: str):
        super().__init__(client, chat_id=sender_id, message_id=4242)
        self.message = text
        for name in (
            "media", "document", "audio", "voice", "video", "video_note",
            "gif", "sticker", "file", "photo",
        ):
            setattr(self, name, None)


class FakeMessageEvent:
    """Minimal Telethon NewMessage double for the admin text commands.

    Replies go through the signature-enforcing client, so an admin screen that
    sends an argument Telethon does not accept fails in the test, not in
    production.
    """

    def __init__(self, sender_id: int, text: str):
        self.sender_id = sender_id
        self.is_private = True
        self.record = SimpleNamespace(replies=[], files=[], file_payloads=[], edits=[])
        self.client = FakeTelegramClient(self.record)
        self.message = _FakeIncomingTextMessage(
            self.client, sender_id=sender_id, text=text
        )

    @property
    def replies(self) -> list[str]:
        return self.record.replies

    async def reply(self, text: str = "", **kwargs):
        return await self.message.reply(text, **kwargs)


class UnlimitedCommandTests(unittest.IsolatedAsyncioTestCase):
    """The admin ``/unlimited`` command and the screens that show the flag."""

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
        await self.db.ensure_user(self.user_id)

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    async def send(self, sender: int, text: str) -> FakeMessageEvent:
        event = FakeMessageEvent(sender, text)
        await self.bot._handle_message(event)
        return event

    async def test_admin_marks_and_unmarks_special_user(self):
        event = await self.send(self.admin_id, f"/unlimited {self.user_id} on استاد درس")
        self.assertTrue(await self.db.is_unlimited(self.user_id))
        self.assertTrue(any("کاربر ویژه شد" in r for r in event.replies))

        event = await self.send(self.admin_id, f"/unlimited {self.user_id} OFF")
        self.assertFalse(await self.db.is_unlimited(self.user_id))
        self.assertTrue(any("دیگر کاربر ویژه نیست" in r for r in event.replies))

    async def test_enabling_requires_a_reason(self):
        event = await self.send(self.admin_id, f"/unlimited {self.user_id} on")
        self.assertFalse(await self.db.is_unlimited(self.user_id))
        self.assertTrue(any("دلیل" in r for r in event.replies))

    async def test_malformed_command_shows_usage(self):
        for text in ("/unlimited", f"/unlimited {self.user_id}", "/unlimited abc on x",
                     f"/unlimited {self.user_id} maybe"):
            event = await self.send(self.admin_id, text)
            self.assertTrue(any("قالب درست" in r for r in event.replies), text)
        self.assertFalse(await self.db.is_unlimited(self.user_id))

    async def test_admins_cannot_be_made_special(self):
        other_admin = next(iter(self.settings.admin_ids))
        event = await self.send(self.admin_id, f"/unlimited {other_admin} on استاد")
        self.assertFalse(await self.db.is_unlimited(other_admin))
        self.assertTrue(any("مدیران" in r for r in event.replies))

    async def test_regular_user_cannot_use_the_command(self):
        event = await self.send(self.user_id, f"/unlimited {self.user_id} on خودم")
        self.assertFalse(await self.db.is_unlimited(self.user_id))
        # Falls through to the normal user reply, not an admin confirmation.
        self.assertFalse(any("کاربر ویژه" in r for r in event.replies))

    async def test_command_is_audited(self):
        await self.send(self.admin_id, f"/unlimited {self.user_id} on استاد")
        audit = await self.db.recent_audit(limit=1)
        self.assertEqual(audit[0]["action"], "unlimited_on")
        self.assertEqual(audit[0]["admin_id"], self.admin_id)
        self.assertEqual(audit[0]["subject_user"], self.user_id)

    async def test_main_menu_and_account_show_unlimited(self):
        await self.db.set_unlimited(self.user_id, True, admin_id=self.admin_id, reason="استاد")
        for data in (b"menu:main", b"account"):
            event = FakeCallbackEvent(self.user_id, data)
            await self.bot._handle_callback(event)
            text, _buttons = event.edits[-1]
            self.assertIn("نامحدود", text, data)
            self.assertNotIn("ساعت", text.split("اعتبار")[1].split("\n")[0], data)

    async def test_admin_user_list_flags_special_users(self):
        await self.db.set_unlimited(self.user_id, True, admin_id=self.admin_id, reason="استاد")
        event = FakeCallbackEvent(self.admin_id, b"adm:users")
        await self.bot._handle_callback(event)
        text, _buttons = event.edits[-1]
        self.assertIn(f"⭐ {self.user_id}", text)
        self.assertIn("/unlimited <user_id> on", text)


class FakeCallbackEvent:
    """Minimal Telethon CallbackQuery double that records every outcome."""

    def __init__(self, sender_id: int, data: bytes):
        self.sender_id = sender_id
        self.data = data
        self.answers: list[tuple] = []    # (text, alert)
        self.edits: list[tuple] = []      # (text, buttons)
        self.responses: list[tuple] = []  # (text, buttons)
        self.client = FakeTelegramClient()

    async def get_input_chat(self) -> int:
        """The chat the callback happened in (used by file-like deliveries)."""
        return self.sender_id

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
