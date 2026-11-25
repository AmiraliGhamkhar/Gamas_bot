"""Free first try (R3): one free quiz per user.

Two layers are covered:

* **Database** — the claim is a single atomic UPDATE, so concurrent generations
  can never share one gift, and the refund/reset paths are exact.
* **Bot** — the gift badge, the free generation itself, the paywall on the
  second attempt, and the provider-failure/cancellation rollback.

The flashcards feature (and the second "deck" gift it was billed against) is
gone: the quiz is the only generation that still offers a first try for free.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gamas_bot.bot import (
    FREE_TRY_BADGE,
    FREE_TRY_RESTORED,
    GamasBot,
    LEARNING_NO_CREDIT,
)
from gamas_bot.db import Database
from gamas_bot.llm import LLMError
from tests.support import FakeCallbackEvent, FakeTelegramClient, make_settings

OWNER = 618
OTHER = 619


def quiz_questions(count: int) -> list[dict]:
    """A valid quiz payload for the DB layer (four options, one answer key)."""
    return [
        {
            "question": f"اگر شرط {index} برقرار باشد کدام گزینه درست است؟",
            "options": [
                f"گزینه درست {index}",
                f"گزینه انحرافی الف {index}",
                f"گزینه انحرافی ب {index}",
                f"گزینه انحرافی ج {index}",
            ],
            "correct_index": 0,
            "explanation": "توضیح یک‌خطی",
            "source": "بخش ۱",
        }
        for index in range(count)
    ]


def quiz_payload(count: int) -> dict:
    """The provider's JSON shape (the parser turns it into ``quiz_questions``)."""
    return {
        "questions": [
            {
                "question": question["question"],
                "options": question["options"],
                "correct": question["correct_index"],
                "explain": question["explanation"],
                "difficulty": "medium",
                "source": question["source"],
            }
            for question in quiz_questions(count)
        ]
    }


class FreeUseDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(db_path=root / "free.db", temp_dir=root / "tmp")
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(OWNER)

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def test_a_new_user_has_the_quiz_gift(self):
        self.assertEqual(await self.db.free_use_state(OWNER), {"quiz": True})

    async def test_the_gift_can_be_claimed_only_once(self):
        self.assertTrue(await self.db.consume_free_use(OWNER, "quiz"))
        self.assertFalse(await self.db.consume_free_use(OWNER, "quiz"))
        self.assertEqual(await self.db.free_use_state(OWNER), {"quiz": False})

    async def test_concurrent_claims_have_exactly_one_winner(self):
        # The race the atomic UPDATE exists for: two generations claiming at the
        # same instant must never both get the free try.
        results = await asyncio.gather(
            self.db.consume_free_use(OWNER, "quiz"),
            self.db.consume_free_use(OWNER, "quiz"),
        )
        self.assertEqual(sorted(results), [False, True])

    async def test_two_users_do_not_share_a_gift(self):
        await self.db.ensure_user(OTHER)
        self.assertTrue(await self.db.consume_free_use(OWNER, "quiz"))
        self.assertTrue(await self.db.consume_free_use(OTHER, "quiz"))

    async def test_refund_only_restores_a_claimed_gift(self):
        self.assertFalse(await self.db.refund_free_use(OWNER, "quiz"))
        await self.db.consume_free_use(OWNER, "quiz")
        self.assertTrue(await self.db.refund_free_use(OWNER, "quiz"))
        self.assertTrue((await self.db.free_use_state(OWNER))["quiz"])

    async def test_retired_and_unknown_kinds_are_rejected(self):
        # ``deck`` is the retired flashcard gift: the column is gone, so the
        # kind must fail loudly instead of silently reading a missing column.
        for kind in ("notes", "deck"):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    await self.db.consume_free_use(OWNER, kind)

    async def test_admin_reset_restores_the_gift_and_audits(self):
        await self.db.consume_free_use(OWNER, "quiz")
        await self.db.reset_free_uses(OWNER, admin_id=1)
        self.assertEqual(await self.db.free_use_state(OWNER), {"quiz": True})
        audit = await self.db.recent_audit(limit=1)
        self.assertEqual(audit[0]["action"], "free_use_reset")
        self.assertEqual(audit[0]["subject_user"], OWNER)


    async def test_token_claim_race_has_one_winner(self):
        claims = await asyncio.gather(*(
            self.db.consume_free_use(OWNER, "quiz", claim_id=f"token-{i}") for i in range(12)
        ))
        self.assertEqual(sum(claims), 1)
        self.assertFalse((await self.db.free_use_state(OWNER))["quiz"])

    async def test_reset_during_generation_does_not_let_old_refund_touch_new_claim(self):
        old = "old-token"
        self.assertTrue(await self.db.consume_free_use(OWNER, "quiz", claim_id=old))
        await self.db.reset_free_uses(OWNER, admin_id=1)
        new = "new-token"
        self.assertTrue(await self.db.consume_free_use(OWNER, "quiz", claim_id=new))
        self.assertNotEqual(old, new)
        self.assertFalse(await self.db.refund_free_use(OWNER, "quiz", claim_id=old))
        self.assertFalse((await self.db.free_use_state(OWNER))["quiz"])
        self.assertTrue(await self.db.refund_free_use(OWNER, "quiz", claim_id=new))
        self.assertTrue((await self.db.free_use_state(OWNER))["quiz"])

    async def test_old_success_after_reset_cannot_finalize_a_newer_claim(self):
        old = "old-token"
        self.assertTrue(await self.db.consume_free_use(OWNER, "quiz", claim_id=old))
        await self.db.reset_free_uses(OWNER, admin_id=1)
        new = "new-token"
        self.assertTrue(await self.db.consume_free_use(OWNER, "quiz", claim_id=new))
        item = await self.db.library_add(OWNER, "booklet", 1, 2, None, title="سند")
        await self.db.quiz_create_session(
            OWNER, item, quiz_questions(1), free_claim_id=old
        )
        # The old success neither spends nor clears the replacement token.
        self.assertTrue(await self.db.refund_free_use(OWNER, "quiz", claim_id=new))
        self.assertTrue((await self.db.free_use_state(OWNER))["quiz"])
        self.assertGreaterEqual(len(list(await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions WHERE user_id = ?", (OWNER,)))), 1)

    async def test_artifact_and_finalization_roll_back_together(self):
        claim = "pending-token"
        self.assertTrue(await self.db.consume_free_use(OWNER, "quiz", claim_id=claim))
        item = await self.db.library_add(OWNER, "booklet", 1, 2, None, title="سند")
        with self.assertRaises(ValueError):
            await self.db.quiz_create_session(
                OWNER, item, [{"question": "بدون گزینه"}], free_claim_id=claim
            )
        self.assertEqual(len(list(await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions WHERE user_id = ?", (OWNER,)))), 0)
        self.assertTrue(await self.db.refund_free_use(OWNER, "quiz", claim_id=claim))

    async def test_interrupted_claims_recover_but_delivered_gifts_stay_consumed(self):
        await self.db.ensure_user(OTHER)
        await self.db.consume_free_use(OWNER, "quiz", claim_id="pending-quiz")
        await self.db.consume_free_use(OTHER, "quiz")  # finalized legacy claim
        self.assertEqual(await self.db.recover_pending_free_uses(), 1)
        self.assertEqual(await self.db.free_use_state(OWNER), {"quiz": True})
        self.assertFalse((await self.db.free_use_state(OTHER))["quiz"])
        self.assertEqual(await self.db.recover_pending_free_uses(), 0)


class FreeFirstTryBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "freebot.db", temp_dir=root / "tmp",
            avalai_api_key="avalai-test-key",  # mocked generation fixture
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.recorder = SimpleNamespace(files=[], file_payloads=[], replies=[], edits=[])
        self.client = FakeTelegramClient(self.recorder)
        self.bot.client = self.client
        await self.db.ensure_user(OWNER)
        self.item_id = await self.db.library_add(
            OWNER, "booklet", 10, 20, None, title="جزوهٔ آزمایشی"
        )
        await self.db.library_set_content(
            self.item_id,
            "تنفس سلولی در میتوکندری رخ می‌دهد و گلیکولیز در سیتوپلاسم انجام می‌شود. "
            "چرخهٔ کربس در ماتریکس میتوکندری است.",
        )

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def press(self, data: bytes, user_id: int = OWNER) -> FakeCallbackEvent:
        event = FakeCallbackEvent(self.client, data=data, chat_id=user_id)
        event.client = self.client
        return event

    def last_text(self, event: FakeCallbackEvent) -> str:
        return event.edits[-1]["text"]

    async def test_badge_is_shown_while_the_gift_is_available(self):
        flash = self.press(f"quiz:new:{self.item_id}".encode())
        await self.bot._handle_callback(flash)
        self.assertIn(FREE_TRY_BADGE, self.last_text(flash))
        flash_labels = [
            button.text
            for row in flash.edits[-1]["buttons"]
            for button in row
            if getattr(button, "text", None)
        ]
        self.assertTrue(any("رایگان در اولین استفاده" in label for label in flash_labels))

        quiz = self.press(f"quiz:new:{self.item_id}".encode())
        await self.bot._handle_callback(quiz)
        self.assertIn(FREE_TRY_BADGE, self.last_text(quiz))
        quiz_labels = [
            button.text
            for row in quiz.edits[-1]["buttons"]
            for button in row
            if getattr(button, "text", None)
        ]
        self.assertTrue(any("رایگان در اولین استفاده" in label for label in quiz_labels))

    async def test_badge_disappears_once_the_gift_is_used(self):
        await self.db.consume_free_use(OWNER, "quiz")
        flash = self.press(f"quiz:new:{self.item_id}".encode())
        await self.bot._handle_callback(flash)
        self.assertNotIn(FREE_TRY_BADGE, self.last_text(flash))

    async def test_provider_failure_returns_the_free_try(self):
        event = self.press(f"quiz:go:{self.item_id}:5".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=LLMError("boom"))
        ):
            await self.bot._handle_callback(event)
        self.assertTrue(
            (await self.db.free_use_state(OWNER))["quiz"],
            "a provider fault must never consume the gift",
        )
        self.assertIn(FREE_TRY_RESTORED, self.client.texts[-1]["text"])
        self.assertEqual(len(list(await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions WHERE user_id = ?", (OWNER,)))), 0)

    async def test_a_valid_generation_consumes_the_gift_and_delivers_html_immediately(self):
        event = self.press(f"quiz:go:{self.item_id}:5".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(5)),
        ):
            await self.bot._handle_callback(event)
        self.assertGreaterEqual(len(list(await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions WHERE user_id = ?", (OWNER,)))), 1)
        self.assertFalse((await self.db.free_use_state(OWNER))["quiz"])
        self.assertEqual(len(await self.db.library_list_kind(OWNER, "quiz")), 1)

    async def test_delivery_failure_restores_the_free_try(self):
        event = self.press(f"quiz:go:{self.item_id}:5".encode())
        with (
            mock.patch(
                "gamas_bot.bot.complete_json",
                new=mock.AsyncMock(return_value=quiz_payload(5)),
            ),
            mock.patch.object(
                self.client,
                "send_file",
                new=mock.AsyncMock(side_effect=RuntimeError("telegram upload failed")),
            ),
        ):
            await self.bot._handle_callback(event)
        self.assertTrue((await self.db.free_use_state(OWNER))["quiz"])
        self.assertEqual(len(list(await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions WHERE user_id = ?", (OWNER,)))), 0)
        self.assertEqual(await self.db.library_list_kind(OWNER, "quiz"), [])

    async def test_first_quiz_is_free_and_second_is_paid(self):
        await self.db.add_credit(OWNER, 600, note="paid", validity_days=30)
        first = self.press(f"quiz:go:{self.item_id}:5".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(5)),
        ):
            await self.bot._handle_callback(first)
        debits = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE kind='debit'"
        )
        self.assertEqual(debits[0][0], 0)  # the gift paid for this quiz
        self.assertFalse((await self.db.free_use_state(OWNER))["quiz"])
        # Finish the session so the admission gate allows a second one.
        rows = await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions")
        await self.db.quiz_finish(int(rows[0][0]), "quit")
        second = self.press(f"quiz:go:{self.item_id}:5".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(5)),
        ) as generator:
            await self.bot._handle_callback(second)
            generator.assert_awaited_once()
        debits = await self.db.db.execute_fetchall(
            "SELECT seconds FROM ledger WHERE kind='debit'"
        )
        self.assertEqual(len(debits), 1)
        self.assertEqual(int(debits[0][0]), 300)  # 5 questions x 60s

    async def test_concurrent_double_tap_claims_one_gift(self):
        async def slow(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return quiz_payload(5)

        with mock.patch(
            "gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=slow)
        ):
            await asyncio.gather(
                self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode())),
                self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode())),
            )
        # The second tap was rejected as busy; the gift was claimed exactly once.
        self.assertGreaterEqual(len(list(await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions WHERE user_id = ?", (OWNER,)))), 1)
        state = await self.db.free_use_state(OWNER)
        self.assertFalse(state["quiz"], "the first tap consumed the gift")
        self.assertEqual(int((await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE kind='debit'"
        ))[0][0]), 0, "the rejected double tap must not spend credit")

    async def test_size_preset_changes_count_and_price(self):
        await self.db.add_credit(OWNER, 5000, note="paid", validity_days=30)
        await self.db.consume_free_use(OWNER, "quiz")
        # The price scales with the requested size: the «استاندارد» preset asks
        # for 10 questions at 60 s each, so it must be billed 600 s.
        event = self.press(f"quiz:go:{self.item_id}:10".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(10)),
        ) as generator:
            await self.bot._handle_callback(event)
            asked = generator.await_args.kwargs["user"]
            self.assertTrue(asked)
        debit = await self.db.db.execute_fetchall(
            "SELECT seconds FROM ledger WHERE kind='debit'"
        )
        self.assertEqual(int(debit[0][0]), 600)
        self.assertEqual(int((await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM quiz_questions"
        ))[0][0]), 10)

    async def test_quiz_size_preset_bills_per_question(self):
        await self.db.add_credit(OWNER, 5000, note="paid", validity_days=30)
        await self.db.consume_free_use(OWNER, "quiz")
        event = self.press(f"quiz:go:{self.item_id}:20".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(20)),
        ):
            await self.bot._handle_callback(event)
        debit = await self.db.db.execute_fetchall(
            "SELECT seconds FROM ledger WHERE kind='debit'"
        )
        self.assertEqual(int(debit[0][0]), 20 * 60)
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM quiz_questions"
        )
        self.assertEqual(int(rows[0][0]), 20)

    async def test_admin_reset_gives_the_gift_back(self):
        admin_id = next(iter(self.settings.admin_ids))
        await self.db.ensure_user(admin_id)
        await self.db.consume_free_use(OWNER, "quiz")
        confirm = self.press(f"adm:hub:usr:fr:{OWNER}".encode(), user_id=admin_id)
        await self.bot._handle_callback(confirm)
        execute = self.press(f"adm:hub:usr:frx:{OWNER}".encode(), user_id=admin_id)
        await self.bot._handle_callback(execute)
        self.assertTrue((await self.db.free_use_state(OWNER))["quiz"])


    async def test_paid_delivery_failure_refunds_credit_once(self):
        await self.db.consume_free_use(OWNER, "quiz")
        await self.db.add_credit(OWNER, 600, note="اعتبار", validity_days=30)
        with (
            mock.patch(
                "gamas_bot.bot.complete_json",
                new=mock.AsyncMock(return_value=quiz_payload(5)),
            ),
            mock.patch.object(
                self.client,
                "send_file",
                new=mock.AsyncMock(side_effect=RuntimeError("upload error")),
            ),
        ):
            await self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode()))
        self.assertEqual(await self.db.available_seconds(OWNER), 600)
        self.assertEqual(len(list(await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions WHERE user_id = ?", (OWNER,)))), 0)

    async def test_progress_edit_failure_does_not_abort_html_delivery(self):
        from tests.support import FakeSentMessage

        with (
            mock.patch.object(
                FakeSentMessage, "edit", new=mock.AsyncMock(side_effect=RuntimeError("blocked"))
            ),
            mock.patch(
                "gamas_bot.bot.complete_json",
                new=mock.AsyncMock(return_value=quiz_payload(5)),
            ),
        ):
            await self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode()))
        self.assertFalse((await self.db.free_use_state(OWNER))["quiz"])
        self.assertGreaterEqual(len(list(await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions WHERE user_id = ?", (OWNER,)))), 1)
        self.assertEqual(len(await self.db.library_list_kind(OWNER, "quiz")), 1)

    async def test_task_cancellation_restores_free_gift(self):
        started = asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=blocked)):
            task = asyncio.create_task(self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode())))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue((await self.db.free_use_state(OWNER))["quiz"])
        self.assertNotIn(OWNER, self.bot._learning_in_flight)

    async def test_quiz_second_attempt_without_credit_reaches_paywall(self):
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=quiz_payload(5))):
            await self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode()))
        row = (await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions"))[0]
        await self.db.quiz_finish(int(row[0]), "quit")
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock()) as generator:
            await self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode()))
        generator.assert_not_awaited()
        self.assertTrue(any(LEARNING_NO_CREDIT in record["text"] for record in self.client.texts))
        # The quiz is the only free-first-try gift, so the first quiz spent it;
        # the second attempt is refused without credit rather than re-granted.
        self.assertFalse((await self.db.free_use_state(OWNER))["quiz"])

    async def test_reset_execute_requires_a_fresh_confirmation(self):
        admin = next(iter(self.settings.admin_ids))
        await self.db.ensure_user(admin)
        await self.db.consume_free_use(OWNER, "quiz")
        forged = self.press(f"adm:hub:usr:frx:{OWNER}".encode(), user_id=admin)
        await self.bot._handle_callback(forged)
        self.assertFalse((await self.db.free_use_state(OWNER))["quiz"])
        self.assertTrue(forged.answers[-1][1])
        await self.bot._handle_callback(self.press(f"adm:hub:usr:fr:{OWNER}".encode(), user_id=admin))
        await self.bot._handle_callback(self.press(b"adm:home", user_id=admin))
        await self.bot._handle_callback(self.press(f"adm:hub:usr:frx:{OWNER}".encode(), user_id=admin))
        self.assertFalse((await self.db.free_use_state(OWNER))["quiz"])


    async def test_shutdown_cancels_active_generation_and_compensates_before_db_close(self):
        started = asyncio.Event()
        async def provider(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with mock.patch("gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=provider)):
            task = asyncio.create_task(self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode())))
            await asyncio.wait_for(started.wait(), 2)
            await self.bot.shutdown()
        self.assertTrue(task.cancelled())
        await self.db.connect()  # shutdown closed SQLite; inspect durable state
        self.assertTrue((await self.db.free_use_state(OWNER))["quiz"])
        self.assertFalse(self.bot._learning_tasks)

    async def test_cancel_during_committed_admission_refunds_its_token(self):
        admitted, release = asyncio.Event(), asyncio.Event()
        original = self.bot._claim_generation_budget
        async def claim(*args, **kwargs):
            result = await original(*args, **kwargs)
            admitted.set()
            await release.wait()
            return result
        with mock.patch.object(self.bot, "_claim_generation_budget", side_effect=claim):
            task = asyncio.create_task(self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode())))
            await asyncio.wait_for(admitted.wait(), 2)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue((await self.db.free_use_state(OWNER))["quiz"])
        self.assertNotIn(OWNER, self.bot._learning_in_flight)

    async def test_shutdown_racing_commit_never_refunds_a_committed_artifact(self):
        saving, release = asyncio.Event(), asyncio.Event()
        original = self.db.quiz_create_session

        async def delayed_save(*args, **kwargs):
            saving.set()
            await release.wait()
            return await original(*args, **kwargs)

        with (
            mock.patch(
                "gamas_bot.bot.complete_json",
                new=mock.AsyncMock(return_value=quiz_payload(5)),
            ),
            mock.patch.object(self.db, "quiz_create_session", side_effect=delayed_save),
        ):
            gen_task = asyncio.create_task(
                self.bot._handle_callback(self.press(f"quiz:go:{self.item_id}:5".encode()))
            )
            await asyncio.wait_for(saving.wait(), 2)
            shutdown_task = asyncio.create_task(self.bot.shutdown())
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(gen_task, shutdown_task, return_exceptions=True)
        await self.db.connect()
        self.assertGreaterEqual(len(list(await self.db.db.execute_fetchall("SELECT session_id FROM quiz_sessions WHERE user_id = ?", (OWNER,)))), 1)
        self.assertFalse((await self.db.free_use_state(OWNER))["quiz"])


if __name__ == "__main__":
    unittest.main()
