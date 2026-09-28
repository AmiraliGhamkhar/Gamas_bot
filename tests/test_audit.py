"""Offline regression tests for the September 2026 repository review."""
from __future__ import annotations

import asyncio
import html
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gamas_bot.bot import StudyBot, _plain_length, render_pages
from gamas_bot.config import Settings
from gamas_bot.database import Database, split_sql_statements
from gamas_bot.media import MediaToolError, parse_probe_output
from gamas_bot.presentations import SlideText, load_presentation, slides_outline
from gamas_bot.stt import Transcript, transcribe
from gamas_bot.structuring import StructuringError, _provider_response, structure_presentation
from support import make_settings


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_directory_exists_before_client_construction(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(session_path=Path(folder) / "new" / "bot")
            bot = StudyBot(settings)
            try:
                self.assertTrue(settings.session_path.parent.is_dir())
            finally:
                await bot.client.disconnect()


class RenderingTests(unittest.TestCase):
    def test_long_escaped_line_preserves_entities_and_characters(self):
        source = "<&>😀" * 3000
        pages = render_pages(source)
        self.assertEqual("".join(html.unescape(page) for page in pages), source)
        self.assertTrue(all(_plain_length(page) <= 4032 for page in pages))
        for page in pages:
            # No partial HTML character reference at a page boundary.
            self.assertNotRegex(page, r"&(amp|lt|gt)?$")

    def test_literal_code_placeholder_cannot_crash_renderer(self):
        pages = render_pages("literal \x009999\x00 and `code`")
        self.assertIn("code", pages[0])


class SettingsTests(unittest.TestCase):
    def test_nonfinite_float_settings_are_rejected(self):
        for name in ("STT_POLL_INTERVAL_SECONDS", "PPTX_MIN_CLIP_SECONDS", "PPTX_SILENCE_SECONDS"):
            for value in ("nan", "inf", "-inf"):
                with self.subTest(name=name, value=value), patch.dict(
                    "os.environ", {name: value}, clear=True
                ):
                    with self.assertRaises(ValueError):
                        Settings.from_env("/nonexistent/gamas.env")


class ProbeTests(unittest.TestCase):
    def test_nonfinite_duration_is_not_accepted(self):
        for duration in ("nan", "inf", "-inf"):
            info = parse_probe_output(json.dumps({
                "has_audio": True, "duration": duration, "audio_codec": "aac",
            }))
            self.assertIsNone(info.duration)

    def test_duration_must_be_a_number_or_null(self):
        info = parse_probe_output(json.dumps({"has_audio": True, "duration": 12.5}))
        self.assertEqual(info.duration, 12.5)
        info = parse_probe_output('{"has_audio": true, "duration": null}')
        self.assertIsNone(info.duration)

    def test_malformed_shape_is_reported_as_media_error(self):
        for payload in ("[]", "null", '{"has_audio": "yes"}', '{"has_audio": 1}'):
            with self.subTest(payload=payload), self.assertRaises(MediaToolError):
                parse_probe_output(payload)


class DatabaseAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_clip_update_does_not_commit_partial_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "bot.sqlite3")
            await db.open()
            try:
                user = await db.upsert_user(7, "student")
                submission = await db.create_submission(user["id"], "f", None, None, None)
                original = [{"part_name": "original.wav", "kind": "audio"}]
                await db.save_presentation_details(submission, 1, original)
                with self.assertRaises(sqlite3.IntegrityError):
                    await db.save_presentation_details(submission, 99, [
                        {"part_name": "replacement.wav", "kind": "audio"},
                        {"part_name": "invalid", "kind": "invalid"},
                    ])
                # An unrelated successful write used to commit the partial replacement.
                await db.upsert_user(8, "other")
                self.assertEqual((await db.presentation_clips(submission))[0]["part_name"], "original.wav")
                cursor = await db._db().execute("SELECT slide_count FROM audio_submissions WHERE id=?", (submission,))
                self.assertEqual((await cursor.fetchone())[0], 1)
            finally:
                await db.close()

    def test_sql_comments_do_not_join_tokens(self):
        db = sqlite3.connect(":memory:")
        try:
            for statement in split_sql_statements("CREATE/* comment */TABLE t (v TEXT); INSERT INTO t VALUES ('a;''b');"):
                db.execute(statement)
            self.assertEqual(db.execute("SELECT v FROM t").fetchone()[0], "a;'b")
        finally:
            db.close()

    async def test_failed_migration_rolls_back_ddl_and_closes_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            migrations = root / "migrations"
            migrations.mkdir()
            (migrations / "001_broken.sql").write_text("CREATE TABLE partial(v); INVALID SQL;", encoding="utf-8")
            db = Database(root / "bot.sqlite3")
            try:
                with patch("gamas_bot.database.MIGRATIONS_DIR", migrations):
                    with self.assertRaises(sqlite3.OperationalError):
                        await db.open()
                self.assertIsNone(db._conn)
                with sqlite3.connect(db.path) as connection:
                    self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone())
            finally:
                await db.close()


