"""Phase 6: the bot as a file interface — the contract a student actually meets.

``/start`` asks for one thing, a supported file starts the pipeline on its own,
progress and failure say what happened, the completed message offers the
transcript, the PDF booklet, the Word booklet and 20 sample questions, and every
account screen lives in the Mini App. These tests pin that contract; the
busywork of each feature (billing, admin, support, referrals) is covered by its
own suite and is only smoke-checked here.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from gamas_bot import texts
from gamas_bot.artifacts import (
    KIND_BOOKLET,
    KIND_DOCX,
    KIND_QUIZ,
    KIND_TRANSCRIPT,
    KIND_TXT,
    delivery_markup,
)
from gamas_bot.bot import GamasBot
from gamas_bot.bot_ui import QUIZ_COUNTS
from gamas_bot.db import Database
from gamas_bot.media import MediaInfo
from tests.support import FakeJobEvent, make_settings
from tests.test_bot import (
    USER_ID,
    BotPipelineTestCase,
    FakeCallbackEvent,
    FakeMessageEvent,
    menu_payloads,
)

ADMIN_ID = 1


def button_urls(menu) -> list[str]:
    """URL buttons of an inline keyboard, flattened row by row."""
    return [
        button.type.url
        for row in (menu or [])
        for button in row
        if getattr(button.type, "url", None)
    ]


class StartScreenTests(BotPipelineTestCase):
    """/start is the whole interface: one sentence and one request."""

    async def test_start_asks_for_a_file_in_one_short_message(self):
        start = FakeMessageEvent(USER_ID, "/start")
        await self.bot._handle_message(start)

        self.assertEqual(len(start.replies), 1)
        text = start.replies[0]
        self.assertTrue(text.startswith("فایل درس را بفرستید"))
        self.assertIn("صوت، ویدیو، PDF، Word یا PowerPoint", text)
        self.assertLess(len(text), 200)
        # No account numbers and no second navigation layer on the start screen.
        self.assertNotIn("اعتبار", text)
        self.assertNotIn("کتابخانه", text)
        self.assertNotIn("بخش‌های دیگر", text)

    async def test_start_is_identical_on_every_visit(self):
        first = FakeMessageEvent(USER_ID, "/start")
        await self.bot._handle_message(first)
        again = FakeMessageEvent(USER_ID, "/start")
        await self.bot._handle_message(again)
        self.assertEqual(first.replies, again.replies)

    async def test_start_offers_the_mini_app_as_the_only_button(self):
        self.bot.settings = replace(self.bot.settings, webapp_url="https://gamas.example")
        start = FakeMessageEvent(USER_ID, "/start")
        await self.bot._handle_message(start)

        buttons = start.client.texts[0]["buttons"]
        self.assertEqual(button_urls(buttons), ["https://gamas.example/app"])
        self.assertEqual(menu_payloads(buttons), set())
        # No reply keyboard is attached: nothing competes with the message box.
        self.assertNotIn("reply_markup", start.client.texts[0])

    async def test_start_adds_only_the_admin_panel_for_admins(self):
        self.bot.settings = replace(self.bot.settings, webapp_url="https://gamas.example")
        start = FakeMessageEvent(ADMIN_ID, "/start")
        await self.bot._handle_message(start)

        buttons = start.client.texts[0]["buttons"]
        self.assertEqual(menu_payloads(buttons), {b"adm:home"})
        self.assertEqual(button_urls(buttons), ["https://gamas.example/app"])

    async def test_start_without_a_public_url_shows_no_menu_button(self):
        self.bot.settings = replace(self.bot.settings, webapp_url="")
        start = FakeMessageEvent(USER_ID, "/start")
        await self.bot._handle_message(start)
        self.assertFalse(start.client.texts[0].get("buttons"))

    async def test_a_bare_message_repeats_the_request_and_the_app_button(self):
        self.bot.settings = replace(self.bot.settings, webapp_url="https://gamas.example")
        event = FakeMessageEvent(USER_ID, "سلام")
        await self.bot._handle_message(event)
        self.assertEqual(event.replies, [texts.UPLOAD_HINT])
        self.assertEqual(
            button_urls(event.client.texts[0]["buttons"]), ["https://gamas.example/app"]
        )


class FileSubmissionTests(BotPipelineTestCase):
    """The file alone is the entry point; validation is unchanged."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The channel gate is exercised by its own suite; here the question is
        # only what intake does once the user is allowed to send a file.
        self.bot.settings = replace(self.settings, subscription_required=False)

    async def test_a_voice_note_starts_the_pipeline_without_any_mode_tap(self):
        source = Path(self._tmp.name) / "lecture.ogg"
        source.write_bytes(b"OggS\x00\x02fake-audio-payload")
        event = FakeJobEvent(source=source, chat_id=USER_ID)
        event.message.attach_voice(duration=12)
        self.bot.client = event.client
        # A new user's once-only free plan is what pays for the first file.
        await self.db.grant_free_plan_once(USER_ID, 3600)

        with (
            mock.patch(
                "gamas_bot.bot.probe_media",
                new=mock.AsyncMock(
                    return_value=MediaInfo(duration=12.0, has_audio=True, audio_codec="opus")
                ),
            ),
            mock.patch("gamas_bot.bot.needs_transcode", return_value=False),
            mock.patch(
                "gamas_bot.bot.extract_audio_track",
                new=mock.AsyncMock(side_effect=lambda source, *a, **k: source),
            ),
        ):
            await self.bot._handle_message(event)

        job = self.bot.queue.pop_pending()
        self.assertIsNotNone(job, "a supported upload must be queued immediately")
        self.assertEqual(job.kind, "audio")
        # The intake acknowledged the file in the chat (status, not instructions).
        self.assertTrue(event.replies)
        row = await self.db.submission(job.submission_id)
        assert row is not None
        self.assertEqual(row.kind, "audio")

    async def test_a_pdf_is_accepted_as_a_document_upload(self):
        event = FakeJobEvent(chat_id=USER_ID)
        event.message.attach_document("lecture.pdf", mime_type="application/pdf")
        self.bot.client = event.client

        with mock.patch.object(
            self.bot, "_accept_document", new=mock.AsyncMock()
        ) as accept:
            await self.bot._handle_message(event)

        accept.assert_awaited_once()
        self.assertEqual(accept.await_args.args[2], "pdf")

    async def test_an_unsupported_file_is_rejected_with_the_supported_list(self):
        event = FakeJobEvent(chat_id=USER_ID)
        event.message.attach_document("archive.zip", mime_type="application/zip")
        self.bot.client = event.client

        await self.bot._handle_message(event)

        self.assertEqual(event.replies, [texts.UNSUPPORTED_FILE])
        # A rejected file never becomes a submission, so it can never be billed.
        self.assertEqual(await self.db.user_submission_count(USER_ID), 0)

    async def test_a_photo_is_answered_with_the_photo_hint(self):
        from gamas_bot.bot_ui import DOC_PHOTO_HINT

        event = FakeJobEvent(chat_id=USER_ID)
        # Telethon exposes an uploaded photo both as ``media`` and ``photo``.
        event.message.media = mock.sentinel.photo_media
        event.message.photo = object()
        self.bot.client = event.client

        await self.bot._handle_message(event)

        self.assertEqual(event.replies, [DOC_PHOTO_HINT])

    async def test_an_unsupported_attachment_is_never_charged(self):
        """Validation happens before any credit is touched."""
        await self.db.add_credit(USER_ID, 3600, note="test")
        before = await self.db.available_seconds(USER_ID)
        event = FakeJobEvent(chat_id=USER_ID)
        event.message.attach_document("notes.exe", mime_type="application/x-msdownload")
        self.bot.client = event.client

        await self.bot._handle_message(event)

        self.assertEqual(await self.db.available_seconds(USER_ID), before)


