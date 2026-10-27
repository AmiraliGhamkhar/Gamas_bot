"""Free first try (R3): one free deck and one free quiz per user.

Two layers are covered:

* **Database** — the claim is a single atomic UPDATE, so concurrent generations
  can never share one gift, and the refund/reset paths are exact.
* **Bot** — the gift badge, the free generation itself, the paywall on the
  second attempt, the provider-failure rollback and the curation preview.
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


def cards_payload(count: int) -> dict:
    return {
        "cards": [
            {"front": f"پرسش {index}", "back": f"پاسخ {index}", "source": "صفحهٔ ۱"}
            for index in range(count)
        ]
    }


def quiz_payload(count: int) -> dict:
    return {
        "questions": [
            {
                "question": f"اگر شرط {index} برقرار باشد کدام گزینه درست است؟",
                "options": [
                    f"گزینه درست {index}",
                    f"گزینه انحرافی الف {index}",
                    f"گزینه انحرافی ب {index}",
                    f"گزینه انحرافی ج {index}",
                ],
                "correct": 0,
                "explain": "توضیح یک‌خطی",
                "source": "صفحهٔ ۱",
            }
            for index in range(count)
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

    async def test_a_new_user_has_both_gifts(self):
        self.assertEqual(await self.db.free_use_state(OWNER), {"deck": True, "quiz": True})

    async def test_the_two_gifts_are_independent(self):
        self.assertTrue(await self.db.consume_free_use(OWNER, "deck"))
        self.assertFalse(await self.db.consume_free_use(OWNER, "deck"))
        self.assertTrue(await self.db.consume_free_use(OWNER, "quiz"))
        self.assertEqual(
            await self.db.free_use_state(OWNER), {"deck": False, "quiz": False}
        )

    async def test_concurrent_claims_have_exactly_one_winner(self):
        # The race the atomic UPDATE exists for: two generations claiming at the
        # same instant must never both get the free try.
        results = await asyncio.gather(
            self.db.consume_free_use(OWNER, "deck"),
            self.db.consume_free_use(OWNER, "deck"),
        )
        self.assertEqual(sorted(results), [False, True])

    async def test_two_users_do_not_share_a_gift(self):
        await self.db.ensure_user(OTHER)
        self.assertTrue(await self.db.consume_free_use(OWNER, "quiz"))
        self.assertTrue(await self.db.consume_free_use(OTHER, "quiz"))

    async def test_refund_only_restores_a_claimed_gift(self):
        self.assertFalse(await self.db.refund_free_use(OWNER, "deck"))
        await self.db.consume_free_use(OWNER, "deck")
        self.assertTrue(await self.db.refund_free_use(OWNER, "deck"))
        self.assertTrue((await self.db.free_use_state(OWNER))["deck"])

    async def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.db.consume_free_use(OWNER, "notes")

    async def test_admin_reset_restores_both_and_audits(self):
        await self.db.consume_free_use(OWNER, "deck")
        await self.db.consume_free_use(OWNER, "quiz")
        await self.db.reset_free_uses(OWNER, admin_id=1)
        self.assertEqual(await self.db.free_use_state(OWNER), {"deck": True, "quiz": True})
        audit = await self.db.recent_audit(limit=1)
        self.assertEqual(audit[0]["action"], "free_use_reset")
        self.assertEqual(audit[0]["subject_user"], OWNER)


class FreeFirstTryBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "freebot.db", temp_dir=root / "tmp", flashcard_ai_deck_size=6
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
        flash = self.press(f"fc:ai:{self.item_id}".encode())
        await self.bot._handle_callback(flash)
        self.assertIn(FREE_TRY_BADGE, self.last_text(flash))

        quiz = self.press(f"quiz:new:{self.item_id}".encode())
        await self.bot._handle_callback(quiz)
        self.assertIn(FREE_TRY_BADGE, self.last_text(quiz))

    async def test_badge_disappears_once_the_gift_is_used(self):
        await self.db.consume_free_use(OWNER, "deck")
        flash = self.press(f"fc:ai:{self.item_id}".encode())
        await self.bot._handle_callback(flash)
        self.assertNotIn(FREE_TRY_BADGE, self.last_text(flash))

    async def test_first_deck_is_free_and_creates_no_debit(self):
        # No credit was granted at all: the gift alone must pay for this deck.
        event = self.press(f"fc:go:{self.item_id}".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=cards_payload(6)),
        ) as generator:
            await self.bot._handle_callback(event)
            generator.assert_awaited_once()
        await self.bot._handle_callback(self.press(b"fc:cur:save"))
        debits = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) FROM ledger WHERE kind='debit'"
        )
        self.assertEqual(debits[0][0], 0)
        self.assertEqual(len(await self.db.decks_list(OWNER)), 1)
        self.assertFalse((await self.db.free_use_state(OWNER))["deck"])

    async def test_second_deck_hits_the_paywall_without_calling_the_model(self):
        await self.db.consume_free_use(OWNER, "deck")
        event = self.press(f"fc:go:{self.item_id}".encode())
        with mock.patch("gamas_bot.bot.complete_json") as generator:
            await self.bot._handle_callback(event)
            generator.assert_not_called()
        self.assertIn(LEARNING_NO_CREDIT, self.client.texts[-1]["text"])
        self.assertEqual(await self.db.deck_count(OWNER), 0)

    async def test_provider_failure_returns_the_free_try(self):
        event = self.press(f"fc:go:{self.item_id}".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=LLMError("boom"))
        ):
            await self.bot._handle_callback(event)
        self.assertTrue(
            (await self.db.free_use_state(OWNER))["deck"],
            "a provider fault must never consume the gift",
        )
        self.assertIn(FREE_TRY_RESTORED, self.client.texts[-1]["text"])
        self.assertEqual(await self.db.deck_count(OWNER), 0)

    async def test_a_valid_generation_consumes_the_gift_only_on_save(self):
        event = self.press(f"fc:go:{self.item_id}".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=cards_payload(6)),
        ):
            await self.bot._handle_callback(event)
        # Claimed for the attempt, but the artwork is not durable yet.
        self.assertEqual(await self.db.deck_count(OWNER), 0)
        save = self.press(b"fc:cur:save")
        await self.bot._handle_callback(save)
        self.assertEqual(await self.db.deck_count(OWNER), 1)

    async def test_cancel_restores_the_free_try(self):
        event = self.press(f"fc:go:{self.item_id}".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=cards_payload(6)),
        ):
            await self.bot._handle_callback(event)
        await self.bot._handle_callback(self.press(b"fc:cur:cancel"))
        self.assertTrue((await self.db.free_use_state(OWNER))["deck"])
        self.assertEqual(await self.db.deck_count(OWNER), 0)

    async def test_first_quiz_is_free_and_second_is_paid(self):
        await self.db.add_credit(OWNER, 600, note="paid", validity_days=30)
        first = self.press(f"quiz:go:{self.item_id}:5".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(5)),
        ):
            await self.bot._handle_callback(first)
        await self.bot._handle_callback(self.press(b"quiz:cur:save"))
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
            return cards_payload(6)

        with mock.patch(
            "gamas_bot.bot.complete_json", new=mock.AsyncMock(side_effect=slow)
        ):
            await asyncio.gather(
                self.bot._handle_callback(self.press(f"fc:go:{self.item_id}".encode())),
                self.bot._handle_callback(self.press(f"fc:go:{self.item_id}".encode())),
            )
        # The second tap was rejected as busy; the gift was claimed exactly once.
        await self.bot._handle_callback(self.press(b"fc:cur:save"))
        self.assertEqual(await self.db.deck_count(OWNER), 1)
        state = await self.db.free_use_state(OWNER)
        self.assertFalse(state["deck"])
        self.assertTrue(state["quiz"], "the quiz gift must be untouched")

    async def test_curation_delete_drops_an_item_before_save(self):
        event = self.press(f"fc:go:{self.item_id}".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=cards_payload(6)),
        ):
            await self.bot._handle_callback(event)
        preview = self.client.texts[-1]["text"]
        self.assertIn("پیش‌نمایش فلش‌کارت", preview)
        self.assertIn("← پاسخ 0", preview)
        # The per-item delete button (R5.1 curation) removes card #1 and
        # re-renders the preview in place.
        delete = self.press(b"fc:cur:del:0")
        await self.bot._handle_callback(delete)
        re_rendered = delete.edits[-1]["text"]
        self.assertNotIn("← پاسخ 0", re_rendered)
        self.assertIn("۵ مورد", re_rendered)
        await self.bot._handle_callback(self.press(b"fc:cur:save"))
        decks = await self.db.decks_list(OWNER)
        self.assertEqual(decks[0]["card_count"], 5)
        cards = await self.db.deck_cards(decks[0]["deck_id"])
        self.assertEqual(cards[0]["front"], "پرسش 1")

    async def test_size_preset_changes_count_and_price(self):
        await self.db.add_credit(OWNER, 5000, note="paid", validity_days=30)
        await self.db.consume_free_use(OWNER, "deck")
        # «سریع» asks for 10 cards; the price scales with the requested size
        # (base 300s for 6 cards → 500s for 10).
        event = self.press(f"fc:go:{self.item_id}:quick".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=cards_payload(10)),
        ) as generator:
            await self.bot._handle_callback(event)
            asked = generator.await_args.kwargs["user"]
            self.assertTrue(asked)
        await self.bot._handle_callback(self.press(b"fc:cur:save"))
        debit = await self.db.db.execute_fetchall(
            "SELECT seconds FROM ledger WHERE kind='debit'"
        )
        self.assertEqual(int(debit[0][0]), 500)
        self.assertEqual((await self.db.decks_list(OWNER))[0]["card_count"], 10)

    async def test_quiz_size_preset_bills_per_question(self):
        await self.db.add_credit(OWNER, 5000, note="paid", validity_days=30)
        await self.db.consume_free_use(OWNER, "quiz")
        event = self.press(f"quiz:go:{self.item_id}:20".encode())
        with mock.patch(
            "gamas_bot.bot.complete_json",
            new=mock.AsyncMock(return_value=quiz_payload(20)),
        ):
            await self.bot._handle_callback(event)
        await self.bot._handle_callback(self.press(b"quiz:cur:save"))
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
        await self.db.consume_free_use(OWNER, "deck")
        confirm = self.press(f"adm:hub:usr:fr:{OWNER}".encode(), user_id=admin_id)
        await self.bot._handle_callback(confirm)
        execute = self.press(f"adm:hub:usr:frx:{OWNER}".encode(), user_id=admin_id)
        await self.bot._handle_callback(execute)
        self.assertTrue((await self.db.free_use_state(OWNER))["deck"])


if __name__ == "__main__":
    unittest.main()
