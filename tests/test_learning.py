"""Learning loop: quiz engine, leaderboard, admin gifts and flashcards.

These tests exercise the screens through the same callback dispatcher the bot
uses in production, so a broken callback payload, a missing ownership check or
a charge that is neither recorded nor refunded fails here rather than in a chat.
Billing assertions read the ledger directly: a quiz that "works" but leaves the
credit untouched (or double-charged) is a bug, not a detail.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gamas_bot.billing import plan_catalog
from gamas_bot.bot import GamasBot
from gamas_bot.db import MIGRATIONS_DIR, Database, InsufficientBalanceError
from gamas_bot.docx_export import DocumentMeta
from gamas_bot.jobs import Job
from gamas_bot.learning import (
    MAX_CARD_BACK_CHARS,
    MAX_CARD_FRONT_CHARS,
    LearningContentError,
    bound_content,
    deck_document_body,
    normalize_cards,
    normalize_questions,
)
from gamas_bot.notes import StructuredNotes
from tests.support import FakeJobEvent, FakeTelegramClient, docx_text, make_settings
from tests.test_bot import FakeCallbackEvent, FakeMessageEvent, menu_payloads

OWNER = 555
OTHER = 556
THIRD = 557
ADMIN = 777


class _Progress:
    async def update(self, *_args, **_kwargs) -> None:
        pass

    async def complete(self, *_args, **_kwargs) -> None:
        pass


class _RenderedDocument:
    """The transcript-document double: only ``llm_document`` is used here."""

    def __init__(self, text: str) -> None:
        self._text = text

    def llm_document(self) -> str:
        return self._text


def _questions(count: int) -> list[dict]:
    return [
        {
            "question": f"سؤال شمارهٔ {index}",
            "options": ["گزینهٔ الف", "گزینهٔ ب", "گزینهٔ ج", "گزینهٔ د"],
            "correct": 0,
            "explanation": f"توضیح سؤال {index}",
        }
        for index in range(count)
    ]


def _cards(count: int = 5) -> list[dict]:
    return [
        {"front": f"پرسش {index}", "back": f"پاسخ {index}"} for index in range(count)
    ]


class LearningTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared fixture: a real database, a signature-enforcing fake client."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=self.root / "learning.db",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({ADMIN}),
            library_max_items=20,
        )
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.record = SimpleNamespace(
            replies=[], files=[], file_payloads=[], edits=[]
        )
        self.client = FakeTelegramClient(self.record)
        self.bot.client = self.client
        # No provider transport is involved: every generation is patched.
        self.bot._http_session = lambda: None  # type: ignore[method-assign]
        for user in (OWNER, OTHER, THIRD, ADMIN):
            await self.db.ensure_user(user)

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    async def _press(self, user_id: int, data: str | bytes) -> FakeCallbackEvent:
        payload = data.encode("ascii") if isinstance(data, str) else data
        event = FakeCallbackEvent(user_id, payload)
        event.client = self.client
        await self.bot._handle_callback(event)
        return event

    async def _booklet(
        self,
        user_id: int = OWNER,
        *,
        content: str | None = "متن کامل جزوهٔ درس",
        title: str = "جزوهٔ درس",
        message_id: int = 100,
        kind: str = "booklet",
    ) -> int:
        return await self.db.library_add(
            user_id,
            kind,
            user_id,
            message_id,
            title=title,
            content_text=content,
        )

    async def _credit(self, user_id: int = OWNER, seconds: int = 3600) -> None:
        await self.db.add_credit(user_id, seconds, note="test credit")

    async def _open_quiz(
        self, count: int = 5, *, questions: list[dict] | None = None
    ) -> tuple[int, int]:
        item_id = await self._booklet()
        with mock.patch(
            "gamas_bot.bot.generate_quiz",
            new=mock.AsyncMock(return_value=questions or _questions(count)),
        ):
            await self._press(OWNER, f"quiz:go:{item_id}:{count}")
        session = await self.db.quiz_active_session(OWNER)
        assert session is not None
        return item_id, int(session["session_id"])

    async def _answer(self, session_id: int, option: int = 0) -> FakeCallbackEvent:
        question = await self.db.quiz_current_question(session_id)
        assert question is not None
        return await self._press(
            OWNER,
            f"quiz:ans:{session_id}:{int(question['question_id'])}:{option}",
        )


# ===========================================================================
# Schema and display names
# ===========================================================================
class MigrationTests(LearningTestCase):
    async def test_011_and_012_upgrade_a_populated_post_v1_database(self):
        path = self.root / "pre-learning.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration.name >= "011_learning_content.sql":
                    break
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    (migration.name, "2026-10-26T00:00:00+00:00"),
                )
            connection.execute(
                "INSERT INTO users(telegram_id, joined_at) VALUES (9090, '2026-10-01T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO library_items(user_id, kind, chat_id, message_id, title, created_at) "
                "VALUES (9090, 'booklet', 9090, 41, 'قدیمی', '2026-10-01T00:00:00+00:00')"
            )
            connection.commit()
        finally:
            connection.close()

        upgraded = Database(path)
        await upgraded.connect()
        try:
                self.assertEqual(await upgraded.count_users(), 1)
                columns = {
                    row[1]
                    for row in await upgraded.db.execute_fetchall(
                        "PRAGMA table_info(users)"
                    )
                }
                self.assertTrue({"first_name", "quiz_points"} <= columns)
                library_columns = {
                    row[1]
                    for row in await upgraded.db.execute_fetchall(
                        "PRAGMA table_info(library_items)"
                    )
                }
                self.assertIn("content_text", library_columns)
                for table in (
                    "quiz_sessions",
                    "quiz_questions",
                    "quiz_answers",
                    "flashcard_decks",
                    "flashcards",
                ):
                    found = await upgraded.db.execute_fetchall(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    )
                    self.assertEqual(len(found), 1, table)
                # A pre-learning row stays legacy instead of being invented.
                rows = await upgraded.library_list(9090, limit=5)
                self.assertEqual(len(rows), 1)
                item = await upgraded.library_item(9090, int(rows[0]["item_id"]))
                assert item is not None
                self.assertIsNone(item["content_text"])
                applied = {
                    row[0]
                    for row in await upgraded.db.execute_fetchall(
                        "SELECT name FROM schema_migrations"
                    )
                }
                self.assertTrue(
                    {"011_learning_content.sql", "012_learning.sql"} <= applied
                )
        finally:
            await upgraded.close()


class DisplayNameTests(LearningTestCase):
    async def test_first_name_is_bounded_and_never_erased_by_a_missing_one(self):
        await self.db.ensure_user(OWNER, "  علی   رضایی ")
        user = await self.db.ensure_user(OWNER)
        self.assertEqual(user.first_name, "علی رضایی")

        await self.db.ensure_user(OWNER, "x" * 200)
        user = await self.db.ensure_user(OWNER)
        self.assertEqual(len(user.first_name), 64)

        await self.db.ensure_user(OWNER, None)
        kept = await self.db.ensure_user(OWNER)
        self.assertEqual(len(kept.first_name), 64)


# ===========================================================================
# Direct spending (quizzes and AI decks are not submissions)
# ===========================================================================
class SpendTests(LearningTestCase):
    async def test_spend_records_a_lot_aware_debit(self):
        await self.db.add_credit(OWNER, 600, note="purchase")
        await self.db.spend_seconds(OWNER, 240, note="quiz-pending:1")

        self.assertEqual(await self.db.available_seconds(OWNER), 360)
        rows = await self.db.read_db.execute_fetchall(
            "SELECT id, seconds, kind, note FROM ledger WHERE user_id = ? "
            "AND kind = 'debit'",
            (OWNER,),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0][1]), 240)
        self.assertEqual(rows[0][3], "quiz-pending:1")
        allocations = await self.db.read_db.execute_fetchall(
            "SELECT SUM(seconds) FROM debit_allocations WHERE debit_id = ?",
            (int(rows[0][0]),),
        )
        self.assertEqual(int(allocations[0][0]), 240)

    async def test_spend_beyond_the_balance_raises_and_writes_nothing(self):
        await self.db.add_credit(OWNER, 100, note="purchase")
        with self.assertRaises(InsufficientBalanceError):
            await self.db.spend_seconds(OWNER, 101, note="quiz-pending:1")
        self.assertEqual(await self.db.available_seconds(OWNER), 100)
        rows = await self.db.read_db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE user_id = ? AND kind = 'debit'",
            (OWNER,),
        )
        self.assertEqual(int(rows[0][0]), 0)

    async def test_refund_returns_a_non_expiring_credit(self):
        await self.db.add_credit(OWNER, 300, note="purchase")
        await self.db.spend_seconds(OWNER, 300, note="quiz-pending:1")
        self.assertEqual(await self.db.available_seconds(OWNER), 0)
        await self.db.refund_seconds(OWNER, 300, note="quiz:1")
        self.assertEqual(await self.db.available_seconds(OWNER), 300)
        rows = await self.db.read_db.execute_fetchall(
            "SELECT expires_at, note FROM ledger WHERE user_id = ? AND kind = 'credit' "
            "ORDER BY id DESC LIMIT 1",
            (OWNER,),
        )
        self.assertIsNone(rows[0][0])
        self.assertEqual(rows[0][1], "refund: quiz:1")

    async def test_unlimited_users_are_never_charged_or_refused(self):
        await self.db.set_unlimited(OWNER, True, admin_id=ADMIN)
        await self.db.spend_seconds(OWNER, 9999, note="quiz-pending:1")
        rows = await self.db.read_db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE user_id = ? AND kind = 'debit'",
            (OWNER,),
        )
        self.assertEqual(int(rows[0][0]), 0)


# ===========================================================================
# Booklet detail screen
# ===========================================================================
class LibraryDetailTests(LearningTestCase):
    async def test_booklet_detail_offers_forward_quiz_and_flashcards(self):
        item_id = await self._booklet()
        event = await self._press(OWNER, f"lib:it:{item_id}")
        text, buttons = event.edits[-1]
        payloads = menu_payloads(buttons)
        self.assertIn("📖 جزوهٔ درس", text)
        self.assertIn(f"lib:f:{item_id}".encode("ascii"), payloads)
        self.assertIn(f"quiz:new:{item_id}".encode("ascii"), payloads)
        self.assertIn(f"fc:ai:{item_id}".encode("ascii"), payloads)
        self.assertIn(b"lib:0", payloads)

    async def test_legacy_item_shows_the_notice_and_only_forwards(self):
        item_id = await self._booklet(content=None)
        event = await self._press(OWNER, f"lib:it:{item_id}")
        text, buttons = event.edits[-1]
        payloads = menu_payloads(buttons)
        self.assertIn(
            "متن این جزوهٔ قدیمی ذخیره نشده است؛ آزمون و فلش‌کارت فقط برای "
            "جزوه‌های جدید فعال است.",
            text,
        )
        self.assertIn(f"lib:f:{item_id}".encode("ascii"), payloads)
        self.assertNotIn(f"quiz:new:{item_id}".encode("ascii"), payloads)
        self.assertNotIn(f"fc:ai:{item_id}".encode("ascii"), payloads)

    async def test_another_users_item_is_not_openable(self):
        item_id = await self._booklet()
        event = await self._press(OTHER, f"lib:it:{item_id}")
        self.assertEqual(event.answers[-1][0], "این فایل دیگر در دسترس نیست.")
        self.assertTrue(event.answers[-1][1])


# ===========================================================================
# Quiz engine
# ===========================================================================
class QuizTests(LearningTestCase):
    async def test_start_screen_renders_the_three_counts_with_config_costs(self):
        item_id = await self._booklet()
        event = await self._press(OWNER, f"quiz:new:{item_id}")
        text, buttons = event.edits[-1]
        self.assertIn("هر پاسخ درست ۱۰ امتیاز دارد", text)
        payloads = menu_payloads(buttons)
        for count in (5, 10, 15):
            self.assertIn(
                f"quiz:go:{item_id}:{count}".encode("ascii"), payloads
            )
        labels = " ".join(button.text for row in buttons for button in row)
        self.assertIn("۵ سؤال — ۵ دقیقه اعتبار", labels)
        self.assertIn("۱۵ سؤال — ۱۵ دقیقه اعتبار", labels)

    async def test_generation_charges_first_then_asks_question_one(self):
        await self._credit(OWNER, 600)
        item_id, session_id = await self._open_quiz(5)

        self.assertEqual(await self.db.available_seconds(OWNER), 300)
        question = await self.db.quiz_current_question(session_id)
        assert question is not None
        self.assertEqual(question["idx"], 0)
        self.assertEqual(len(question["options"]), 4)

        shown = await self._press(OWNER, f"quiz:next:{session_id}")
        self.assertIn("🧪 سؤال ۱ از ۵", shown.edits[-1][0])
        payloads = menu_payloads(shown.edits[-1][1])
        self.assertTrue(any(p.startswith(b"quiz:ans:") for p in payloads))
        self.assertIn(f"quiz:quit:{session_id}".encode("ascii"), payloads)

    async def test_correct_answer_reveals_and_scores_once(self):
        await self._credit(OWNER, 600)
        _, session_id = await self._open_quiz(5)

        event = await self._answer(session_id, 0)
        text = event.edits[-1][0]
        self.assertIn("✅ پاسخ درست: گزینهٔ الف", text)
        self.assertNotIn("❌", text)
        self.assertIn("💡 توضیح سؤال 0", text)
        self.assertIn("سؤال بعدی ➡️", " ".join(
            button.text for row in event.edits[-1][1] for button in row
        ))

        self.assertEqual(await self.db.user_points(OWNER), 10)
        rows = await self.db.read_db.execute_fetchall(
            "SELECT COUNT(*) FROM quiz_answers WHERE session_id = ?", (session_id,)
        )
        self.assertEqual(int(rows[0][0]), 1)

    async def test_wrong_answer_reveals_the_correct_option_without_points(self):
        await self._credit(OWNER, 600)
        _, session_id = await self._open_quiz(5)

        event = await self._answer(session_id, 2)
        text = event.edits[-1][0]
        self.assertIn("❌ پاسخ شما درست نبود.", text)
        self.assertIn("✅ پاسخ درست: گزینهٔ الف", text)
        self.assertEqual(await self.db.user_points(OWNER), 0)

    async def test_double_tap_is_acknowledged_and_never_scores_twice(self):
        await self._credit(OWNER, 600)
        _, session_id = await self._open_quiz(5)
        question = await self.db.quiz_current_question(session_id)
        assert question is not None
        payload = f"quiz:ans:{session_id}:{int(question['question_id'])}:0"

        await self._press(OWNER, payload)
        again = await self._press(OWNER, payload)

        self.assertEqual(again.answers[-1][0], "پاسخ شما ثبت شده بود.")
        self.assertEqual(await self.db.user_points(OWNER), 10)
        rows = await self.db.read_db.execute_fetchall(
            "SELECT COUNT(*) FROM quiz_answers WHERE question_id = ?",
            (int(question["question_id"]),),
        )
        self.assertEqual(int(rows[0][0]), 1)

    async def test_another_users_session_cannot_be_answered(self):
        await self._credit(OWNER, 600)
        _, session_id = await self._open_quiz(5)
        question = await self.db.quiz_current_question(session_id)
        assert question is not None

        foreign = await self._press(
            OTHER,
            f"quiz:ans:{session_id}:{int(question['question_id'])}:0",
        )
        self.assertEqual(foreign.answers[-1][0], "این آزمون دیگر معتبر نیست.")
        self.assertEqual(await self.db.user_points(OTHER), 0)
        rows = await self.db.read_db.execute_fetchall(
            "SELECT COUNT(*) FROM quiz_answers WHERE session_id = ?", (session_id,)
        )
        self.assertEqual(int(rows[0][0]), 0)

    async def test_out_of_turn_taps_are_refused(self):
        await self._credit(OWNER, 600)
        _, session_id = await self._open_quiz(5)
        first = await self.db.quiz_current_question(session_id)
        assert first is not None
        await self._press(
            OWNER, f"quiz:ans:{session_id}:{int(first['question_id'])}:0"
        )

        # The cursor has moved on, so the same button must not score again.
        stale = await self._press(
            OWNER, f"quiz:ans:{session_id}:{int(first['question_id'])}:1"
        )
        self.assertIn(
            stale.answers[-1][0],
            ("پاسخ شما ثبت شده بود.", "این آزمون دیگر معتبر نیست."),
        )
        self.assertEqual(await self.db.user_points(OWNER), 10)

        # A question that is not the cursor's question is refused outright.
        forged = await self._press(OWNER, f"quiz:ans:{session_id}:999999:0")
        self.assertEqual(forged.answers[-1][0], "این آزمون دیگر معتبر نیست.")

    async def test_finishing_every_question_produces_the_summary_and_rank(self):
        await self._credit(OWNER, 600)
        _, session_id = await self._open_quiz(5)
        for _ in range(5):
            await self._answer(session_id, 0)

        frame = await self._press(OWNER, f"quiz:next:{session_id}")
        text = frame.edits[-1][0]
        self.assertIn("🎉 آزمون تمام شد!", text)
        self.assertIn("پاسخ‌های درست: ۵ از ۵", text)
        self.assertIn("امتیاز کسب‌شده: ۵۰", text)
        self.assertIn("رتبهٔ شما در لیدربورد: ۱", text)
        payloads = menu_payloads(frame.edits[-1][1])
        self.assertIn(b"board", payloads)
        self.assertIn(b"library", payloads)

        session = await self.db.quiz_get_session(OWNER, session_id)
        assert session is not None
        self.assertEqual(session["status"], "done")
        self.assertEqual(int(session["score"]), 50)

    async def test_quitting_keeps_the_points_already_earned(self):
        await self._credit(OWNER, 600)
        _, session_id = await self._open_quiz(5)
        await self._answer(session_id, 0)

        frame = await self._press(OWNER, f"quiz:quit:{session_id}")
        self.assertIn("🎉 آزمون تمام شد!", frame.edits[-1][0])
        self.assertIn("امتیاز کسب‌شده: ۱۰", frame.edits[-1][0])
        self.assertEqual(await self.db.user_points(OWNER), 10)

    async def test_generation_failure_refunds_the_whole_charge(self):
        await self._credit(OWNER, 600)
        item_id = await self._booklet()
        with mock.patch(
            "gamas_bot.bot.generate_quiz",
            new=mock.AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            event = await self._press(OWNER, f"quiz:go:{item_id}:5")

        self.assertIn(
            "ساخت محتوا ناموفق بود؛ اعتبار شما کامل برگشت داده شد.",
            event.responses[-1][0],
        )
        self.assertEqual(await self.db.available_seconds(OWNER), 600)
        self.assertIsNone(await self.db.quiz_active_session(OWNER))

    async def test_unusable_content_is_treated_as_a_failure_and_refunded(self):
        await self._credit(OWNER, 600)
        item_id = await self._booklet()
        with mock.patch(
            "gamas_bot.bot.generate_quiz",
            new=mock.AsyncMock(side_effect=LearningContentError("only 1 usable")),
        ):
            event = await self._press(OWNER, f"quiz:go:{item_id}:5")

        self.assertIn("برگشت داده شد", event.responses[-1][0])
        self.assertEqual(await self.db.available_seconds(OWNER), 600)

    async def test_no_credit_offers_the_wallet_and_never_calls_the_provider(self):
        item_id = await self._booklet()
        with mock.patch(
            "gamas_bot.bot.generate_quiz", new=mock.AsyncMock()
        ) as generate:
            event = await self._press(OWNER, f"quiz:go:{item_id}:5")

        self.assertIn(
            "اعتبار شما برای این کار کافی نیست", event.edits[-1][0]
        )
        self.assertIn(b"wallet", menu_payloads(event.edits[-1][1]))
        generate.assert_not_awaited()
        self.assertIsNone(await self.db.quiz_active_session(OWNER))

    async def test_legacy_item_cannot_start_a_quiz(self):
        item_id = await self._booklet(content=None)
        event = await self._press(OWNER, f"quiz:new:{item_id}")
        self.assertIn(
            "این آزمون دیگر معتبر نیست.", [text for text, _ in event.answers]
        )
        self.assertIn(
            "متن این جزوهٔ قدیمی ذخیره نشده است", event.edits[-1][0]
        )


# ===========================================================================
# Leaderboard
# ===========================================================================
class LeaderboardTests(LearningTestCase):
    async def _seed(self, user_id: int, points: int, name: str) -> None:
        await self.db.set_first_name(user_id, name)
        connection = sqlite3.connect(self.settings.db_path)
        try:
            connection.execute(
                "UPDATE users SET quiz_points = ? WHERE telegram_id = ?",
                (points, user_id),
            )
            connection.commit()
        finally:
            connection.close()

    async def test_board_orders_by_points_and_shows_only_first_names(self):
        await self._seed(OWNER, 30, "علی")
        await self._seed(OTHER, 50, "مریم")
        await self._seed(THIRD, 10, "حسن")

        event = await self._press(OWNER, "board")
        text = event.edits[-1][0]
        self.assertIn("🏆 لیدربورد", text)
        self.assertLess(text.index("مریم"), text.index("علی"))
        self.assertLess(text.index("علی"), text.index("حسن"))
        self.assertIn("🥇 مریم", text)
        self.assertIn("🥈 علی", text)
        self.assertIn("🥉 حسن", text)
        self.assertIn("رتبهٔ شما: ۲ با ۳۰ امتیاز", text)
        self.assertNotIn(str(OWNER), text)

    async def test_banned_users_are_excluded(self):
        await self._seed(OWNER, 30, "علی")
        await self._seed(OTHER, 90, "مریم")
        await self.db.set_banned(OTHER, True, admin_id=ADMIN)

        event = await self._press(OWNER, "board")
        text = event.edits[-1][0]
        self.assertNotIn("مریم", text)
        self.assertIn("رتبهٔ شما: ۱ با ۳۰ امتیاز", text)

    async def test_empty_board_uses_the_empty_state(self):
        event = await self._press(OWNER, "board")
        self.assertEqual(
            event.edits[-1][0],
            "🏆 لیدربورد\n\nهنوز امتیازی ثبت نشده است.\n"
            "اولین آزمون خود را از «📚 کتابخانه من» شروع کنید.",
        )

    async def test_missing_name_falls_back_to_the_generic_label(self):
        await self._seed(OWNER, 20, "")
        event = await self._press(OWNER, "board")
        self.assertIn("کاربر گاماس", event.edits[-1][0])


# ===========================================================================
# Admin gifts
# ===========================================================================
class AdminGiftTests(LearningTestCase):
    async def _seed_top_user(self) -> None:
        await self.db.set_first_name(OWNER, "علی")
        connection = sqlite3.connect(self.settings.db_path)
        try:
            connection.execute(
                "UPDATE users SET quiz_points = 50 WHERE telegram_id = ?", (OWNER,)
            )
            connection.commit()
        finally:
            connection.close()

    def _first_paid_plan(self):
        for plan in plan_catalog(self.settings.tariff_values()):
            if not plan.is_free:
                return plan
        raise AssertionError("the catalog always has a paid plan")

    async def test_gift_flow_credits_audits_notifies_and_rerenders(self):
        await self._seed_top_user()
        board = await self._press(ADMIN, "adm:board")
        board_text, board_buttons = board.edits[-1]
        self.assertIn("علی", board_text)
        self.assertIn(b"adm:bg:555", menu_payloads(board_buttons))

        picker = await self._press(ADMIN, "adm:bg:555")
        plan = self._first_paid_plan()
        self.assertIn(
            f"adm:bg:555:{plan.code}".encode("ascii"),
            menu_payloads(picker.edits[-1][1]),
        )

        confirm = await self._press(ADMIN, f"adm:bg:555:{plan.code}")
        self.assertIn("هدیه به علی با اعتبار", confirm.edits[-1][0])
        self.assertIn(
            f"adm:bgok:555:{plan.code}".encode("ascii"),
            menu_payloads(confirm.edits[-1][1]),
        )

        before = await self.db.available_seconds(OWNER)
        applied = await self._press(ADMIN, f"adm:bgok:555:{plan.code}")
        self.assertIn("هدیه ثبت شد.", applied.edits[-1][0])
        self.assertEqual(
            await self.db.available_seconds(OWNER),
            before + plan.included_seconds,
        )

        credit = await self.db.read_db.execute_fetchall(
            "SELECT note, seconds FROM ledger WHERE user_id = ? AND kind = 'credit' "
            "ORDER BY id DESC LIMIT 1",
            (OWNER,),
        )
        self.assertEqual(credit[0][0], "leaderboard gift")
        self.assertEqual(int(credit[0][1]), plan.included_seconds)
        audit = await self.db.read_db.execute_fetchall(
            "SELECT action, subject_user FROM audit WHERE admin_id = ? "
            "AND action = 'grant_credit'",
            (ADMIN,),
        )
        self.assertEqual(int(audit[0][1]), OWNER)
        self.assertTrue(
            any("🎁 تبریک!" in entry["text"] for entry in self.client.texts),
            "the recipient must receive the gift notification",
        )

    async def test_a_target_outside_the_top_five_is_refused(self):
        event = await self._press(ADMIN, "adm:bg:999999")
        self.assertEqual(event.answers[-1][0], "این هدیه دیگر معتبر نیست.")
        credit = await self.db.read_db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE note = 'leaderboard gift'"
        )
        self.assertEqual(int(credit[0][0]), 0)

    async def test_non_admin_cannot_open_the_board(self):
        event = await self._press(OWNER, "adm:board")
        self.assertFalse(event.edits)


# ===========================================================================
# Flashcards
# ===========================================================================
class FlashcardTests(LearningTestCase):
    async def _ai_deck(self, *, cards: int = 5, charge: int = 600) -> int:
        await self._credit(OWNER, charge)
        item_id = await self._booklet(title="جزوهٔ زیست")
        with mock.patch(
            "gamas_bot.bot.generate_flashcards",
            new=mock.AsyncMock(return_value=_cards(cards)),
        ):
            await self._press(OWNER, f"fc:go:{item_id}")
        decks = await self.db.decks_list(OWNER, limit=5)
        assert decks
        return int(decks[0]["deck_id"])

    async def test_ai_deck_charges_creates_cards_and_opens_the_deck(self):
        deck_id = await self._ai_deck(cards=5)
        self.assertEqual(await self.db.available_seconds(OWNER), 300)
        deck = await self.db.deck_get(OWNER, deck_id)
        assert deck is not None
        self.assertEqual(int(deck["card_count"]), 5)
        self.assertEqual(deck["source"], "ai")
        self.assertIn("فلش‌کارت", deck["title"])

    async def test_ai_failure_refunds(self):
        await self._credit(OWNER, 600)
        item_id = await self._booklet()
        with mock.patch(
            "gamas_bot.bot.generate_flashcards",
            new=mock.AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            event = await self._press(OWNER, f"fc:go:{item_id}")
        self.assertIn("برگشت داده شد", event.edits[-1][0])
        self.assertEqual(await self.db.available_seconds(OWNER), 600)
        self.assertEqual(await self.db.decks_count(OWNER), 0)

    async def test_manual_flow_writes_cards_then_disarms_on_navigation(self):
        await self._press(OWNER, "fc:man")
        deck = await self.db.manual_deck(OWNER)
        assert deck is not None
        deck_id = int(deck["deck_id"])

        await self.bot._handle_message(FakeMessageEvent(OWNER, "پرسش دستی"))
        self.assertEqual(
            self.bot._pending_flashcard[OWNER][1], "back"
        )
        await self.bot._handle_message(FakeMessageEvent(OWNER, "پاسخ دستی"))
        cards = await self.db.deck_cards(deck_id)
        self.assertEqual(
            [(card["front"], card["back"]) for card in cards],
            [("پرسش دستی", "پاسخ دستی")],
        )

        # Leaving the deck disarms the prompt: the next text is a re-trigger.
        await self._press(OWNER, "fc:hub")
        self.assertNotIn(OWNER, self.bot._pending_flashcard)
        stray = FakeMessageEvent(OWNER, "پیام پرت")
        await self.bot._handle_message(stray)
        self.assertEqual(len(await self.db.deck_cards(deck_id)), 1)
        self.assertIn("برای شروع", stray.replies[-1])

    async def test_empty_answer_is_rejected_and_the_prompt_stays_armed(self):
        await self._press(OWNER, "fc:man")
        deck = await self.db.manual_deck(OWNER)
        assert deck is not None
        await self.bot._handle_message(FakeMessageEvent(OWNER, "   "))
        event = FakeMessageEvent(OWNER, "   ")
        await self.bot._handle_message(event)
        self.assertIn("متن خالی پذیرفته نمی‌شود", event.replies[-1])
        self.assertIn(OWNER, self.bot._pending_flashcard)
        self.assertEqual(await self.db.deck_cards(int(deck["deck_id"])), [])

    async def test_flip_study_persists_the_cursor_and_restarts(self):
        deck_id = await self._ai_deck(cards=2)
        first = await self._press(OWNER, f"fc:study:{deck_id}")
        self.assertIn("کارت ۱ از ۲", first.edits[-1][0])
        cards = await self.db.deck_cards(deck_id)

        flipped = await self._press(
            OWNER, f"fc:flip:{deck_id}:{int(cards[0]['card_id'])}"
        )
        self.assertIn("پاسخ:", flipped.edits[-1][0])
        self.assertIn("پاسخ 0", flipped.edits[-1][0])

        await self._press(OWNER, f"fc:next:{deck_id}")
        deck = await self.db.deck_get(OWNER, deck_id)
        assert deck is not None
        self.assertEqual(int(deck["study_cursor"]), 1)
        again = await self._press(OWNER, f"fc:study:{deck_id}")
        self.assertIn("کارت ۲ از ۲", again.edits[-1][0])

        done = await self._press(OWNER, f"fc:done:{deck_id}")
        self.assertIn("مرور تمام شد — ۲ کارت مرور شد.", done.edits[-1][0])
        restarted = await self._press(OWNER, f"fc:restart:{deck_id}")
        self.assertIn("کارت ۱ از ۲", restarted.edits[-1][0])

    async def test_a_foreign_card_id_is_refused(self):
        deck_id = await self._ai_deck(cards=2)
        foreign = await self._press(OTHER, f"fc:flip:{deck_id}:1")
        self.assertEqual(foreign.answers[-1][0], "این مجموعه در دسترس نیست.")

    async def test_delete_requires_confirmation_and_removes_the_cards(self):
        deck_id = await self._ai_deck(cards=5)
        confirm = await self._press(OWNER, f"fc:del:{deck_id}")
        self.assertIn("آیا مطمئنید؟", confirm.edits[-1][0])
        self.assertEqual(await self.db.decks_count(OWNER), 1)

        await self._press(OWNER, f"fc:delok:{deck_id}")
        self.assertEqual(await self.db.decks_count(OWNER), 0)
        rows = await self.db.read_db.execute_fetchall(
            "SELECT COUNT(*) FROM flashcards WHERE deck_id = ?", (deck_id,)
        )
        self.assertEqual(int(rows[0][0]), 0)

    async def test_word_export_delivers_an_rtl_document(self):
        deck_id = await self._ai_deck(cards=5)
        await self._press(OWNER, f"fc:exp:{deck_id}")

        delivery = next(
            entry
            for entry in self.client.deliveries
            if str(entry["path"]).endswith(".docx")
        )
        self.assertIn("🃏 فلش‌کارت‌ها —", delivery["caption"])
        payload = next(
            body for name, body in self.record.file_payloads if name.endswith(".docx")
        )
        text = docx_text(payload)
        self.assertIn("پرسش 1:", text)
        self.assertIn("پاسخ: پاسخ 0", text)

    async def test_hub_renders_the_empty_state_and_lists_decks(self):
        empty = await self._press(OWNER, "library:fc")
        self.assertIn("هنوز مجموعه‌ای نساخته‌اید.", empty.edits[-1][0])

        deck_id = await self._ai_deck(cards=5)
        listed = await self._press(OWNER, "fc:hub")
        self.assertIn(f"fc:deck:{deck_id}".encode("ascii"), menu_payloads(listed.edits[-1][1]))
        self.assertIn("۵ کارت", listed.edits[-1][0])


# ===========================================================================
# Delivery capture (content_text + caption CTA)
# ===========================================================================
class ContentCaptureTests(LearningTestCase):
    async def _deliver(self, *, document_text: str = "متن کامل گفتار درس") -> Job:
        event = FakeJobEvent(chat_id=OWNER)
        submission_id = await self.db.create_submission(OWNER, "audio", "lecture.ogg")
        job = Job(
            submission_id=submission_id,
            user_id=OWNER,
            kind="audio",
            filename="lecture.ogg",
            message=event.message,
            is_admin=False,
        )
        raw_path = self.root / "raw.txt"
        raw_path.write_text(document_text, encoding="utf-8")
        await self.bot._deliver_raw(job, document_text, raw_path, "GMS-7")
        notes = StructuredNotes(title="جزوهٔ آزمایشی", summary="خلاصه")
        meta = DocumentMeta(
            reference="GMS-7", source_name="lecture.ogg", created_at=datetime.now()
        )
        await self.bot._deliver_booklet(
            job,
            self.root,
            notes,
            document=_RenderedDocument(document_text),
            reference="GMS-7",
            meta=meta,
            progress=_Progress(),
        )
        return job

    async def test_both_artifacts_archive_their_text(self):
        await self._deliver(document_text="متن کامل گفتار درس")

        items = await self.db.library_list(OWNER, limit=10)
        by_kind = {item["kind"]: item for item in items}
        self.assertIn("transcript", by_kind)
        self.assertIn("booklet", by_kind)
        self.assertGreater(int(by_kind["transcript"]["message_id"]), 0)
        for kind in ("transcript", "booklet"):
            item = await self.db.library_item(
                OWNER, int(by_kind[kind]["item_id"])
            )
            assert item is not None
            self.assertEqual(item["content_text"], "متن کامل گفتار درس")

    async def test_booklet_caption_carries_the_library_cue_and_learning_cta(self):
        await self._deliver()
        booklet = next(
            entry
            for entry in self.client.deliveries
            if str(entry["path"]).endswith(".docx")
        )
        self.assertIn("📚 در کتابخانهٔ شما ذخیره شد", booklet["caption"])
        self.assertIn(
            "🧪 از همین جزوه آزمون بده یا 🃏 فلش‌کارت بساز: «کتابخانه من»",
            booklet["caption"],
        )
        self.assertLessEqual(len(booklet["caption"]), 1024)

    async def test_caption_keeps_the_reference_when_it_is_very_long(self):
        caption = self.bot._booklet_caption("R" * 1200)
        self.assertLessEqual(len(caption), 1024)
        self.assertTrue(caption.startswith("📖 جزوهٔ Word — RRR"))

    async def test_library_set_content_and_the_picker_query(self):
        item_id = await self._booklet(content=None)
        self.assertEqual(await self.db.library_content_count(OWNER), 0)
        self.assertTrue(await self.db.library_set_content(item_id, "متن تازه"))
        self.assertEqual(await self.db.library_get_content(OWNER, item_id), "متن تازه")
        self.assertEqual(await self.db.library_content_count(OWNER), 1)
        rows = await self.db.library_items_with_content(OWNER)
        self.assertEqual([row["item_id"] for row in rows], [item_id])


# ===========================================================================
# Pure content validation
# ===========================================================================
class ContentValidationTests(unittest.TestCase):
    def test_questions_are_shuffled_with_the_index_remapped(self):
        payload = {
            "questions": [
                {
                    "question": "کدام گزینه درست است؟",
                    "options": ["الف", "ب", "ج", "د"],
                    "correct": 2,
                    "explain": "زیرا ج درست است.",
                }
            ]
        }
        import random

        for seed in range(20):
            normalized = normalize_questions(
                payload, 1, rng=random.Random(seed)
            )
            self.assertEqual(len(normalized), 1)
            question = normalized[0]
            self.assertEqual(len(question["options"]), 4)
            self.assertEqual(question["options"][question["correct"]], "ج")

    def test_malformed_questions_are_dropped(self):
        payload = {
            "questions": [
                {"question": "", "options": ["a", "b", "c", "d"], "correct": 0},
                {"question": "q", "options": ["a", "b", "c"], "correct": 0},
                {"question": "q", "options": ["a", "a", "c", "d"], "correct": 0},
                {"question": "q", "options": ["a", "b", "c", "d"], "correct": 9},
                {"question": "q", "options": ["a", "b", "c", "d"], "correct": True},
                {"question": "خوب", "options": ["a", "b", "c", "d"], "correct": 1},
            ]
        }
        normalized = normalize_questions(payload, 5)
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["question"], "خوب")

    def test_question_count_is_capped_at_the_requested_number(self):
        payload = {
            "questions": [
                {
                    "question": f"q{index}",
                    "options": ["a", "b", "c", "d"],
                    "correct": 0,
                }
                for index in range(10)
            ]
        }
        self.assertEqual(len(normalize_questions(payload, 3)), 3)
        self.assertEqual(normalize_questions("not a dict", 3), [])

    def test_cards_are_validated_clamped_and_deduplicated(self):
        payload = {
            "cards": [
                {"front": "پرسش", "back": "پاسخ"},
                {"front": "پرسش", "back": "پاسخ دیگر"},
                {"front": "", "back": "بی‌پرسش"},
                {"front": "ف" * 500, "back": "پ" * 900},
                "not a dict",
            ]
        }
        cards = normalize_cards(payload)
        self.assertEqual(len(cards), 2)
        self.assertEqual(len(cards[1]["front"]), MAX_CARD_FRONT_CHARS)
        self.assertEqual(len(cards[1]["back"]), MAX_CARD_BACK_CHARS)

    def test_content_is_bounded_on_a_paragraph_boundary(self):
        paragraph = "جملهٔ کامل درس. "
        text = "\n\n".join([paragraph * 20] * 400)
        bounded = bound_content(text, limit=5_000)
        self.assertLessEqual(len(bounded), 5_000)
        self.assertFalse(bounded.endswith("جمل"))

    def test_deck_document_body_numbers_every_card(self):
        body = deck_document_body(
            [{"front": "پرسش یک", "back": "پاسخ یک"}, {"front": "دو", "back": "دو"}]
        )
        self.assertIn("پرسش 1: پرسش یک", body)
        self.assertIn("پاسخ: پاسخ یک", body)
        self.assertIn("پرسش 2: دو", body)


# ===========================================================================
# Guards: hourly limit, lazy expiry, deck ceiling
# ===========================================================================
class GuardTests(LearningTestCase):
    async def test_hourly_limit_refuses_another_quiz_start(self):
        await self._credit(OWNER, 10_000)
        item_id = await self._booklet()
        with mock.patch(
            "gamas_bot.bot.generate_quiz",
            new=mock.AsyncMock(side_effect=lambda *a, **k: _questions(5)),
        ):
            for _ in range(self.settings.quiz_max_sessions_per_hour):
                await self._press(OWNER, f"quiz:go:{item_id}:5")
                session = await self.db.quiz_active_session(OWNER)
                assert session is not None
                await self._press(OWNER, f"quiz:quit:{int(session['session_id'])}")

            blocked = await self._press(OWNER, f"quiz:go:{item_id}:5")

        self.assertTrue(blocked.answers[-1][1])
        self.assertIn("تعداد آزمون‌های شما در این ساعت زیاد است", blocked.answers[-1][0])
        self.assertIsNone(await self.db.quiz_active_session(OWNER))

    async def test_a_stale_quiz_is_expired_lazily_instead_of_blocking(self):
        await self._credit(OWNER, 600)
        _, session_id = await self._open_quiz(5)

        connection = sqlite3.connect(self.settings.db_path)
        try:
            connection.execute(
                "UPDATE quiz_sessions SET updated_at = ? WHERE session_id = ?",
                ("2000-01-01T00:00:00+00:00", session_id),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(
            await self.db.quiz_expire_stale(OWNER, self.settings.quiz_max_active_hours),
            1,
        )
        self.assertIsNone(await self.db.quiz_active_session(OWNER))

    async def test_an_admin_command_disarms_the_manual_card_prompt(self):
        """An early-returning admin command must not leave a card prompt armed."""
        await self._press(ADMIN, "fc:man")
        self.assertIn(ADMIN, self.bot._pending_flashcard)

        await self.bot._handle_message(FakeMessageEvent(ADMIN, "/grant 555 5"))
        self.assertNotIn(ADMIN, self.bot._pending_flashcard)

        # The next ordinary message is a re-trigger, not a card question.
        stray = FakeMessageEvent(ADMIN, "یک پیام عادی")
        await self.bot._handle_message(stray)
        deck = await self.db.manual_deck(ADMIN)
        assert deck is not None
        self.assertEqual(await self.db.deck_cards(int(deck["deck_id"])), [])
        self.assertIn("برای شروع", stray.replies[-1])

    async def test_the_deck_ceiling_refuses_a_new_manual_deck(self):
        from dataclasses import replace

        self.bot.settings = replace(self.settings, flashcard_max_decks_per_user=1)
        await self.db.deck_create(OWNER, "تنها مجموعه", source="ai")

        event = await self._press(OWNER, "fc:man")
        self.assertTrue(event.answers[-1][1])
        self.assertIn("به سقف رسیده است", event.answers[-1][0])
        self.assertIsNone(await self.db.manual_deck(OWNER))