class PresentationPreservationTests(unittest.IsolatedAsyncioTestCase):
    def test_default_outline_does_not_drop_later_slides(self):
        slides = [SlideText(i, f"Slide {i}", ("word " * 1000,)) for i in range(1, 9)]
        self.assertIn("Slide 8", slides_outline(slides))

    async def test_long_slide_only_outline_is_fully_sent_to_note_api(self):
        outline = "first " * 4000 + "LAST_SLIDE_MARKER"
        with patch("gamas_bot.structuring._structure_chunk", new=AsyncMock(return_value="notes")) as chunker:
            await structure_presentation(outline, "", make_settings(), max_chars=4000)
        documents = [call.args[0] for call in chunker.await_args_list]
        self.assertIn("LAST_SLIDE_MARKER", "".join(documents))
        self.assertTrue(all(len(document) <= 4000 for document in documents))

    async def test_long_outline_and_audio_both_reach_note_api(self):
        outline = "slide " * 2000 + "LAST_SLIDE_MARKER"
        transcript = "audio " * 2000 + "LAST_AUDIO_MARKER"
        with patch("gamas_bot.structuring._structure_chunk", new=AsyncMock(return_value="notes")) as chunker:
            await structure_presentation(outline, transcript, make_settings(), max_chars=4000)
        documents = [call.args[0] for call in chunker.await_args_list]
        self.assertIn("LAST_SLIDE_MARKER", "".join(documents))
        self.assertIn("LAST_AUDIO_MARKER", "".join(documents))
        self.assertTrue(all(len(document) <= 4000 for document in documents))

    async def test_cancelled_loader_waits_for_extraction_before_cleanup(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def extract(*_args):
            started.set()
            release.wait(3)
            finished.set()

        with patch("gamas_bot.presentations.read_presentation", side_effect=extract):
            task = asyncio.create_task(load_presentation(Path("deck"), Path("media"), make_settings()))
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0.02)
            cancelled_early = task.done()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(cancelled_early)
            self.assertTrue(finished.is_set())


class ProviderSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_timeout_bounds_provider_and_allows_fallback(self):
        async def slow(*_args):
            await asyncio.sleep(0.2)
            return Transcript("speechmatics", "late", 1.0)

        with tempfile.NamedTemporaryFile() as audio, patch("gamas_bot.stt._speechmatics", side_effect=slow), patch(
            "gamas_bot.stt._deepgram", new=AsyncMock(return_value=Transcript("deepgram", "fallback", 1.0))
        ):
            result = await transcribe(Path(audio.name), make_settings(stt_job_timeout=0.03))
        self.assertEqual(result.engine, "deepgram")

    def test_truncated_note_responses_are_not_treated_as_complete(self):
        cases = [
            ("openai_compatible", {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}),
            ("gemini", {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]}),
            ("anthropic", {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]}),
        ]
        for provider, payload in cases:
            with self.subTest(provider=provider), self.assertRaises(StructuringError):
                _provider_response(payload, provider)


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_flood_wait_does_not_resend_already_delivered_pages(self):
        from telethon.errors import FloodWaitError

        bot = StudyBot.__new__(StudyBot)
        bot.db = SimpleNamespace(user_ids=AsyncMock(return_value=[7]), add_broadcast=AsyncMock())
        bot.client = SimpleNamespace(send_message=AsyncMock(side_effect=[None, FloodWaitError(request=None, capture=0), None]))
        with patch("gamas_bot.bot.split_message", return_value=["first", "second"]), patch(
            "gamas_bot.bot.asyncio.sleep", new=AsyncMock()
        ):
            self.assertEqual(await bot._broadcast(1, "text"), 1)
        self.assertEqual([call.args[1] for call in bot.client.send_message.await_args_list], ["first", "second", "second"])


