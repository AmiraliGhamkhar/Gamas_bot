"""The user's own screens have to survive a growing deployment.

Both screens built one message out of *every* row: the purchase screen rendered
each enabled paid plan as a line plus a button, and the account screen listed
every active entitlement. Telegram caps a message at 4 096 characters and a
keyboard at 100 buttons, so past that point the screen stops sending — the user
cannot buy anything or see their own credit, with no explanation. The admin
equivalents were fixed earlier; these are the user-facing halves.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support import make_settings

from gamas_bot.billing import format_duration
from gamas_bot.bot import StudyBot
from gamas_bot.database import Database
from gamas_bot.progress import to_persian_digits

#: A plan the seeded catalog always ships, so a payment request can be created.
SEED_PLAN_CODE = "paid_25h_30d"


def _button_data(button) -> bytes:
    """Callback payload of a Telethon inline button (any 1.x layout)."""
    data = getattr(button, "data", None)
    if data:
        return bytes(data)
    inner = getattr(button, "type", None)
    data = getattr(inner, "data", None)
    return bytes(data) if data else b""


def _payloads(buttons: list) -> list[str]:
    return [_button_data(button).decode("utf-8") for row in buttons for button in row]


def _stamp(day_offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=day_offset)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class UserAccountScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "user-screens.sqlite3")
        await self.db.open()
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.db = self.db
        self.user = await self.db.upsert_user(501, "buyer")
        self.user_id = int(self.user["id"])
        self.plan = await self.db.create_plan(
            code="starter02h",
            name="طرح پایه",
            hours=2,
            price_toman=90_000,
            validity_days=30,
            admin_id=7,
        )
        self.plan_id = int(self.plan["id"])

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp.cleanup()

    async def _make_plans(self, count: int) -> list[str]:
        codes: list[str] = []
        for index in range(count):
            plan = await self.db.create_plan(
                code=f"deep{index:02d}",
                name=f"طرح {index + 1}",
                hours=2 + index,
                price_toman=10_000 * (1 + index),
                validity_days=30,
                admin_id=7,
            )
            codes.append(str(plan["code"]))
        return codes

    async def _grant(self, *, rows: int, remaining: int) -> None:
        async with self.db._transaction(immediate=True) as db:
            for _ in range(rows):
                await db.execute(
                    "INSERT INTO entitlements (user_id, plan_id, granted_seconds, "
                    "remaining_seconds, starts_at, expires_at, status, source, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, datetime(?), datetime(?, '+30 day'), "
                    "'active', 'admin_credit', datetime(?), datetime(?))",
                    (
                        self.user_id,
                        self.plan_id,
                        remaining + 1_000,
                        remaining,
                        _stamp(0),
                        _stamp(0),
                        _stamp(0),
                        _stamp(0),
                    ),
                )

    # ---------------------------------------------------------- purchase screen
    async def _paid_codes(self) -> list[str]:
        # setUp already created one plan, so every count here comes from the
        # table rather than from the number of rows a test added.
        return sorted(
            str(plan["code"]) for plan in await self.db.list_plans(paid_only=True)
        )

    async def test_plan_screen_stays_within_telegram_caps_with_a_large_catalog(self) -> None:
        await self._make_plans(45)
        codes = await self._paid_codes()
        pages = -(-len(codes) // StudyBot.USER_PLAN_PAGE)
        text, buttons = await self.bot._billing_plans()
        self.assertLess(len(text.encode("utf-8")), 4096)
        payloads = _payloads(buttons)
        self.assertLessEqual(len(payloads), 100)
        self.assertTrue(payloads)
        for payload in payloads:
            self.assertLessEqual(len(payload.encode("utf-8")), 64, payload)
        self.assertIn(
            f"({self._fa(len(codes))} طرح فعال — صفحهٔ ۱ از {self._fa(pages)})", text
        )
        self.assertEqual(
            len([payload for payload in payloads if payload.startswith("billing:buy:")]),
            StudyBot.USER_PLAN_PAGE,
        )

    async def test_pages_together_cover_every_plan_exactly_once(self) -> None:
        await self._make_plans(45)
        codes = await self._paid_codes()
        pages = -(-len(codes) // StudyBot.USER_PLAN_PAGE)
        seen: list[str] = []
        for page in range(pages):
            text, buttons = await self.bot._billing_plans(page)
            bought = [
                payload.split(":", 2)[2]
                for payload in _payloads(buttons)
                if payload.startswith("billing:buy:")
            ]
            self.assertEqual(len(bought), len(set(bought)), text)
            seen.extend(bought)
            if page + 1 < pages:
                self.assertIn(f"billing:plans:{page + 1}", _payloads(buttons))
        self.assertEqual(sorted(seen), sorted(codes))

    async def test_small_catalog_shows_every_plan_with_no_paging_noise(self) -> None:
        # The seeded catalog is smaller than one page, which is the normal case:
        # paging must add nothing a user has to read.
        codes = await self._paid_codes()
        self.assertLessEqual(len(codes), StudyBot.USER_PLAN_PAGE)
        text, buttons = await self.bot._billing_plans()
        self.assertNotIn("صفحهٔ", text)
        self.assertNotIn("billing:plans:1", _payloads(buttons))
        bought = [p.split(":", 2)[2] for p in _payloads(buttons) if p.startswith("billing:buy:")]
        self.assertEqual(sorted(bought), sorted(codes))

    async def test_stale_page_lands_on_the_last_real_page(self) -> None:
        await self._make_plans(45)
        codes = await self._paid_codes()
        pages = -(-len(codes) // StudyBot.USER_PLAN_PAGE)
        text, buttons = await self.bot._billing_plans(99)
        self.assertIn(f"صفحهٔ {self._fa(pages)} از {self._fa(pages)}", text)
        self.assertIn(f"billing:plans:{pages - 2}", _payloads(buttons))

    @staticmethod
    def _fa(value: int) -> str:
        return to_persian_digits(value)

    # ----------------------------------------------------------- account screen
    async def test_many_active_entitlements_are_summarised_not_overflowed(self) -> None:
        await self._grant(rows=60, remaining=60)
        active = len((await self.db.user_balance(self.user_id))["entitlements"])
        text = await self.bot._billing_balance_text(self.user_id)
        self.assertLess(len(text.encode("utf-8")), 4096)
        hidden = active - StudyBot.ACCOUNT_ENTITLEMENT_ROWS
        self.assertGreater(hidden, 0)
        self.assertIn("اعتبارهای فعال:\n", text)
        self.assertIn(
            f"… و {to_persian_digits(hidden)} اعتبار فعال دیگر", text
        )
        balance = await self.db.user_balance(self.user_id)
        self.assertIn(format_duration(balance["available_seconds"]), text)
        self.assertEqual(text.count("طرح پایه"), StudyBot.ACCOUNT_ENTITLEMENT_ROWS)

    async def test_short_entitlement_history_lists_every_row(self) -> None:
        await self._grant(rows=2, remaining=45)
        text = await self.bot._billing_balance_text(self.user_id)
        self.assertNotIn("اعتبار فعال دیگر", text)
        self.assertEqual(text.count("طرح پایه"), 2)


class PlanPagingDispatchTests(unittest.IsolatedAsyncioTestCase):
    """A pressed page button reaches the page it names; junk does not break it."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "plans.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({7}),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()
        self.bot._pending_admin_actions = {}
        self.bot._pending_credential_setup = {}
        self.bot.client = SimpleNamespace(send_message=AsyncMock(), send_file=AsyncMock())
        self.user = await self.bot.db.upsert_user(501, "buyer")
        for index in range(20):
            await self.bot.db.create_plan(
                code=f"deep{index:02d}",
                name=f"طرح {index + 1}",
                hours=2 + index,
                price_toman=10_000 * (1 + index),
                validity_days=30,
                admin_id=7,
            )

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp.cleanup()

    def _event(self, data: str) -> SimpleNamespace:
        return SimpleNamespace(
            data=data.encode("utf-8"),
            is_private=True,
            get_sender=AsyncMock(
                return_value=SimpleNamespace(id=501, username="buyer", bot=False)
            ),
            answer=AsyncMock(),
            edit=AsyncMock(),
            respond=AsyncMock(),
        )

    @staticmethod
    def _rendered(event: SimpleNamespace) -> tuple[str, list]:
        for mock in (event.edit, event.respond):
            if mock.await_args is None:
                continue
            call = mock.await_args
            text = call.args[0] if call.args else call.kwargs.get("text", "")
            buttons = call.kwargs.get("buttons") or (
                call.args[1] if len(call.args) > 1 else None
            )
            return str(text), list(buttons or [])
        raise AssertionError("the plan screen was never rendered")

    async def test_more_button_walks_to_the_next_page(self) -> None:
        first = self._event("billing:plans")
        await self.bot._handle_callback(first)
        text, buttons = self._rendered(first)
        self.assertIn("صفحهٔ ۱ از", text)
        self.assertIn("billing:plans:1", _payloads(buttons))

        second = self._event("billing:plans:1")
        await self.bot._handle_callback(second)
        text, buttons = self._rendered(second)
        self.assertIn("صفحهٔ ۲ از", text)
        payloads = _payloads(buttons)
        self.assertIn("billing:plans:0", payloads)
        self.assertIn("billing:plans:2", payloads)

    async def test_a_non_numeric_page_token_renders_the_first_page(self) -> None:
        for data in ("billing:plans:all", "billing:plans:-1", "billing:plans:1:2"):
            with self.subTest(data=data):
                event = self._event(data)
                await self.bot._handle_callback(event)
                text, _buttons = self._rendered(event)
                self.assertIn("صفحهٔ ۱ از", text)
                self.assertNotIn("صفحهٔ ۲", text)

    async def test_every_purchase_button_on_a_page_targets_a_real_plan(self) -> None:
        event = self._event("billing:plans:2")
        await self.bot._handle_callback(event)
        _text, buttons = self._rendered(event)
        payloads = _payloads(buttons)
        codes = {str(row["code"]) for row in await self.bot.db.list_plans(paid_only=True)}
        page = sorted(p.split(":", 2)[2] for p in payloads if p.startswith("billing:buy:"))
        self.assertTrue(page)
        self.assertTrue(set(page) <= codes, page)
        for payload in payloads:
            self.assertLessEqual(len(payload.encode("utf-8")), 64, payload)
        # a page button must not look like a buy button and vice versa
        self.assertNotIn("billing:plans:", " ".join(p for p in payloads if p.startswith("billing:buy:")))


