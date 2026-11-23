"""Regression coverage for the booklet completion and wallet UX."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.extensions import html
from telethon.tl.types import MessageEntityBold, MessageEntityStrike

from gamas_bot.artifacts import KIND_BOOKLET, KIND_DOCX, KIND_TRANSCRIPT, delivery_markup
from gamas_bot.billing import round_display_credits, seconds_to_credits
from gamas_bot.bot_wallet import WalletWorkflowMixin


def payloads(rows):
    return [button.type.data for row in rows for button in row if hasattr(button.type, "data")]


class RoundingTests(unittest.TestCase):
    def test_conservative_display_rounding_does_not_modify_conversion(self):
        for original, displayed in [(0, 0), (1, 1), (19, 10), (100_000, 100_000),
                                    (333_334, 300_000), (666_667, 600_000),
                                    (866_667, 800_000)]:
            self.assertEqual(round_display_credits(original), displayed)
        self.assertEqual(seconds_to_credits(10 * 3600), 333_334)

    def test_the_delivery_keyboard_is_only_artifacts_and_the_quiz(self):
        """No referral or menu row: a finished file is the whole screen."""
        for kinds, has_booklet in [
            ({KIND_BOOKLET, KIND_TRANSCRIPT}, True),
            ({KIND_DOCX, KIND_TRANSCRIPT}, False),
            ({KIND_TRANSCRIPT}, False),
        ]:
            rows = delivery_markup(reference="GMS-1", speakers=0,
                                   has_booklet=has_booklet, kinds=kinds)
            data = payloads(rows)
            self.assertNotIn(b"ref:result", data)
            self.assertNotIn(b"menu:main", data)
            self.assertTrue(
                all(value.startswith(b"art:") or value.startswith(b"quiz:") for value in data),
                data,
            )
            if has_booklet:
                self.assertTrue(any(value.startswith(b"art:") for value in data))


class WalletPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_discount_html_rounding_exact_details_and_no_financial_mutation(self):
        wallet = WalletWorkflowMixin()
        plans = [
            {"code": "ten", "hours": 10, "price": 109_000,
             "original_price": 220_000, "validity_days": 30},
            {"code": "plain", "hours": 3, "price": 79_000,
             "original_price": 79_000, "validity_days": 30},
        ]
        wallet.db = SimpleNamespace(
            open_payment_for_user=AsyncMock(return_value=None),
            plans=AsyncMock(return_value=plans),
            count_user_payments=AsyncMock(return_value=0),
            payment_history_paginated=AsyncMock(return_value=[]),
        )
        wallet.settings = SimpleNamespace(payment_card_number="123")
        wallet._balance_text = AsyncMock(return_value="۳۳۳٬۳۳۴ اعتبار")
        wallet._display_balance_text = AsyncMock(return_value="۳۰۰٬۰۰۰ اعتبار")
        wallet._edit_or_reply = AsyncMock()
        event = SimpleNamespace(sender_id=42)
        await wallet._show_wallet(event)
        args, kwargs = wallet._edit_or_reply.await_args
        raw, buttons = args[1], args[2]
        plain, entities = html.parse(raw)
        self.assertEqual(kwargs["parse_mode"], "html")
        self.assertEqual(sum(isinstance(e, MessageEntityStrike) for e in entities), 1)
        self.assertEqual(sum(isinstance(e, MessageEntityBold) for e in entities), 2)
        self.assertIn("۳۰۰٬۰۰۰ اعتبار (۱۰ ساعت)", plain)
        self.assertIn("۱۰۰٬۰۰۰ اعتبار (۳ ساعت)", plain)
        self.assertIn("۲۲۰٬۰۰۰ تومان", plain)
        self.assertIn("۱۰۹٬۰۰۰ تومان", plain)
        self.assertNotIn("حدود", plain)
        self.assertIn(b"wallet:exact", payloads(buttons))
        self.assertIn("۳۰۰٬۰۰۰", buttons[0][0].text)
        self.assertEqual(plans[0]["hours"], 10)
        self.assertEqual(plans[0]["price"], 109_000)
        await wallet._show_wallet_exact(event, 42)
        exact = wallet._edit_or_reply.await_args.args[1]
        self.assertIn("۳۳۳٬۳۳۴", exact)
        self.assertIn("۱۰ ساعت", exact)


if __name__ == "__main__":
    unittest.main()
