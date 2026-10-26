"""Tests for the learning loop: quiz engine, leaderboard and content helpers.

The LLM is faked at the ``gamas_bot.bot.complete_json`` seam exactly like the
note pipeline tests fake their generators, so every billing/refund guarantee
runs against a real temporary database with no network.
"""

from __future__ import annotations

import asyncio
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gamas_bot.bot import GamasBot, LEARNING_GENERATION_FAILED, LEARNING_NO_CREDIT
from gamas_bot.db import Database
from gamas_bot.learning import (
    MAX_CONTENT_CHARS,
    bound_content_text,
    parse_flashcard_payload,
    parse_quiz_payload,
)
from gamas_bot.llm import LLMError
from tests.support import (
    FakeCallbackEvent,
    FakeTelegramClient,
    docx_text,
    make_settings,
)
from tests.test_bot import FakeMessageEvent, menu_payloads

OWNER = 555
OTHER = 999


def quiz_payload(count: int = 5) -> dict:
    return {
        "questions": [
            {
                "question": f"سؤال شماره {index}؟",
                "options": [
                    f"گزینه درست {index}",
                    f"انحرافی الف {index}",
                    f"انحرافی ب {index}",
                    f"انحرافی ج {index}",
                ],
                "correct": 0,
                "explain": f"زیرا نکته {index} این‌گونه است.",
            }
            for index in range(count)
        ]
    }


class LearningHelperTests(unittest.TestCase):
    def test_bound_content_text_cuts_on_paragraph_boundary(self):
        text = "پاراگراف نخست.\n\n" + ("کلمه " * 20_000)
        bounded = bound_content_text(text)
        self.assertLessEqual(len(bounded), MAX_CONTENT_CHARS)
        self.assertTrue(bounded.startswith("پاراگراف نخست."))

    def test_bound_content_text_passes_short_text_through(self):
        self.assertEqual(bound_content_text("  متن کوتاه  "), "متن کوتاه")
        self.assertEqual(bound_content_text(None), "")

    def test_quiz_payload_validation_drops_malformed_and_remaps_shuffle(self):
        payload = {
            "questions": [
                {  # valid
                    "question": "سؤال سالم",
                    "options": ["یک", "دو", "سه", "چهار"],
                    "correct": 2,
                    "explain": "توضیح",
                },
                {  # only three options -> dropped
                    "question": "ناقص",
                    "options": ["یک", "دو", "سه"],
                    "correct": 0,
                    "explain": "توضیح",
                },
                {  # duplicate options -> dropped
                    "question": "تکراری",
                    "options": ["یک", "یک", "سه", "چهار"],
                    "correct": 0,
                    "explain": "توضیح",
                },
                {  # correct out of range -> dropped
                    "question": "کلید اشتباه",
                    "options": ["یک", "دو", "سه", "چهار"],
                    "correct": 9,
                    "explain": "توضیح",
                },
                {  # bool is not an int index -> dropped
                    "question": "کلید بولین",
                    "options": ["یک", "دو", "سه", "چهار"],
                    "correct": True,
                    "explain": "توضیح",
                },
            ]
        }
        rng = random.Random(2026)
        parsed = parse_quiz_payload(payload, 5, rng=rng)
        self.assertEqual(len(parsed), 1)
        question = parsed[0]
        # The correct *text* must follow the shuffle, whatever its new index.
        self.assertEqual(question["options"][question["correct_index"]], "سه")
        self.assertEqual(len(question["options"]), 4)

    def test_quiz_payload_respects_requested_count(self):
        parsed = parse_quiz_payload(quiz_payload(10), 3, rng=random.Random(1))
        self.assertEqual(len(parsed), 3)

    def test_flashcard_payload_validation(self):
        payload = {
            "cards": [
                {"front": "پرسش یک", "back": "پاسخ یک"},
                {"front": "", "back": "پاسخ بدون پرسش"},  # dropped
                {"front": "پرسش دو", "back": "پاسخ دو"},
            ]
        }
        cards = parse_flashcard_payload(payload, 10)
        self.assertEqual(cards, [
            {"front": "پرسش یک", "back": "پاسخ یک"},
            {"front": "پرسش دو", "back": "پاسخ دو"},
        ])


class QuizDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "quiz.db")
        await self.db.connect()
        await self.db.ensure_user(OWNER)
        await self.db.ensure_user(OTHER)
        self.item_id = await self.db.library_add(
            OWNER, "booklet", 10, 20, None, title="جزوه"
        )
        await self.db.library_set_content(self.item_id, "متن جزوه برای آزمون")

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def _create_session(self, count: int = 3) -> int:
        questions = [
            {
                "question": f"سؤال {index}",
                "options": ["الف", "ب", "ج", "د"],
                "correct_index": index % 4,
                "explanation": "چرا",
            }
            for index in range(count)
        ]
        return await self.db.quiz_create_session(OWNER, self.item_id, questions)

    async def test_full_answer_flow_scores_points_and_ranks(self):
        session_id = await self._create_session()
        first = await self.db.quiz_current_question(session_id)
        self.assertEqual(first["idx"], 0)
        # Correct answer on question 0 (correct_index 0).
        result = await self.db.quiz_record_answer(session_id, first["question_id"], 0)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["correct"])
        self.assertFalse(result["finished"])
        rank = await self.db.leaderboard_rank(OWNER)
        self.assertEqual(rank["points"], 10)
        self.assertEqual(rank["rank"], 1)
        # Second answer, intentionally wrong.
        second = await self.db.quiz_current_question(session_id)
        result = await self.db.quiz_record_answer(
            session_id, second["question_id"], 3
        )
        self.assertFalse(result["correct"])
        rank = await self.db.leaderboard_rank(OWNER)
        self.assertEqual(rank["points"], 10)  # wrong answer adds nothing
        # Third answer correct -> session finishes.
        third = await self.db.quiz_current_question(session_id)
        result = await self.db.quiz_record_answer(session_id, third["question_id"], 2)
        self.assertTrue(result["finished"])
        self.assertEqual(result["score"], 20)
        session = await self.db.quiz_get_session(session_id, OWNER)
        self.assertEqual(session["status"], "done")
        self.assertIsNone(await self.db.quiz_current_question(session_id))
        # Exactly one answer row per question.
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM quiz_answers WHERE session_id = ?", (session_id,)
        )
        self.assertEqual(rows[0][0], 3)

    async def test_double_answer_is_rejected_once_and_never_double_scores(self):
        session_id = await self._create_session()
        question = await self.db.quiz_current_question(session_id)
        first = await self.db.quiz_record_answer(
            session_id, question["question_id"], 0
        )
        self.assertEqual(first["status"], "ok")
        second = await self.db.quiz_record_answer(
            session_id, question["question_id"], 1
        )
        self.assertEqual(second["status"], "already")
        rank = await self.db.leaderboard_rank(OWNER)
        self.assertEqual(rank["points"], 10)

    async def test_out_of_turn_question_is_stale(self):
        session_id = await self._create_session()
        questions = await self.db.db.execute_fetchall(
            "SELECT question_id FROM quiz_questions WHERE session_id = ? ORDER BY idx",
            (session_id,),
        )
        # Question idx=1 while current_index is 0.
        result = await self.db.quiz_record_answer(session_id, questions[1][0], 0)
        self.assertEqual(result["status"], "stale")

    async def test_ownership_is_enforced(self):
        session_id = await self._create_session()
        self.assertIsNone(await self.db.quiz_get_session(session_id, OTHER))

    async def test_admission_blocks_active_rate_limited_and_expired(self):
        await self._create_session()
        reason = await self.db.quiz_admission(
            OWNER, max_active_hours=6, max_sessions_per_hour=3
        )
        self.assertEqual(reason, "active")
        # Finish it; the hourly cap then applies.
        await self.db.db.execute(
            "UPDATE quiz_sessions SET status='done' WHERE user_id=?", (OWNER,)
        )
        await self.db.db.commit()
        reason = await self.db.quiz_admission(
            OWNER, max_active_hours=6, max_sessions_per_hour=1
        )
        self.assertEqual(reason, "rate")
        reason = await self.db.quiz_admission(
            OWNER, max_active_hours=6, max_sessions_per_hour=3
        )
        self.assertIsNone(reason)
        # A stale active session is lazily expired.
        await self.db.db.execute(
            "UPDATE quiz_sessions SET status='active', updated_at=? WHERE user_id=?",
            ("2000-01-01T00:00:00+00:00", OWNER),
        )
        await self.db.db.commit()
        reason = await self.db.quiz_admission(
            OWNER, max_active_hours=6, max_sessions_per_hour=3
        )
        self.assertIsNone(reason)
        statuses = await self.db.db.execute_fetchall(
            "SELECT DISTINCT status FROM quiz_sessions WHERE user_id=?", (OWNER,)
        )
        self.assertIn("expired", {row[0] for row in statuses})

    async def test_leaderboard_orders_and_excludes_banned_and_zeros(self):
        await self.db.set_first_name(OWNER, "نگار")
        await self.db.ensure_user(777)
        await self.db.set_first_name(777, "سارا")
        await self.db.db.execute("UPDATE users SET quiz_points=30 WHERE telegram_id=?", (OWNER,))
        await self.db.db.execute("UPDATE users SET quiz_points=50 WHERE telegram_id=?", (777,))
        await self.db.db.execute("UPDATE users SET banned=1, quiz_points=99 WHERE telegram_id=?", (OTHER,))
        await self.db.db.commit()
        board = await self.db.leaderboard_top(20)
        self.assertEqual([row["quiz_points"] for row in board], [50, 30])
        self.assertEqual(board[1]["first_name"], "نگار")
        # Banned user cannot outrank anyone.
        self.assertTrue(all(row["first_name"] != "" for row in board))
        rank_other = await self.db.leaderboard_rank(OTHER)
        # Banned users still get a numeric rank from the points table; the user
        # screen itself is ban-gated before this is ever shown.
        self.assertIsInstance(rank_other["rank"], int)

    async def test_quit_keeps_earned_points(self):
        session_id = await self._create_session()
        question = await self.db.quiz_current_question(session_id)
        await self.db.quiz_record_answer(session_id, question["question_id"], 0)
        await self.db.quiz_finish(session_id, "quit")
        session = await self.db.quiz_get_session(session_id, OWNER)
        self.assertEqual(session["status"], "quit")
        self.assertEqual(session["score"], 10)
        self.assertEqual((await self.db.leaderboard_rank(OWNER))["points"], 10)


class LeaderboardAndGiftTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(db_path=root / "board.db", temp_dir=root / "tmp")
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        self.bot.client = self.client
        self.admin_id = next(iter(self.settings.admin_ids))
        for uid in (OWNER, OTHER, 777):
            await self.db.ensure_user(uid)
        await self.db.sync_plans([
            {"code": "paid_5h_30d", "hours": 5, "price": 50000, "validity_days": 30},
        ])

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def _event(self, data: bytes, user_id: int) -> FakeCallbackEvent:
        event = FakeCallbackEvent(self.client, data=data, chat_id=user_id)
        event.client = self.client
        return event

    async def test_empty_board_uses_persian_empty_state(self):
        event = self._event(b"board", OWNER)
        await self.bot._handle_callback(event)
        self.assertIn("هنوز امتیازی ثبت نشده است", event.edits[-1]["text"])

    async def test_board_shows_medals_names_and_own_rank_footer(self):
        await self.db.set_first_name(OWNER, "نگار")
        await self.db.set_first_name(777, "سارا")
        await self.db.db.execute("UPDATE users SET quiz_points=50 WHERE telegram_id=?", (777,))
        await self.db.db.execute("UPDATE users SET quiz_points=30 WHERE telegram_id=?", (OWNER,))
        await self.db.db.commit()
        event = self._event(b"board", OWNER)
        await self.bot._handle_callback(event)
        text = event.edits[-1]["text"]
        self.assertIn("🥇 سارا", text)
        self.assertIn("🥈 نگار", text)
        self.assertIn("رتبهٔ شما: ۲ با ۳۰ امتیاز", text)
        # No telegram id ever reaches the public board.
        self.assertNotIn(str(OWNER), text)

    async def test_missing_name_renders_fallback_only(self):
        await self.db.db.execute("UPDATE users SET quiz_points=10 WHERE telegram_id=?", (OWNER,))
        await self.db.db.commit()
        event = self._event(b"board", OTHER)
        await self.bot._handle_callback(event)
        self.assertIn("کاربر گاماس", event.edits[-1]["text"])

    async def test_admin_gift_flow_credits_audits_and_dms(self):
        await self.db.set_first_name(OWNER, "نگار")
        await self.db.db.execute("UPDATE users SET quiz_points=40 WHERE telegram_id=?", (OWNER,))
        await self.db.db.commit()
        board = self._event(b"adm:board", self.admin_id)
        await self.bot._handle_callback(board)
        board_text = board.edits[-1]["text"]
        self.assertIn("نگار", board_text)
        gift_payloads = menu_payloads(board.edits[-1]["buttons"])
        self.assertIn(f"adm:bg:{OWNER}".encode(), gift_payloads)
        # Plan picker.
        pick = self._event(f"adm:bg:{OWNER}".encode(), self.admin_id)
        await self.bot._handle_callback(pick)
        pick_payloads = menu_payloads(pick.edits[-1]["buttons"])
        self.assertIn(f"adm:bg:{OWNER}:paid_5h_30d".encode(), pick_payloads)
        # Confirmation screen §6.13.
        confirm = self._event(
            f"adm:bg:{OWNER}:paid_5h_30d".encode(), self.admin_id
        )
        await self.bot._handle_callback(confirm)
        confirm_text = confirm.edits[-1]["text"]
        self.assertIn("ثبت شود؟", confirm_text)
        self.assertIn("نگار", confirm_text)
        # Execute.
        ok = self._event(
            f"adm:bgok:{OWNER}:paid_5h_30d".encode(), self.admin_id
        )
        await self.bot._handle_callback(ok)
        self.assertEqual(await self.db.available_seconds(OWNER), 5 * 3600)
        audit = await self.db.db.execute_fetchall(
            "SELECT action, subject_user FROM audit WHERE subject_user=?", (OWNER,)
        )
        self.assertTrue(any(row[0] == "grant_credit" for row in audit))
        # DM delivered with §6.8 wording.
        dm = [record["text"] for record in self.client.texts if record["entity"] == OWNER]
        self.assertTrue(dm and "تبریک" in dm[-1] and "اعتبار رایگان" in dm[-1])
        # Board re-rendered with the success notice.
        self.assertIn("هدیه ثبت شد", ok.edits[-1]["text"])

    async def test_gift_to_another_admin_is_refused(self):
        ok = self._event(
            f"adm:bgok:{self.admin_id}:paid_5h_30d".encode(), self.admin_id
        )
        await self.bot._handle_callback(ok)
        self.assertEqual(await self.db.available_seconds(self.admin_id), 0)

    async def test_gift_to_banned_user_is_refused(self):
        await self.db.set_banned(OWNER, True)
        pick = self._event(f"adm:bg:{OWNER}".encode(), self.admin_id)
        await self.bot._handle_callback(pick)
        self.assertTrue(pick.answers[-1][1])  # alert

    async def test_non_admin_cannot_open_gift_screens(self):
        event = self._event(b"adm:board", OWNER)
        await self.bot._handle_callback(event)
        self.assertEqual(event.answers[-1][1], True)
        self.assertFalse(event.edits)


class FlashcardDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "fc.db")
        await self.db.connect()
        await self.db.ensure_user(OWNER)
        await self.db.ensure_user(OTHER)
        self.item_id = await self.db.library_add(
            OWNER, "booklet", 10, 20, None, title="جزوه"
        )

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def test_deck_lifecycle_and_cascade(self):
        deck_id = await self.db.deck_create(OWNER, self.item_id, "دسته", "ai")
        for index in range(3):
            count = await self.db.deck_add_card(deck_id, f"پرسش {index}", f"پاسخ {index}")
        self.assertEqual(count, 3)
        self.assertEqual(len(await self.db.deck_cards(deck_id)), 3)
        await self.db.deck_set_cursor(deck_id, 2)
        decks = await self.db.decks_list(OWNER)
        self.assertEqual(decks[0]["card_count"], 3)
        self.assertEqual(decks[0]["study_cursor"], 2)
        self.assertIsNone(await self.db.deck_get(deck_id, OTHER))
        self.assertTrue(await self.db.deck_delete(deck_id, OWNER))
        cards = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM flashcards WHERE deck_id=?", (deck_id,)
        )
        self.assertEqual(cards[0][0], 0)

    async def test_manual_deck_is_singular_and_itemless(self):
        first = await self.db.manual_deck_get_or_create(OWNER)
        second = await self.db.manual_deck_get_or_create(OWNER)
        self.assertEqual(first, second)
        deck = await self.db.deck_get(first, OWNER)
        self.assertEqual(deck["source"], "manual")
        self.assertIsNone(deck["item_id"])

    async def test_deck_survives_library_prune_but_quiz_sessions_do_not(self):
        deck_id = await self.db.deck_create(OWNER, self.item_id, "دسته", "ai")
        await self.db.deck_add_card(deck_id, "پ", "پا")
        questions = [{
            "question": "سؤال", "options": ["الف", "ب", "ج", "د"],
            "correct_index": 0, "explanation": "چرا",
        }]
        session_id = await self.db.quiz_create_session(OWNER, self.item_id, questions)
        await self.db.library_prune(OWNER, keep=0)
        deck = await self.db.deck_get(deck_id, OWNER)
        self.assertIsNotNone(deck)
        self.assertIsNone(deck["item_id"])  # SET NULL, deck retained
        self.assertIsNone(await self.db.quiz_get_session(session_id, OWNER))  # CASCADE


class FlashcardFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "fc.db",
            temp_dir=root / "tmp",
            flashcard_ai_cost_seconds=300,
            flashcard_ai_deck_size=6,
            flashcard_max_decks_per_user=2,
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
        await self.db.add_credit(OWNER, 3600, note="test", validity_days=30)
        self.item_id = await self.db.library_add(
            OWNER, "booklet", 10, 20, None, title="📖 جزوهٔ Word — GMS-9"
        )
        await self.db.library_set_content(self.item_id, "متن جزوه برای فلش‌کارت")
        # A transcript that must never appear in the booklet picker.
        await self.db.library_add(OWNER, "transcript", 11, 21, None, title="متن خام")

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def _event(self, data: bytes, user_id: int = OWNER) -> FakeCallbackEvent:
        event = FakeCallbackEvent(self.client, data=data, chat_id=user_id)
        event.client = self.client
        return event

    @staticmethod
    def _cards_payload(count: int) -> dict:
        return {
            "cards": [
                {"front": f"پرسش {index}", "back": f"پاسخ {index}"}
                for index in range(count)
            ]
        }

    async def test_ai_generation_debits_and_builds_deck(self):
        event = self._event(f"fc:go:{self.item_id}".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=self._cards_payload(6)),
        ) as generator:
            await self.bot._handle_callback(event)
            generator.assert_awaited_once()
        self.assertEqual(await self.db.available_seconds(OWNER), 3300)
        decks = await self.db.decks_list(OWNER)
        self.assertEqual(len(decks), 1)
        self.assertEqual(decks[0]["card_count"], 6)
        self.assertTrue(decks[0]["title"].endswith("— فلش‌کارت"))
        cards = await self.db.deck_cards(decks[0]["deck_id"])
        self.assertEqual(cards[0]["front"], "پرسش 0")

    async def test_start_screen_shows_cost_and_size(self):
        event = self._event(f"fc:ai:{self.item_id}".encode())
        await self.bot._handle_callback(event)
        text = event.edits[-1]["text"]
        self.assertIn("هزینه: ۵ دقیقه اعتبار", text)
        self.assertIn("۶ پرسش و پاسخ", text)

    async def test_failure_refunds_and_short_output_refunds(self):
        event = self._event(f"fc:go:{self.item_id}".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=self._cards_payload(2)),  # < 5 valid
        ):
            await self.bot._handle_callback(event)
        self.assertEqual(await self.db.available_seconds(OWNER), 3600)
        self.assertEqual(await self.db.deck_count(OWNER), 0)
        self.assertIn(LEARNING_GENERATION_FAILED, self.client.texts[-1]["text"])

    async def test_insufficient_balance_denies_without_generation(self):
        await self.db.db.execute("DELETE FROM ledger WHERE user_id=?", (OWNER,))
        await self.db.db.commit()
        event = self._event(f"fc:go:{self.item_id}".encode())
        with mock.patch("gamas_bot.bot.complete_json") as generator:
            await self.bot._handle_callback(event)
            generator.assert_not_called()
        self.assertIn(LEARNING_NO_CREDIT, self.client.texts[-1]["text"])

    async def test_hub_empty_state_and_manual_guided_flow_with_disarm(self):
        hub = self._event(b"fc:hub")
        await self.bot._handle_callback(hub)
        self.assertIn("هنوز مجموعه‌ای نساخته‌اید", hub.edits[-1]["text"])
        # Arm the manual conversation.
        await self.bot._handle_callback(self._event(b"fc:man"))
        self.assertEqual(self.bot._pending_flashcard[OWNER][1], "front")
        front = FakeMessageEvent(OWNER, "پرسش دستی یکم؟")
        await self.bot._handle_message(front)
        self.assertEqual(self.bot._pending_flashcard[OWNER][1], "back")
        self.assertIn("پاسخ کارت", front.replies[-1])
        back = FakeMessageEvent(OWNER, "پاسخ دستی یکم.")
        await self.bot._handle_message(back)
        self.assertIn("کارت اضافه شد (۱ کارت)", back.replies[-1])
        deck_id = self.bot._pending_flashcard[OWNER][0]
        # Finishing disarms.
        done = self._event(b"fc:man:done")
        await self.bot._handle_callback(done)
        self.assertNotIn(OWNER, self.bot._pending_flashcard)
        cards = await self.db.deck_cards(deck_id)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["front"], "پرسش دستی یکم؟")
        # A stray text after a navigation tap never becomes a card.
        await self.bot._handle_callback(self._event(b"fc:man"))
        await self.bot._handle_callback(self._event(b"library"))
        stray = FakeMessageEvent(OWNER, "این متن نباید کارت شود")
        await self.bot._handle_message(stray)
        self.assertEqual(len(await self.db.deck_cards(deck_id)), 1)

    async def test_study_flip_next_persists_cursor_and_restarts(self):
        deck_id = await self.db.deck_create(OWNER, None, "دسته دستی", "manual")
        for index in range(3):
            await self.db.deck_add_card(deck_id, f"پرسش {index}", f"پاسخ {index}")
        study = self._event(f"fc:study:{deck_id}".encode())
        await self.bot._handle_callback(study)
        self.assertIn("پرسش:", study.edits[-1]["text"])
        card = (await self.db.deck_cards(deck_id))[0]
        flip = self._event(f"fc:flip:{deck_id}:{card['card_id']}".encode())
        await self.bot._handle_callback(flip)
        self.assertIn("پاسخ 0", flip.edits[-1]["text"])
        nxt = self._event(f"fc:next:{deck_id}".encode())
        await self.bot._handle_callback(nxt)
        self.assertEqual((await self.db.deck_get(deck_id, OWNER))["study_cursor"], 1)
        # Walk to the end summary.
        await self.bot._handle_callback(self._event(f"fc:next:{deck_id}".encode()))
        await self.bot._handle_callback(self._event(f"fc:next:{deck_id}".encode()))
        end = self._event(f"fc:next:{deck_id}".encode())
        await self.bot._handle_callback(end)
        self.assertIn("مرور تمام شد — ۳ کارت مرور شد", end.edits[-1]["text"])
        restart = self._event(f"fc:restart:{deck_id}".encode())
        await self.bot._handle_callback(restart)
        self.assertEqual((await self.db.deck_get(deck_id, OWNER))["study_cursor"], 0)

    async def test_word_export_delivers_rtl_docx_and_removes_the_temp_file(self):
        deck_id = await self.db.deck_create(OWNER, None, "دسته آزمون", "manual")
        await self.db.deck_add_card(deck_id, "پرسش مهم؟", "پاسخ کامل")
        export = self._event(f"fc:exp:{deck_id}".encode())
        await self.bot._handle_callback(export)
        docx_paths = [
            record["path"] for record in self.client.deliveries
            if str(record["path"]).endswith(".docx")
        ]
        self.assertTrue(docx_paths)
        # The temp file is removed after delivery.
        self.assertFalse(Path(docx_paths[-1]).exists())
        payload = self.recorder.file_payloads[-1][1]
        text = docx_text(payload)
        self.assertIn("پرسش مهم؟", text)
        self.assertIn("پاسخ کامل", text)

    async def test_delete_needs_confirmation_and_cascades(self):
        deck_id = await self.db.deck_create(OWNER, None, "دسته", "manual")
        await self.db.deck_add_card(deck_id, "پ", "پا")
        confirm = self._event(f"fc:del:{deck_id}".encode())
        await self.bot._handle_callback(confirm)
        self.assertIn("آیا مطمئنید؟", confirm.edits[-1]["text"])
        self.assertIsNotNone(await self.db.deck_get(deck_id, OWNER))
        ok = self._event(f"fc:delok:{deck_id}".encode())
        await self.bot._handle_callback(ok)
        self.assertIsNone(await self.db.deck_get(deck_id, OWNER))

    async def test_picker_lists_booklets_only(self):
        picker = self._event(b"fc:pick:0")
        await self.bot._handle_callback(picker)
        payloads = menu_payloads(picker.edits[-1]["buttons"])
        self.assertIn(f"fc:ai:{self.item_id}".encode(), payloads)
        self.assertFalse(any(b"transcript" in payload for payload in payloads))

    async def test_deck_cap_is_enforced(self):
        await self.db.deck_create(OWNER, self.item_id, "یک", "ai")
        await self.db.deck_create(OWNER, self.item_id, "دو", "ai")
        event = self._event(f"fc:go:{self.item_id}".encode())
        with mock.patch("gamas_bot.bot.complete_json") as generator:
            await self.bot._handle_callback(event)
            generator.assert_not_called()
        self.assertTrue(any("حداکثر" in t["text"] for t in self.client.texts))

    async def test_foreign_deck_callbacks_fail_closed(self):
        deck_id = await self.db.deck_create(OWNER, None, "دسته", "manual")
        event = self._event(f"fc:deck:{deck_id}".encode(), user_id=OTHER)
        await self.bot._handle_callback(event)
        self.assertEqual(event.answers[-1][1], True)


class QuizFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "flow.db",
            temp_dir=root / "tmp",
            quiz_cost_seconds_per_question=60,
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.client = FakeTelegramClient()
        await self.db.ensure_user(OWNER)
        await self.db.add_credit(OWNER, 3600, note="test", validity_days=30)
        self.item_id = await self.db.library_add(
            OWNER, "booklet", 10, 20, None, title="جزوهٔ آزمون"
        )
        await self.db.library_set_content(self.item_id, "متن کامل جزوه برای ساخت آزمون")

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def _event(self, data: bytes, user_id: int = OWNER) -> FakeCallbackEvent:
        event = FakeCallbackEvent(self.client, data=data, chat_id=user_id)
        event.client = self.client
        return event

    @staticmethod
    def _payloads(buttons) -> set[bytes]:
        return menu_payloads(buttons)

    @staticmethod
    def _labels(buttons) -> list[str]:
        return [button.text for row in buttons for button in row]

    async def test_quiz_start_screen_shows_three_costs(self):
        event = self._event(f"quiz:new:{self.item_id}".encode())
        await self.bot._handle_callback(event)
        record = event.edits[-1]
        self.assertIn("آزمون از جزوه", record["text"])
        for count in (5, 10, 15):
            self.assertIn(
                f"quiz:go:{self.item_id}:{count}".encode(),
                self._payloads(record["buttons"]),
            )

    async def test_generation_debits_then_full_quiz_completes_with_rank(self):
        with mock.patch("gamas_bot.learning.random.shuffle"):
            with mock.patch(
                "gamas_bot.bot.complete_json",
                new=mock.AsyncMock(return_value=quiz_payload(5)),
            ) as generator:
                event = self._event(f"quiz:go:{self.item_id}:5".encode())
                await self.bot._handle_callback(event)
                generator.assert_awaited_once()
        # A lot-aware debit exists with allocations.
        debits = await self.db.db.execute_fetchall(
            "SELECT id, seconds, note FROM ledger WHERE user_id=? AND kind='debit'",
            (OWNER,),
        )
        self.assertEqual(len(debits), 1)
        self.assertEqual(debits[0][1], 300)
        allocations = await self.db.db.execute_fetchall(
            "SELECT COUNT(*), SUM(seconds) FROM debit_allocations WHERE debit_id=?",
            (debits[0][0],),
        )
        self.assertEqual(tuple(allocations[0]), (1, 300))
        self.assertEqual(await self.db.available_seconds(OWNER), 3300)
        session_rows = await self.db.db.execute_fetchall(
            "SELECT session_id FROM quiz_sessions WHERE user_id=?", (OWNER,)
        )
        session_id = session_rows[0][0]
        # Answer all five questions, alternating correct (option 0, no shuffle).
        for index in range(5):
            question = await self.db.quiz_current_question(session_id)
            answer = self._event(
                f"quiz:ans:{session_id}:{question['question_id']}:0".encode()
            )
            await self.bot._handle_callback(answer)
            reveal = answer.edits[-1]
            self.assertIn("✅ پاسخ درست", reveal["text"])
            if index < 4:
                self.assertTrue(
                    any("سؤال بعدی" in label for label in self._labels(reveal["buttons"]))
                )
                next_event = self._event(f"quiz:next:{session_id}".encode())
                await self.bot._handle_callback(next_event)
        # Finish screen on the last reveal's next tap.
        next_event = self._event(f"quiz:next:{session_id}".encode())
        await self.bot._handle_callback(next_event)
        finish = next_event.edits[-1]
        self.assertIn("آزمون تمام شد", finish["text"])
        self.assertIn("امتیاز کسب‌شده: ۵۰", finish["text"])
        self.assertIn(b"board", self._payloads(finish["buttons"]))
        self.assertEqual((await self.db.leaderboard_rank(OWNER))["points"], 50)

    async def test_llm_failure_refunds_in_full_and_shows_notice(self):
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(side_effect=LLMError("boom")),
        ):
            event = self._event(f"quiz:go:{self.item_id}:5".encode())
            await self.bot._handle_callback(event)
        self.assertEqual(await self.db.available_seconds(OWNER), 3600)
        self.assertIn(LEARNING_GENERATION_FAILED, self.client.texts[-1]["text"])
        sessions = await self.db.db.execute_fetchall("SELECT COUNT(*) FROM quiz_sessions")
        self.assertEqual(sessions[0][0], 0)
        refund = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE note LIKE 'refund:%'"
        )
        self.assertEqual(refund[0][0], 1)

    async def test_validation_shortfall_refunds(self):
        short = {"questions": quiz_payload(2)["questions"][:2]}
        with mock.patch(
            "gamas_bot.bot.complete_json", new=mock.AsyncMock(return_value=short)
        ):
            event = self._event(f"quiz:go:{self.item_id}:5".encode())
            await self.bot._handle_callback(event)
        self.assertEqual(await self.db.available_seconds(OWNER), 3600)
        self.assertIn(LEARNING_GENERATION_FAILED, self.client.texts[-1]["text"])

    async def test_insufficient_balance_denies_without_debit(self):
        await self.db.db.execute(
            "DELETE FROM ledger WHERE user_id=?", (OWNER,)
        )
        await self.db.db.commit()
        event = self._event(f"quiz:go:{self.item_id}:5".encode())
        with mock.patch("gamas_bot.bot.complete_json") as generator:
            await self.bot._handle_callback(event)
            generator.assert_not_called()
        self.assertIn(LEARNING_NO_CREDIT, self.client.texts[-1]["text"])
        debits = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE kind='debit'"
        )
        self.assertEqual(debits[0][0], 0)

    async def test_double_tap_answer_gets_friendly_ack_and_one_row(self):
        with mock.patch("gamas_bot.learning.random.shuffle"), mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(5)),
        ):
            await self.bot._handle_callback(
                self._event(f"quiz:go:{self.item_id}:5".encode())
            )
        session_id = (await self.db.db.execute_fetchall(
            "SELECT session_id FROM quiz_sessions"
        ))[0][0]
        question = await self.db.quiz_current_question(session_id)
        data = f"quiz:ans:{session_id}:{question['question_id']}:0".encode()
        await self.bot._handle_callback(self._event(data))
        second = self._event(data)
        await self.bot._handle_callback(second)
        self.assertEqual(second.answers[-1], ("پاسخ شما ثبت شده بود.", False))
        rows = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM quiz_answers WHERE question_id=?",
            (question["question_id"],),
        )
        self.assertEqual(rows[0][0], 1)

    async def test_foreign_user_and_stale_callbacks_fail_closed(self):
        with mock.patch("gamas_bot.learning.random.shuffle"), mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(5)),
        ):
            await self.bot._handle_callback(
                self._event(f"quiz:go:{self.item_id}:5".encode())
            )
        session_id = (await self.db.db.execute_fetchall(
            "SELECT session_id FROM quiz_sessions"
        ))[0][0]
        question = await self.db.quiz_current_question(session_id)
        foreign = self._event(
            f"quiz:ans:{session_id}:{question['question_id']}:0".encode(),
            user_id=OTHER,
        )
        await self.bot._handle_callback(foreign)
        self.assertEqual(foreign.answers[-1][1], True)  # alert
        self.assertIn("معتبر نیست", foreign.answers[-1][0])
        # Malformed callback data.
        bogus = self._event(b"quiz:ans:abc:def:0")
        await self.bot._handle_callback(bogus)
        self.assertEqual(bogus.answers[-1][1], True)

    async def test_quit_shows_partial_summary_and_keeps_points(self):
        with mock.patch("gamas_bot.learning.random.shuffle"), mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(5)),
        ):
            await self.bot._handle_callback(
                self._event(f"quiz:go:{self.item_id}:5".encode())
            )
        session_id = (await self.db.db.execute_fetchall(
            "SELECT session_id FROM quiz_sessions"
        ))[0][0]
        question = await self.db.quiz_current_question(session_id)
        await self.bot._handle_callback(
            self._event(f"quiz:ans:{session_id}:{question['question_id']}:0".encode())
        )
        quit_event = self._event(f"quiz:quit:{session_id}".encode())
        await self.bot._handle_callback(quit_event)
        text = quit_event.edits[-1]["text"]
        self.assertIn("آزمون پایان یافت", text)
        self.assertIn("امتیاز کسب‌شده: ۱۰", text)
        self.assertEqual((await self.db.leaderboard_rank(OWNER))["points"], 10)

    async def test_legacy_item_without_content_shows_notice(self):
        legacy = await self.db.library_add(
            OWNER, "booklet", 11, 21, None, title="جزوهٔ قدیمی"
        )
        event = self._event(f"quiz:new:{legacy}".encode())
        await self.bot._handle_callback(event)
        self.assertIn("قدیمی ذخیره نشده است", event.edits[-1]["text"])

    async def test_unlimited_user_quizzes_for_free(self):
        await self.db.set_unlimited(OWNER, True, admin_id=1, reason="vip")
        with mock.patch("gamas_bot.learning.random.shuffle"), mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(5)),
        ):
            event = self._event(f"quiz:go:{self.item_id}:5".encode())
            await self.bot._handle_callback(event)
        debits = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE kind='debit'"
        )
        self.assertEqual(debits[0][0], 0)
        sessions = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM quiz_sessions WHERE status='active'"
        )
        self.assertEqual(sessions[0][0], 1)

    async def test_concurrent_tap_is_rejected_before_a_second_debit(self):
        async def hold_then_answer(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return quiz_payload(5)

        with mock.patch(
            "gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=hold_then_answer)
        ):
            first = self._event(f"quiz:go:{self.item_id}:5".encode())
            second = self._event(f"quiz:go:{self.item_id}:5".encode())
            await asyncio.gather(
                self.bot._handle_callback(first),
                self.bot._handle_callback(second),
            )
        # Only one generation and one debit; the second tap was rejected.
        debits = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE kind='debit'"
        )
        self.assertEqual(debits[0][0], 1)
        sessions = await self.db.db.execute_fetchall("SELECT COUNT(*) FROM quiz_sessions")
        self.assertEqual(sessions[0][0], 1)
        self.assertTrue(
            any(
                answer is not None and "در حال پردازش" in answer
                for answer, _alert in second.answers
            )
        )
