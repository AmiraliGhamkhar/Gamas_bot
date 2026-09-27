"""Coverage for direct audio/video uploads, normalisation and unsupported files."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gamas_bot.bot import (
    UNSUPPORTED_FILE_MESSAGE,
    StudyBot,
    _is_unsupported_attachment,
    _media_metadata,
)
from gamas_bot.media import MediaInfo, build_transcode_command, needs_transcode
from gamas_bot.stt import Transcript, deepgram_params, speechmatics_config

from support import fake_media_bytes, make_settings, stub_tools


def fake_message(
    filename=None,
    mime_type=None,
    *,
    voice=False,
    audio=False,
    video=False,
    video_note=False,
    gif=False,
    document=True,
    size=2048,
    media=True,
):
    return SimpleNamespace(
        voice=SimpleNamespace(duration=12) if voice else None,
        audio=object() if audio else None,
        video=object() if video else None,
        video_note=object() if video_note else None,
        gif=object() if gif else None,
        document=SimpleNamespace(id=99) if document else None,
        photo=None,
        media=object() if media else None,
        id=5,
        file=SimpleNamespace(name=filename, mime_type=mime_type, size=size, duration=None)
        if media
        else None,
    )


class MediaDetectionTests(unittest.TestCase):
    def test_audio_sources_are_detected(self):
        self.assertEqual(_media_metadata(fake_message(voice=True))[0], "audio")
        self.assertEqual(_media_metadata(fake_message("class.mp3", "audio/mpeg"))[0], "audio")
        self.assertEqual(_media_metadata(fake_message("class.wma", None))[0], "audio")
        self.assertEqual(_media_metadata(fake_message("class.amr", None))[0], "audio")

    def test_video_sources_are_detected(self):
        self.assertEqual(_media_metadata(fake_message(video=True))[0], "video")
        self.assertEqual(_media_metadata(fake_message(video_note=True))[0], "video")
        self.assertEqual(_media_metadata(fake_message("lecture.mp4", "video/mp4"))[0], "video")
        self.assertEqual(_media_metadata(fake_message("lecture.mkv", None))[0], "video")

    def test_silent_gifs_and_other_files_are_not_media(self):
        self.assertIsNone(_media_metadata(fake_message("anim.mp4", "video/mp4", gif=True))[0])
        self.assertIsNone(_media_metadata(fake_message("notes.pdf", "application/pdf"))[0])
        self.assertIsNone(_media_metadata(fake_message("deck.pptx", None))[0])

    def test_voice_duration_is_reported(self):
        kind, _name, _mime, duration, _file_id = _media_metadata(fake_message(voice=True))
        self.assertEqual(kind, "audio")
        self.assertEqual(duration, 12.0)


class TranscodeDecisionTests(unittest.TestCase):
    def test_known_good_uploads_are_passed_through(self):
        self.assertFalse(needs_transcode("a.mp3", MediaInfo(True, 10, "mp3")))
        self.assertFalse(needs_transcode("a.m4a", MediaInfo(True, 10, "aac")))
        self.assertFalse(needs_transcode("a.wav", MediaInfo(True, 10, "pcm_s16le")))
        self.assertFalse(needs_transcode("a.ogg", MediaInfo(True, 10, "opus")))

    def test_unusual_container_or_codec_is_normalised(self):
        self.assertTrue(needs_transcode("a.wma", MediaInfo(True, 10, "wmav2")))
        self.assertTrue(needs_transcode("a.amr", MediaInfo(True, 10, "amr_nb")))
        self.assertTrue(needs_transcode("a.wav", MediaInfo(True, 10, "adpcm_ms")))

    def test_without_a_probe_report_nothing_is_transcoded(self):
        self.assertFalse(needs_transcode("a.wma", None))
        self.assertFalse(needs_transcode(None, MediaInfo(False, None, None)))

    def test_transcode_command_takes_the_first_audio_track(self):
        command = build_transcode_command(
            "ffmpeg", Path("in.mkv"), Path("out.wav"), output_format="wav"
        )
        self.assertIn("-vn", command)
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")
        self.assertIn("pcm_s16le", command)
        self.assertEqual(command[command.index("-ar") + 1], "16000")
        self.assertEqual(command[-1], "out.wav")

        opus = build_transcode_command(
            "ffmpeg", Path("in.mkv"), Path("out.ogg"), output_format="opus"
        )
        self.assertIn("libopus", opus)
        self.assertEqual(opus[opus.index("-ar") + 1], "48000")


class LanguageSettingTests(unittest.TestCase):
    def test_stt_language_reaches_both_providers(self):
        settings = make_settings(stt_language="en-US")
        self.assertEqual(
            speechmatics_config(settings)["transcription_config"]["language"], "en-US"
        )
        self.assertEqual(deepgram_params(settings)["language"], "en-US")

    def test_default_language_is_persian(self):
        self.assertEqual(deepgram_params(make_settings())["language"], "fa")

    def test_invalid_language_is_rejected(self):
        from gamas_bot.config import Settings

        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
                "SPEECHMATICS_API_KEY=k\nSTT_LANGUAGE=not a language\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=False):
                with self.assertRaises(ValueError):
                    Settings.from_env(env)


class FakeEvent:
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


class MediaJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tools = stub_tools(self.root / "bin")
        self.settings = make_settings(
            database_path=self.root / "bot.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            **self.tools,
        )
        self.bot = StudyBot(self.settings)
        await self.bot.db.open()
        self.addCleanup(self.temp.cleanup)

    async def asyncTearDown(self):
        await self.bot.db.close()

    async def _run(self, name: str, kind: str, payload: bytes | None = None):
        source = self.root / name
        source.write_bytes(payload if payload is not None else fake_media_bytes(45.0))
        user = await self.bot.db.upsert_user(101, "student")
        submission_id = await self.bot.db.create_submission(
            user["id"], "f1", None, name, None, source_type=kind
        )
        event = FakeEvent(source)
        with patch(
            "gamas_bot.bot.transcribe",
            new=AsyncMock(return_value=Transcript("deepgram", "متن درس", 0.9)),
        ) as stt, patch(
            "gamas_bot.bot.structure_transcript", new=AsyncMock(return_value="# جزوه")
        ):
            await self.bot._process_submission(event, submission_id, name, kind)
        return event, stt, submission_id

    async def test_video_upload_is_converted_before_transcription(self):
        event, stt, submission_id = await self._run("lecture.mp4", "video")
        sent_path = Path(stt.await_args.args[0])
        self.assertTrue(sent_path.name.startswith("extracted-audio"))
        self.assertTrue(any("صدای ویدیو را جدا" in item for item in event.responses))
        self.assertTrue(any("جزوه" in item for item in event.responses))
        stats = await self.bot.db.stats()
        self.assertEqual(stats["videos"], 1)
        self.assertEqual(stats["done"], 1)

    async def test_unusual_audio_codec_is_normalised(self):
        _event, stt, _ = await self._run("voice.wma", "audio")
        self.assertTrue(Path(stt.await_args.args[0]).name.startswith("normalised-audio"))

    async def test_standard_audio_is_sent_untouched(self):
        _event, stt, _ = await self._run("class.mp3", "audio")
        self.assertEqual(Path(stt.await_args.args[0]).name, "source.mp3")

    async def test_file_without_audio_track_is_rejected(self):
        event, stt, _ = await self._run(
            "slides.mp4", "video", payload=fake_media_bytes(30.0, has_audio=False)
        )
        stt.assert_not_awaited()
        self.assertTrue(any("صدایی پیدا نکردم" in item for item in event.replies))

    async def test_temporary_files_are_cleaned_up(self):
        await self._run("lecture.mp4", "video")
        self.assertEqual(list(self.settings.temp_dir.glob("submission-*")), [])

    async def test_video_is_refused_when_ffmpeg_is_missing(self):
        self.bot.settings = make_settings(
            database_path=self.settings.database_path,
            temp_dir=self.settings.temp_dir,
            ffprobe_bin=self.tools["ffprobe_bin"],
            ffmpeg_bin=str(self.root / "no-ffmpeg"),
        )
        user = await self.bot.db.upsert_user(102, None)
        event = FakeEvent(self.root / "x")
        event.message.file = SimpleNamespace(size=100)
        await self.bot._accept_media(event, user, "video", "a.mp4", "video/mp4", None, "7")
        self.assertTrue(any("نمی‌توانم ویدیو را پردازش کنم" in i for i in event.replies))
        self.assertEqual((await self.bot.db.stats())["submissions"], 0)


class UnsupportedFileTests(unittest.IsolatedAsyncioTestCase):
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

    async def _handle(self, message) -> FakeEvent:
        event = FakeEvent(self.root)
        event.message = message
        event.raw_text = ""
        event.get_sender = AsyncMock(
            return_value=SimpleNamespace(id=777, username="student", bot=False)
        )
        await self.bot._handle_message(event)
        return event

    async def test_unsupported_document_gets_a_helpful_reply(self):
        event = await self._handle(fake_message("notes.pdf", "application/pdf"))
        self.assertEqual(event.replies, [UNSUPPORTED_FILE_MESSAGE])
        self.assertIn("PowerPoint", event.replies[0])

    async def test_link_previews_are_not_treated_as_files(self):
        from telethon.tl.types import MessageMediaWebPage

        message = fake_message(media=False, document=False)
        message.media = MessageMediaWebPage(webpage=None)
        self.assertFalse(_is_unsupported_attachment(message))
        event = await self._handle(message)
        self.assertEqual(event.replies, [])

    async def test_plain_text_is_still_ignored(self):
        message = fake_message(media=False, document=False)
        event = await self._handle(message)
        self.assertEqual(event.replies, [])


if __name__ == "__main__":
    unittest.main()