class DeliveryContractTests(BotPipelineTestCase):
    """What the finished file offers: transcript, PDF, Word, 20 questions."""

    def test_the_completed_message_offers_exactly_the_student_outputs(self):
        markup = delivery_markup(
            reference="GMS-1",
            speakers=1,
            has_booklet=True,
            kinds={KIND_BOOKLET, KIND_TRANSCRIPT, KIND_TXT, KIND_DOCX, KIND_QUIZ},
            item_id=77,
            learning_available=True,
        )
        payloads = menu_payloads(markup)
        self.assertIn(b"art:GMS-1:booklet", payloads)
        self.assertIn(b"art:GMS-1:transcript", payloads)
        self.assertIn(b"art:GMS-1:docx", payloads)
        # The 20-question pack opens its screen; the size and price are confirmed
        # there, so this tap never spends credits by itself.
        self.assertIn(b"quiz:new:77", payloads)
        labels = [button.text for row in markup for button in row]
        self.assertIn(texts.BTN_QUIZ_FROM_BOOKLET, labels)
        self.assertIn("۲۰", texts.BTN_QUIZ_FROM_BOOKLET)
        # No account navigation and no second menu on a finished file.
        self.assertNotIn(b"ref:result", payloads)
        self.assertNotIn(b"menu:main", payloads)

    def test_the_completed_message_hides_artifacts_that_do_not_exist(self):
        markup = delivery_markup(
            reference="GMS-2",
            speakers=None,
            has_booklet=False,
            kinds={KIND_TRANSCRIPT},
            item_id=None,
            learning_available=False,
        )
        self.assertEqual(menu_payloads(markup), {b"art:GMS-2:transcript"})

    async def test_the_quiz_screen_offers_a_20_question_pack(self):
        user_id = USER_ID
        item_id = await self.db.library_add(
            user_id, "booklet", user_id, 123, None, title="جزوهٔ آزمایشی"
        )
        await self.db.library_set_content(item_id, "متن ذخیره‌شده برای ساخت سؤال")
        self.bot.settings = replace(
            self.bot.settings,
            avalai_api_key="test-key",
            note_providers=("avalai",),
        )
        self.assertTrue(self.bot.settings.note_configured)

        event = FakeCallbackEvent(user_id, f"quiz:new:{item_id}".encode("ascii"))
        await self.bot._handle_callback(event)

        text, buttons = event.edits[-1]
        payloads = menu_payloads(buttons)
        twenty = f"quiz:go:{item_id}:20".encode("ascii")
        self.assertIn(twenty, payloads)
        self.assertIn(twenty, {data for data in payloads if data.endswith(b":20")})
        labels = [button.text for row in buttons for button in row]
        self.assertTrue(any("۲۰" in label for label in labels), labels)
        # The screen still names the price of the size it offers.
        self.assertIn("سؤال", text)
        self.assertIn(20, QUIZ_COUNTS)

    async def test_a_failed_job_says_what_happened_and_delivers_nothing(self):
        event = self.media_event()
        # The worker swallows the provider error and reports it in the chat; the
        # user must see a planted failure, not a stack trace or silence.
        await self.run_media_job(event, transcribe=RuntimeError("provider down"))

        self.assertTrue(event.replies)
        joined = "\n".join(event.replies)
        self.assertTrue(
            any(word in joined for word in ("ناموفق", "انجام نشد", "تکمیل نشد")),
            joined,
        )
        self.assertEqual(event.files, [])

    async def test_a_finished_job_delivers_the_files_it_announces(self):
        from gamas_bot.stt import RawTranscript as STTRawTranscript

        event = self.media_event()
        submission = await self.run_media_job(
            event, transcribe=STTRawTranscript.from_text("متن گفتار", engine="deepgram")
        )
        delivery = "\n".join(event.replies)
        self.assertIn(texts.DELIVERY_READY_HEADER.split("{")[0], delivery)
        # Every button in the completion screen points at a persisted artifact.
        buttons = menu_payloads(event.client.texts[-1].get("buttons"))
        self.assertTrue(buttons, delivery)
        for data in buttons:
            self.assertTrue(
                data.startswith(b"art:") or data.startswith(b"quiz:"),
                data,
            )
        row = await self.db.submission(submission)
        assert row is not None
        self.assertEqual(row.status, "done")


