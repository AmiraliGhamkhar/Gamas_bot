"""Regression coverage for bugs found while auditing the bot end to end."""

from __future__ import annotations

import html
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gamas_bot.bot import (
    TELEGRAM_TEXT_LIMIT,
    StudyBot,
    _is_unsupported_attachment,
    _plain_length,
    markdown_to_telegram_html,
    render_pages,
    split_message,
)
from gamas_bot.config import Settings
from gamas_bot.database import Database, split_sql_statements
from gamas_bot.media import build_merge_command, needs_transcode, MediaInfo
from gamas_bot.presentations import natural_key, prepare_audio, read_presentation
from gamas_bot.stt import Transcript, transcribe

from support import AUDIO_REL, build_deck, fake_media_bytes, make_settings, stub_tools


def plain_text(rendered: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", rendered))


class MarkdownRenderingTests(unittest.TestCase):
    def test_underscores_inside_words_are_not_emphasis(self):
        rendered = markdown_to_telegram_html("• @my_user_name | فایل: lecture_01_final.mp3")
        self.assertNotIn("<i>", rendered)
        self.assertIn("@my_user_name", rendered)
        self.assertIn("lecture_01_final.mp3", rendered)

    def test_multiplication_signs_are_not_emphasis(self):
        self.assertEqual(markdown_to_telegram_html("2 * 3 * 4 = 24"), "2 * 3 * 4 = 24")

    def test_real_emphasis_still_renders(self):
        rendered = markdown_to_telegram_html("**پررنگ** و *کج* و _کج دوم_")
        self.assertIn("<b>پررنگ</b>", rendered)
        self.assertEqual(rendered.count("<i>"), 2)

    def test_table_header_without_rows_is_not_lost(self):
        rendered = markdown_to_telegram_html("| درس | زمان |\n|---|---|")
        self.assertIn("درس", rendered)
        self.assertIn("زمان", rendered)

    def test_short_separator_rows_are_recognised(self):
        rendered = markdown_to_telegram_html("| a | b |\n|--|-:|\n| 1 | 2 |")
        self.assertEqual(rendered, "<b>a:</b> 1 · <b>b:</b> 2")

    def test_table_state_does_not_leak_into_later_lines(self):
        rendered = markdown_to_telegram_html("| a | b |\n|-|-|\n| 1 | 2 |\nپایان")
        self.assertTrue(rendered.endswith("پایان"))


class PaginationTests(unittest.TestCase):
    def test_pages_respect_the_telegram_limit_after_rendering(self):
        # Tables repeat their header on every row, so the rendered text is much
        # longer than its Markdown source and used to overflow Telegram's limit.
        source = "\n".join(
            ["| درس ریاضیات پیشرفته | زمان مطالعهٔ روزانه |", "|---|---|"]
            + [f"| جبر خطی {index} | دو ساعت |" for index in range(400)]
        )
        pages = render_pages(source)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(_plain_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages))

    def test_no_row_is_lost_or_duplicated_when_a_table_is_split(self):
        source = "\n".join(
            ["| ستون | مقدار |", "|---|---|"]
            + [f"| ردیف {index} | مقدار {index} |" for index in range(300)]
        )
        joined = plain_text("\n".join(render_pages(source)))
        for index in range(300):
            self.assertEqual(joined.count(f"ردیف {index} "), 1)
        # Every row keeps the real header as its label.
        self.assertEqual(joined.count("ستون:"), 300)

    def test_ordinary_notes_survive_pagination(self):
        source = "\n\n".join(
            f"## بخش {index}\n\n" + "این یک جملهٔ آزمایشی فارسی است. " * 30
            for index in range(30)
        )
        pages = render_pages(source)
        joined = plain_text("\n".join(pages))
        self.assertTrue(all(_plain_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages))
        self.assertTrue(all(f"بخش {index}" in joined for index in range(30)))

    def test_one_huge_line_is_cut_without_breaking_markup(self):
        pages = render_pages("**" + "کلمه " * 4000 + "**")
        self.assertTrue(all(_plain_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages))
        for page in pages:
            self.assertEqual(page.count("<"), page.count(">"))

    def test_emoji_pages_are_measured_in_utf16_units(self):
        pages = render_pages("😀" * 5000)
        self.assertTrue(all(_plain_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages))

    def test_split_message_terminates_with_a_tiny_limit(self):
        pages = split_message("😀😀😀", limit=1)
        self.assertEqual("".join(pages), "😀😀😀")


class AttachmentTests(unittest.TestCase):
    @staticmethod
    def _message(**overrides):
        base = {"media": object(), "document": SimpleNamespace(id=1), "photo": None}
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_stickers_and_animations_do_not_trigger_the_error_reply(self):
        self.assertFalse(_is_unsupported_attachment(self._message(sticker=object())))
        self.assertFalse(_is_unsupported_attachment(self._message(gif=object())))

    def test_real_documents_still_trigger_it(self):
        self.assertTrue(_is_unsupported_attachment(self._message()))


