from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gamas_bot.bot import (
    UNSUPPORTED_PRESENTATION_MESSAGE,
    StudyBot,
    _presentation_metadata,
)
from gamas_bot.stt import Transcript

from support import (
    AUDIO_REL,
    FIXTURES_DIR,
    build_deck,
    make_settings,
    wav_bytes,
)


class FakeEvent:
    """Minimal stand-in for a Telethon NewMessage event."""

    def __init__(self, source: Path):
        self.source = source
        self.replies: list[str] = []
        self.responses: list[str] = []
        self.message = SimpleNamespace(download_media=self._download)

    async def _download(self, file: str) -> str:
        shutil.copyfile(self.source, file)
        return file

    async def reply(self, text: str, **_kwargs) -> None:
        self.replies.append(text)

    async def respond(self, text: str, **_kwargs) -> None:
        self.responses.append(text)


def fake_document(filename: str | None, mime_type: str | None, size: int = 1024):
    return SimpleNamespace(
        document=SimpleNamespace(id=4242),
        voice=None,
        audio=None,
        id=7,
        file=SimpleNamespace(name=filename, mime_type=mime_type, size=size),
    )


class PresentationDetectionTests(unittest.TestCase):
    def test_powerpoint_documents_are_detected(self):
        kind, name, mime, file_id = _presentation_metadata(
            fake_document(
                "lecture.pptx",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
        )
        self.assertEqual(kind, "native")
        self.assertEqual(name, "lecture.pptx")
        self.assertEqual(file_id, "4242")
        self.assertTrue(mime.endswith("presentationml.presentation"))

        self.assertEqual(_presentation_metadata(fake_document("old.ppt", None))[0], "legacy")
        self.assertEqual(_presentation_metadata(fake_document("deck.odp", None))[0], "unsupported")
        self.assertIsNone(_presentation_metadata(fake_document("song.mp3", "audio/mpeg"))[0])
        self.assertIsNone(_presentation_metadata(fake_document("notes.pdf", "application/pdf"))[0])

    def test_voice_messages_are_not_treated_as_decks(self):
        message = fake_document("voice.pptx", None)
        message.voice = object()
        self.assertIsNone(_presentation_metadata(message)[0])


class PresentationJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "bot.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
        )
        self.bot = StudyBot(self.settings)
        await self.bot.db.open()
        self.addCleanup(self.temp.cleanup)

    async def asyncTearDown(self):
        await self.bot.db.close()

    async def _run_job(self, deck: Path, kind: str = "native") -> tuple[FakeEvent, int]:
        user = await self.bot.db.upsert_user(555, "student")
        submission_id = await self.bot.db.create_submission(
            user["id"], "file-1", None, deck.name, None, source_type="pptx"
        )
        event = FakeEvent(deck)
        await self.bot._process_presentation(event, submission_id, deck.name, kind)
        return event, submission_id

    async def test_deck_with_narration_produces_a_booklet(self):
        deck = build_deck(
            self.root / "lecture.pptx",
            slides=[
                {
                    "title": "مقدمه",
                    "bullets": ["تعریف اول"],
                    "notes": "یادآوری امتحان",
                    "media": [("a.wav", wav_bytes(40.0), AUDIO_REL)],
                },
                {
                    "title": "ادامه",
                    "bullets": ["تعریف دوم"],
                    "media": [("b.wav", wav_bytes(20.0), AUDIO_REL)],
                },
            ],
        )
        with patch(
            "gamas_bot.bot.transcribe",
            new=AsyncMock(return_value=Transcript("deepgram", "متن پیاده‌سازی‌شده", 0.93)),
        ) as stt, patch(
            "gamas_bot.bot.structure_presentation", new=AsyncMock(return_value="# جزوهٔ نهایی")
        ) as structuring:
            event, submission_id = await self._run_job(deck)

        stt.assert_awaited_once()
        outline, transcript, _settings = structuring.await_args.args
        self.assertIn("### اسلاید 1 — مقدمه", outline)
        self.assertIn("یادآوری امتحان", outline)
        self.assertEqual(transcript, "متن پیاده‌سازی‌شده")

        self.assertTrue(any("صداها آماده شدند" in item for item in event.responses))
        self.assertTrue(any("۲ فایل صوتی" in item or "2 فایل صوتی" in item for item in event.responses))
        self.assertTrue(any("جزوهٔ نهایی" in item for item in event.responses))

        clips = await self.bot.db.presentation_clips(submission_id)
        self.assertEqual(len(clips), 2)
        self.assertTrue(all(clip["included"] == 1 for clip in clips))
        self.assertEqual([clip["slide_number"] for clip in clips], [1, 2])
        stats = await self.bot.db.stats()
        self.assertEqual(stats["presentations"], 1)
        self.assertEqual(stats["presentation_clips"], 2)
        self.assertEqual(stats["done"], 1)

    async def test_deck_without_audio_falls_back_to_slide_text(self):
        deck = build_deck(
            self.root / "silent.pptx",
            slides=[{"title": "فقط متن", "bullets": ["نکتهٔ مهم"]}],
        )
        with patch("gamas_bot.bot.transcribe", new=AsyncMock()) as stt, patch(
            "gamas_bot.bot.structure_presentation", new=AsyncMock(return_value="جزوهٔ متنی")
        ) as structuring:
            event, submission_id = await self._run_job(deck)

        stt.assert_not_awaited()
        structuring.assert_awaited_once()
        self.assertTrue(any("صدایی در این ارائه پیدا نشد" in item for item in event.responses))
        stats = await self.bot.db.stats()
        self.assertEqual(stats["done"], 1)

    async def test_empty_deck_is_reported_to_the_user(self):
        deck = build_deck(self.root / "empty.pptx", slides=[])
        event, submission_id = await self._run_job(deck)
        self.assertTrue(any("متن یا صدای قابل استفاده‌ای پیدا نکردم" in item for item in event.replies))
        stats = await self.bot.db.stats()
        self.assertEqual(stats["failed"], 1)

    async def test_legacy_deck_is_converted_before_processing(self):
        # A real PowerPoint 97–2003 binary goes through ppt2pptx first; it has
        # no narration, so the booklet is built from its slide text.
        legacy = self.root / "legacy.ppt"
        shutil.copyfile(FIXTURES_DIR / "visual_minimal.ppt", legacy)
        with patch("gamas_bot.bot.transcribe", new=AsyncMock()) as stt, patch(
            "gamas_bot.bot.structure_presentation", new=AsyncMock(return_value="جزوه")
        ) as structuring:
            event, _ = await self._run_job(legacy, kind="legacy")
        self.assertTrue(any("تبدیل می‌شود" in item for item in event.responses))
        self.assertTrue(any("جزوه" in item for item in event.responses))
        stt.assert_not_awaited()
        structuring.assert_awaited_once()
        outline = structuring.await_args.args[0]
        self.assertIn("اسلاید", outline)

    async def test_legacy_deck_is_rejected_when_disabled(self):
        self.bot.settings = make_settings(
            database_path=self.settings.database_path,
            temp_dir=self.settings.temp_dir,
            presentation_legacy_enabled=False,
        )
        event = FakeEvent(self.root / "missing.ppt")
        user = await self.bot.db.upsert_user(556, None)
        await self.bot._accept_presentation(event, user, "legacy", "old.ppt", None, "9")
        self.assertTrue(any("pptx" in item for item in event.replies))

    async def test_odp_deck_is_rejected_with_a_clear_message(self):
        event = FakeEvent(self.root / "deck.odp")
        user = await self.bot.db.upsert_user(557, None)
        await self.bot._accept_presentation(
            event, user, "unsupported", "deck.odp", None, "10"
        )
        self.assertEqual(event.replies, [UNSUPPORTED_PRESENTATION_MESSAGE])
        stats = await self.bot.db.stats()
        self.assertEqual(stats["submissions"], 0)

    async def test_structuring_failure_still_returns_the_material(self):
        deck = build_deck(
            self.root / "fallback.pptx",
            slides=[{"title": "عنوان", "media": [("a.wav", wav_bytes(15.0), AUDIO_REL)]}],
        )
        with patch(
            "gamas_bot.bot.transcribe",
            new=AsyncMock(return_value=Transcript("deepgram", "متن خام صدا", 0.7)),
        ), patch(
            "gamas_bot.bot.structure_presentation",
            new=AsyncMock(side_effect=RuntimeError("gemini down")),
        ):
            event, submission_id = await self._run_job(deck)
        booklet = "\n".join(event.responses)
        self.assertIn("متن خام صدا", booklet)
        self.assertIn("نتوانستم متن را به شکل جزوه مرتب کنم", booklet)
        stats = await self.bot.db.stats()
        self.assertEqual(stats["done"], 1)

    async def test_temporary_files_are_cleaned_up(self):
        deck = build_deck(
            self.root / "cleanup.pptx",
            slides=[{"title": "عنوان", "media": [("a.wav", wav_bytes(15.0), AUDIO_REL)]}],
        )
        with patch("gamas_bot.bot.transcribe", new=AsyncMock(return_value=Transcript("deepgram", "متن", 0.7))), patch(
            "gamas_bot.bot.structure_presentation", new=AsyncMock(return_value="جزوه")
        ):
            await self._run_job(deck)
        self.assertEqual(list(self.settings.temp_dir.glob("deck-*")), [])


if __name__ == "__main__":
    unittest.main()