class AccountReachTests(BotPipelineTestCase):
    """Account, money, support and admin keep working — through one entry each."""

    async def test_the_payment_flow_is_reachable_by_command(self):
        event = FakeMessageEvent(USER_ID, "/buy")
        await self.bot._handle_message(event)

        self.assertEqual(len(event.replies), 1)
        self.assertIn(texts.WALLET_TITLE, event.replies[0])
        payloads = menu_payloads(event.client.texts[0]["buttons"])
        self.assertTrue(
            any(data.startswith(b"plan:") for data in payloads),
            payloads,
        )

    async def test_help_names_the_commands_instead_of_a_menu(self):
        self.bot.settings = replace(
            self.settings, avalai_api_key="test-key", note_providers=("avalai",)
        )
        event = FakeMessageEvent(USER_ID, "/help")
        await self.bot._handle_message(event)

        text = event.replies[0]
        self.assertIn("/buy", text)
        self.assertIn("/support", text)
        self.assertIn("۲۰ سؤال نمونه", text)
        self.assertIn("مینی‌اپ", text)
        self.assertLessEqual(len([line for line in text.splitlines() if line.strip()]), 15)
        # The guide's only button is the bot's one action, not a menu.
        buttons = event.client.texts[0]["buttons"]
        self.assertEqual([button.text for row in buttons for button in row],
                         [texts.BTN_BACK_TO_UPLOAD])
        self.assertEqual(menu_payloads(buttons), {b"menu:main"})

    async def test_help_in_raw_only_mode_promises_only_what_it_can_build(self):
        event = FakeMessageEvent(USER_ID, "/help")
        await self.bot._handle_message(event)
        text = event.replies[0]
        self.assertIn("متن خام و فایل‌های استخراج‌شده", text)
        self.assertIn("ساخت خودکار جزوه", text)

    async def test_mini_app_deep_links_open_the_screen_they_name(self):
        """The Mini App links back with ?start=buy / ?start=support."""
        await self.db.ensure_user(USER_ID)
        buy = FakeMessageEvent(USER_ID, "/start buy")
        await self.bot._handle_message(buy)
        self.assertTrue(buy.replies)
        self.assertIn(texts.WALLET_TITLE, buy.replies[0])
        support = FakeMessageEvent(USER_ID, "/start support")
        await self.bot._handle_message(support)
        self.assertEqual(support.replies, [texts.SUPPORT_MENU])

    async def test_the_office_draft_stays_reachable_by_command(self):
        self.bot.settings = replace(
            self.settings, office_enabled=True, subscription_required=False
        )
        event = FakeMessageEvent(USER_ID, "/office")
        await self.bot._handle_message(event)
        self.assertIn(USER_ID, self.bot._office_sessions)
        self.assertTrue(event.replies)

    async def test_support_is_reachable_by_command(self):
        event = FakeMessageEvent(USER_ID, "/support")
        await self.bot._handle_message(event)
        self.assertEqual(event.replies, [texts.SUPPORT_MENU])

    async def test_an_admin_keeps_the_panel(self):
        await self.db.ensure_user(ADMIN_ID)
        event = FakeMessageEvent(ADMIN_ID, texts.BTN_ADMIN)
        await self.bot._handle_message(event)
        self.assertTrue(any("مدیریت" in reply for reply in event.replies))

    async def test_an_admin_command_still_grants_credit(self):
        await self.db.ensure_user(999_001)
        event = FakeMessageEvent(ADMIN_ID, "/grant 999001 1")
        await self.bot._handle_message(event)
        self.assertTrue(any("اعتبار" in reply for reply in event.replies))
        self.assertEqual(await self.db.available_seconds(999_001), 3600)