class TranscodeDecisionTests(unittest.TestCase):
    def test_unnamed_opus_upload_is_not_re_encoded(self):
        # Telegram voice notes arrive without a filename; the stored copy keeps
        # the .ogg suffix and must be passed through untouched.
        self.assertTrue(needs_transcode(None, MediaInfo(True, 10, "opus")))
        self.assertFalse(needs_transcode("source.ogg", MediaInfo(True, 10, "opus")))


class MergeCommandTests(unittest.TestCase):
    def test_merge_pins_the_first_audio_stream_of_every_input(self):
        command = build_merge_command(
            "ffmpeg", [Path("a.mp4"), Path("b.m4a")], Path("out.wav")
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("[0:a:0]", graph)
        self.assertIn("[1:a:0]", graph)


class MediaOrderingTests(unittest.TestCase):
    def test_media_files_sort_numerically(self):
        names = ["ppt/media/media10.m4a", "ppt/media/media2.m4a", "ppt/media/media1.m4a"]
        self.assertEqual(
            sorted(names, key=natural_key),
            ["ppt/media/media1.m4a", "ppt/media/media2.m4a", "ppt/media/media10.m4a"],
        )

    def test_names_starting_with_a_digit_do_not_break_sorting(self):
        sorted(["1a.mp3", "b2.mp3", "10.mp3", "a.mp3"], key=natural_key)


class SettingsRobustnessTests(unittest.TestCase):
    def test_blank_values_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
                "SPEECHMATICS_API_KEY=k\n"
                "SPEECHMATICS_BASE_URL=\nDEEPGRAM_MODEL=\nGEMINI_MODEL=\n"
                "DATABASE_PATH=\nMAX_CONCURRENT_JOBS=\nSTT_PRIMARY=\nLOG_LEVEL=\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                settings = Settings.from_env(env)
        self.assertEqual(
            settings.speechmatics_base_url, "https://eu1.asr.api.speechmatics.com/v2"
        )
        self.assertEqual(settings.deepgram_model, "nova-3")
        self.assertEqual(settings.gemini_model, "gemini-2.5-flash-lite")
        self.assertEqual(settings.database_path, Path("data/bot.sqlite3"))
        self.assertEqual(settings.max_concurrent_jobs, 3)
        self.assertEqual(settings.stt_primary, "speechmatics")
        self.assertEqual(settings.log_level, "INFO")


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def test_statement_splitter_ignores_comments_and_strings(self):
        statements = split_sql_statements(
            "-- توضیح؛ با نقطه‌ویرگول\n"
            "ALTER TABLE t ADD COLUMN c TEXT NOT NULL DEFAULT 'a;b';\n"
            "/* بلوکی; */\nCREATE INDEX IF NOT EXISTS i ON t(c);\n"
        )
        self.assertEqual(len(statements), 2)
        self.assertIn("'a;b'", statements[0])
        self.assertTrue(statements[1].startswith("CREATE INDEX"))

    async def test_migrations_can_be_replayed_on_an_existing_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot.sqlite3"
            first = Database(path)
            await first.open()
            async with first._lock:
                await first._db().execute("DELETE FROM schema_migrations")
                await first._db().commit()
            await first.close()

            # A half-applied migration (or a database predating the bookkeeping
            # table) must not wedge startup with "duplicate column name".
            second = Database(path)
            await second.open()
            user = await second.upsert_user(1, "again")
            self.assertEqual(
                await second.create_submission(user["id"], "f", None, None, None), 1
            )
            await second.close()


class STTRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_without_a_key_is_replaced_even_without_fallback(self):
        settings = make_settings(
            stt_primary="speechmatics",
            speechmatics_api_key=None,
            stt_fallback_enabled=False,
        )
        with tempfile.NamedTemporaryFile() as audio:
            with patch(
                "gamas_bot.stt._deepgram",
                new=AsyncMock(return_value=Transcript("deepgram", "متن", 0.9)),
            ) as deepgram:
                result = await transcribe(Path(audio.name), settings)
        deepgram.assert_awaited_once()
        self.assertEqual(result.engine, "deepgram")


class PreparedAudioReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_skip_reasons_survive_when_no_clip_is_usable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = make_settings(**stub_tools(root / "bin"))
            deck = build_deck(
                root / "deck.pptx",
                slides=[
                    {
                        "title": "یک",
                        "media": [("a.m4a", fake_media_bytes(0.1), AUDIO_REL)],
                    }
                ],
            )
            content = read_presentation(deck, root / "media", settings)
            reasons: list[str] = []
            prepared = await prepare_audio(
                content, root / "work", settings, skipped_out=reasons
            )
        self.assertIsNone(prepared)
        self.assertTrue(any("کوتاه‌تر" in reason for reason in reasons))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_note_with_a_big_table_is_delivered_in_valid_pages(self):
        sent: list[str] = []

        class Event:
            async def respond(self, text, **_kwargs):
                sent.append(text)

        settings = make_settings()
        bot = StudyBot.__new__(StudyBot)
        bot.settings = settings
        note = "\n".join(
            ["## جدول", "", "| موضوع درس امروز | توضیح کامل |", "|---|---|"]
            + [f"| موضوع {index} | توضیح {index} |" for index in range(250)]
        )
        with patch("gamas_bot.bot.asyncio.sleep", new=AsyncMock()):
            await StudyBot._send_long_message(bot, Event(), note)
        self.assertGreater(len(sent), 1)
        for page in sent:
            self.assertLessEqual(_plain_length(page), TELEGRAM_TEXT_LIMIT)
            self.assertEqual(page.count("<b>"), page.count("</b>"))


class StaleWorkdirTests(unittest.TestCase):
    def test_leftover_job_folders_are_removed_at_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            temp_dir = Path(folder) / "tmp"
            temp_dir.mkdir()
            (temp_dir / "submission-1-abc").mkdir()
            (temp_dir / "deck-2-xyz").mkdir()
            (temp_dir / "keep-me").mkdir()
            keep_file = temp_dir / "submission-note.txt"
            keep_file.write_text("x", encoding="utf-8")

            bot = StudyBot.__new__(StudyBot)
            bot.settings = make_settings(temp_dir=temp_dir)
            bot._clean_stale_workdirs()

            self.assertEqual(
                sorted(item.name for item in temp_dir.iterdir()),
                ["keep-me", "submission-note.txt"],
            )


class TableCellPreservationTests(unittest.TestCase):
    """A malformed table row must never lose a cell."""

    def test_extra_cells_are_kept(self):
        rendered = markdown_to_telegram_html(
            "| نام | مقدار |\n|---|---|\n| الف | ب | ج |"
        )
        self.assertIn("ج", plain_text(rendered))

    def test_empty_header_label_does_not_render_a_stray_colon(self):
        rendered = markdown_to_telegram_html("|  | مقدار |\n|---|---|\n| الف | ب |")
        self.assertNotIn("<b>:</b>", rendered)
        self.assertIn("الف", plain_text(rendered))


class AdminMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_replies_keep_the_admin_button_for_admins(self):
        seen: list = []

        class Event:
            async def respond(self, text, parse_mode=None, buttons=None):
                seen.append(buttons)

        bot = StudyBot.__new__(StudyBot)
        with patch("gamas_bot.bot.asyncio.sleep", new=AsyncMock()):
            await StudyBot._send_long_message(bot, Event(), "سلام", is_admin=True)
            await StudyBot._send_long_message(bot, Event(), "سلام", is_admin=False)
        def callbacks(rows):
            return [button.type.data for row in rows for button in row]

        admin_rows, user_rows = seen
        self.assertIn(b"admin:home", callbacks(admin_rows))
        self.assertNotIn(b"admin:home", callbacks(user_rows))


class PendingAdminActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_captioned_upload_is_not_read_as_an_admin_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(
                database_path=Path(folder) / "bot.sqlite3",
                temp_dir=Path(folder) / "tmp",
                admin_ids=frozenset({7}),
            )
            bot = StudyBot.__new__(StudyBot)
            bot.settings = settings
            bot.db = Database(settings.database_path)
            bot._pending_admin_actions = {7: "broadcast"}
            await bot.db.open()

            accepted: list = []

            async def accept_media(event, user, *args):
                accepted.append(args[0])

            bot._accept_media = accept_media
            sender = SimpleNamespace(id=7, username="admin", bot=False)
            message = SimpleNamespace(
                voice=SimpleNamespace(duration=10),
                audio=None,
                video=None,
                video_note=None,
                gif=None,
                document=SimpleNamespace(id=3),
                photo=None,
                media=object(),
                id=1,
                file=SimpleNamespace(
                    name="lecture.ogg", mime_type="audio/ogg", size=10, duration=10
                ),
            )
            event = SimpleNamespace(
                raw_text="سلام",
                message=message,
                get_sender=AsyncMock(return_value=sender),
                reply=AsyncMock(),
                respond=AsyncMock(),
            )
            broadcast = AsyncMock(return_value=0)
            bot._broadcast = broadcast
            await bot._handle_message(event)
            await bot.db.close()

            broadcast.assert_not_awaited()
            self.assertEqual(accepted, ["audio"])
            self.assertEqual(bot._pending_admin_actions, {})


if __name__ == "__main__":
    unittest.main()