class PresentationVariantTests(unittest.TestCase):
    def test_native_slideshow_and_template_variants_keep_slide_text(self):
        import zipfile
        from support import build_deck
        from gamas_bot.presentations import read_presentation

        main = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
        types = {
            "ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
            "potx": "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
            "ppsm": "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml",
            "potm": "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = build_deck(root / "base.pptx", slides=[{"title": "Lesson", "notes": "Remember"}])
            with zipfile.ZipFile(base) as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            for suffix, content_type in types.items():
                with self.subTest(suffix=suffix):
                    deck = root / f"deck.{suffix}"
                    with zipfile.ZipFile(deck, "w") as archive:
                        for name, payload in members.items():
                            if name == "[Content_Types].xml":
                                payload = payload.replace(main.encode(), content_type.encode())
                            archive.writestr(name, payload)
                    result = read_presentation(deck, root / suffix, make_settings())
                    self.assertEqual(result.slides[0].title, "Lesson")
                    self.assertEqual(result.slides[0].notes, "Remember")
            result = read_presentation(base, root / "no-text", make_settings(presentation_include_slide_text=False))
            self.assertEqual(result.slides, ())
            self.assertEqual(result.slide_count, 1)


class AdminIsolationTests(unittest.IsolatedAsyncioTestCase):
    def make_bot_and_event(self, *, text="group message", private=False):
        bot = StudyBot.__new__(StudyBot)
        bot.settings = make_settings(admin_ids=frozenset({7}))
        bot.db = SimpleNamespace(upsert_user=AsyncMock(return_value={"id": 1, "telegram_id": 7, "is_banned": False}))
        bot._pending_admin_actions = {7: "broadcast"}
        bot._broadcast = AsyncMock(return_value=1)
        event = SimpleNamespace(
            is_private=private, raw_text=text,
            get_sender=AsyncMock(return_value=SimpleNamespace(id=7, username="admin", bot=False)),
            message=SimpleNamespace(file=None, voice=None, audio=None, document=None, media=None),
            reply=AsyncMock(), answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock(),
        )
        return bot, event

    async def test_group_message_cannot_answer_private_broadcast_prompt(self):
        bot, event = self.make_bot_and_event()
        await bot._handle_message(event)
        bot._broadcast.assert_not_awaited()
        self.assertEqual(bot._pending_admin_actions, {7: "broadcast"})

    async def test_group_admin_commands_are_refused(self):
        bot, event = self.make_bot_and_event(text="/broadcast private information")
        await bot._handle_message(event)
        bot._broadcast.assert_not_awaited()
        event.reply.assert_awaited_once()

    async def test_menu_navigation_cancels_stale_admin_prompt(self):
        bot, event = self.make_bot_and_event(private=True)
        event.data = b"menu:create"
        await bot._handle_callback(event)
        self.assertEqual(bot._pending_admin_actions, {})

    async def test_unknown_command_is_not_broadcast(self):
        bot, event = self.make_bot_and_event(text="/unknown", private=True)
        await bot._handle_message(event)
        bot._broadcast.assert_not_awaited()

    async def test_group_admin_callbacks_are_refused(self):
        bot, event = self.make_bot_and_event()
        event.data = b"admin:broadcast"
        await bot._handle_callback(event)
        event.edit.assert_not_awaited()
        event.answer.assert_awaited_once()


class ExtraSafetyTests(unittest.TestCase):
    def test_splitters_reject_nonpositive_limits(self):
        from gamas_bot.bot import split_message
        from gamas_bot.structuring import split_transcript
        for function in (split_message, split_transcript):
            for limit in (0, -1):
                with self.assertRaises(ValueError):
                    function("text", limit)

    def test_gemini_key_is_sent_as_header_not_url_parameter(self):
        from gamas_bot.structuring import _provider_request
        url, headers, _payload, params = _provider_request("text", make_settings(), "prompt")
        self.assertEqual(headers["x-goog-api-key"], "gemini-key")
        self.assertEqual(params, {})
        self.assertNotIn("gemini-key", url)

    def test_no_runtime_path_invokes_system_media_binaries(self):
        from gamas_bot.media import build_probe_command, build_merge_command, build_extract_command
        from gamas_bot.media_worker import INPUT_PROTOCOL_OPTIONS

        commands = [
            build_probe_command(Path("in.mp3")),
            build_extract_command(Path("in.mp3"), Path("out.wav")),
            build_merge_command([Path("a.wav"), Path("b.wav")], Path("out.wav")),
        ]
        for command in commands:
            # Commands run the in-repo Python worker, never ffmpeg/ffprobe/soffice.
            self.assertEqual(command[0], sys.executable)
            self.assertIn("gamas_bot.media_worker", command)
            for argument in command:
                self.assertNotIn(
                    Path(argument).name.lower(), {"ffmpeg", "ffprobe", "soffice"}
                )
        # The worker applies this whitelist to every media input it opens.
        self.assertEqual(INPUT_PROTOCOL_OPTIONS, {"protocol_whitelist": "file,pipe"})

    def test_sql_splitter_keeps_trigger_statements_together(self):
        script = """
        CREATE TABLE t(v);
        CREATE TABLE log(v);
        CREATE TRIGGER record AFTER INSERT ON t BEGIN
          INSERT INTO log VALUES (new.v);
          INSERT INTO log VALUES ('second;value');
        END;
        INSERT INTO t VALUES (1);
        """
        db = sqlite3.connect(":memory:")
        try:
            for statement in split_sql_statements(script):
                db.execute(statement)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM log").fetchone()[0], 2)
        finally:
            db.close()


class STTPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_provider_exception_is_sanitized(self):
        from gamas_bot.stt import STTError
        with tempfile.NamedTemporaryFile() as audio, patch(
            "gamas_bot.stt._speechmatics", new=AsyncMock(side_effect=RuntimeError("SECRET lecture and token"))
        ), self.assertLogs("gamas_bot.stt", level="WARNING") as logs:
            with self.assertRaises(STTError) as caught:
                await transcribe(Path(audio.name), make_settings(stt_fallback_enabled=False))
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("SECRET", " ".join(logs.output))
        self.assertTrue(caught.exception.__suppress_context__)

    async def test_http_error_body_is_not_read_or_logged(self):
        from gamas_bot.stt import STTError, _deepgram
        response = SimpleNamespace(status=400, text=AsyncMock(return_value="PRIVATE"))
        context = AsyncMock()
        context.__aenter__.return_value = response
        session = SimpleNamespace(post=lambda *args, **kwargs: context)
        with tempfile.NamedTemporaryFile() as audio:
            with self.assertRaises(STTError) as caught:
                await _deepgram(session, Path(audio.name), make_settings())
        response.text.assert_not_awaited()
        self.assertIn("HTTP 400", str(caught.exception))


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_file_is_not_mislabeled_as_speechmatics(self):
        import csv
        from unittest.mock import MagicMock
        from scripts.benchmark_stt import run

        sample = MagicMock(spec=Path)
        sample.name = "large.wav"
        sample.suffix = ".wav"
        sample.is_file.return_value = True
        sample.stat.return_value.st_size = 1_000_000_000
        sample.with_suffix.return_value.exists.return_value = False
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "new" / "results.csv"
            with patch("scripts.benchmark_stt.Settings.from_env", return_value=make_settings()), patch(
                "scripts.benchmark_stt.transcribe", new=AsyncMock(return_value=Transcript("deepgram", "text", 1.0))
            ) as stt:
                await run(SimpleNamespace(audio=[sample], output=output))
            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual([(row["engine"], row["status"]) for row in rows], [("speechmatics", "failed"), ("deepgram", "ok")])
        stt.assert_awaited_once()


class PresentationDurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_merge_size_estimate_includes_inserted_silence(self):
        from gamas_bot.media import MediaInfo
        from gamas_bot.presentations import MediaClip, PresentationContent, prepare_audio

        content = PresentationContent((), (
            MediaClip(1, 1, "one.wav", Path("one.wav"), "audio"),
            MediaClip(2, 2, "two.wav", Path("two.wav"), "audio"),
        ))
        with patch("gamas_bot.presentations.probe_media", new=AsyncMock(return_value=MediaInfo(True, 1, "pcm_s16le"))), patch(
            "gamas_bot.presentations.merge_audio_tracks", new=AsyncMock(return_value=Path("out.wav"))
        ) as merge:
            result = await prepare_audio(content, Path("out"), make_settings())
        self.assertEqual(result.total_duration, 2)
        self.assertEqual(merge.await_args.kwargs["total_duration"], 2.5)

    async def test_known_clips_cannot_exceed_cap_even_with_unknown_duration_clip(self):
        from gamas_bot.media import MediaInfo
        from gamas_bot.presentations import MediaClip, PresentationContent, PresentationError, prepare_audio

        content = PresentationContent((), (
            MediaClip(1, 1, "one.wav", Path("one.wav"), "audio"),
            MediaClip(2, 2, "two.wav", Path("two.wav"), "audio"),
        ))
        with patch("gamas_bot.presentations.probe_media", new=AsyncMock(side_effect=[MediaInfo(True, 100), MediaInfo(True, None)])), self.assertRaises(PresentationError):
            await prepare_audio(content, Path("out"), make_settings(presentation_max_total_duration=50))