class PaymentHistoryHeadingTests(unittest.IsolatedAsyncioTestCase):
    """`/history` prints the newest ten rows and must say how many really exist."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "history.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({7}),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()
        self.bot._pending_admin_actions = {}
        self.bot._pending_credential_setup = {}
        self.user = await self.bot.db.upsert_user(601, "buyer")
        # `create_payment_request` reuses an open request, so every row needs a
        # real state transition first: paid-then-cancelled pairs are the shipped way.
        self.rows = 0

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp.cleanup()

    async def _make_payments(self, count: int) -> None:
        for _ in range(count):
            request = await self.bot.db.create_payment_request(
                int(self.user["id"]), SEED_PLAN_CODE
            )
            await self.bot.db.cancel_payment_intent(int(request["id"]), int(self.user["id"]))
            self.rows += 1

    def _event(self, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            raw_text=text,
            is_private=True,
            message=SimpleNamespace(media=None),
            get_sender=AsyncMock(
                return_value=SimpleNamespace(id=601, username="buyer", bot=False)
            ),
            reply=AsyncMock(),
        )

    async def _history_text(self) -> str:
        event = self._event("/history")
        await self.bot._handle_message(event)
        event.reply.assert_awaited_once()
        return str(event.reply.await_args.args[0])

    async def test_an_older_payment_is_announced_behind_the_window(self) -> None:
        await self._make_payments(12)
        self.assertEqual(await self.bot.db.payment_count(int(self.user["id"])), 12)
        text = await self._history_text()
        self.assertIn("۱۰ پرداخت اخیر از ۱۲ پرداخت", text)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("•")]), 10)

    async def test_a_short_history_is_not_called_a_window(self) -> None:
        await self._make_payments(3)
        text = await self._history_text()
        self.assertIn("سوابق پرداخت:", text)
        self.assertNotIn("از", text.splitlines()[0])
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("•")]), 3)

    async def test_no_payment_still_says_so(self) -> None:
        self.assertEqual(await self.bot.db.payment_count(int(self.user["id"])), 0)
        event = self._event("/history")
        await self.bot._handle_message(event)
        event.reply.assert_awaited_once()
        self.assertEqual(
            event.reply.await_args.args[0], "سابقهٔ پرداختی ندارید."
        )


if __name__ == "__main__":
    unittest.main()