class NoFlashcardSurfacesTests(unittest.TestCase):
    """Flashcards are gone from every user-facing workflow."""

    def test_no_user_facing_copy_mentions_flashcards(self):
        offenders = [
            name
            for name in dir(texts)
            if isinstance(getattr(texts, name), str)
            and any(word in getattr(texts, name) for word in ("فلش", "فلش‌کارت", "کارت‌ها"))
        ]
        self.assertEqual(offenders, [])

    def test_the_bot_draws_no_navigation_keyboards(self):
        self.assertFalse(hasattr(GamasBot, "_reply_keyboard"))
        self.assertFalse(hasattr(GamasBot, "_more_menu"))
        for is_admin in (False, True):
            self.assertEqual(texts.reply_menu_rows(is_admin=is_admin), [])

    def test_a_retired_flashcard_callback_does_nothing(self):
        import asyncio

        async def press() -> FakeCallbackEvent:
            event = FakeCallbackEvent(USER_ID, b"board")
            await bot._handle_callback(event)
            return event

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(db_path=root / "bot.db", temp_dir=root / "tmp")
            db = Database(settings.db_path)
            bot = GamasBot(settings)
            bot.db = db

            async def run() -> FakeCallbackEvent:
                await db.connect()
                await db.ensure_user(USER_ID)
                try:
                    return await press()
                finally:
                    await bot.shutdown()
                    await db.close()

            event = asyncio.run(run())

        self.assertFalse(event.edits)
        self.assertFalse(event.responses)


if __name__ == "__main__":
    unittest.main()
