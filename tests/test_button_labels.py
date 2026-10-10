"""Telegram caps a button label at 64 UTF-16 code units, and one oversized button
makes the whole message unsendable.

Two panel labels are built from stored, human-written text whose length no panel
controls: the purchase button interpolates a plan *name* (the schema keeps up to
60 characters) and the provider-health button interpolates a credential *label*
(no length limit at all). Before the clamp, a long enough value did not make the
button small — it made the screen disappear for every user. Panels already trim
some labels by hand (``label[:16]``, ``model.model_id[:32]``); those are safe, if
crude, and this file checks the two that were not, plus the shared helper they use.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support import make_settings

from gamas_bot.bot import StudyBot
from gamas_bot.database import Database
from gamas_bot.telegram_text import BUTTON_LABEL_LIMIT, _utf16_length, button_label


def _utf16(value: str) -> int:
    return _utf16_length(value)


def _button_data(button) -> bytes:
    """Callback payload of a Telethon inline button (any 1.x layout)."""
    data = getattr(button, "data", None)
    if data:
        return bytes(data)
    inner = getattr(button, "type", None)
    data = getattr(inner, "data", None)
    return bytes(data) if data else b""


def _buttons(rows: list) -> list[tuple[str, str]]:
    """Every rendered button as ``(label, callback payload)``."""
    out: list[tuple[str, str]] = []
    for row in rows:
        for button in row:
            label = getattr(button, "text", None)
            out.append(
                (
                    str(label if label is not None else button),
                    _button_data(button).decode("utf-8"),
                )
            )
    return out


def _labels(rows: list) -> list[str]:
    return [label for label, _data in _buttons(rows)]


class ButtonLabelHelperTests(unittest.TestCase):
    def test_a_label_within_budget_is_returned_untouched(self) -> None:
        for value in ("🎙 STT", "طرح ماهانه", "ط" * BUTTON_LABEL_LIMIT):
            with self.subTest(value=value[:12]):
                self.assertEqual(button_label(value), value)

    def test_a_long_label_fits_and_says_so(self) -> None:
        label = button_label("خرید " + "ط" * 90)
        self.assertLessEqual(_utf16(label), BUTTON_LABEL_LIMIT)
        self.assertTrue(label.startswith("خرید "))
        self.assertTrue(label.endswith("…"))

    def test_astral_characters_count_double_and_still_fit(self) -> None:
        # 40 emoji are 80 code units: a plain 64-character slice would stay over
        # the limit, so the cut has to be measured, not counted in characters.
        label = button_label("🧪" * 40)
        self.assertLessEqual(_utf16(label), BUTTON_LABEL_LIMIT)
        self.assertEqual(_utf16(label), 63)
        self.assertTrue(label.endswith("…"))

    def test_whitespace_at_the_cut_is_not_kept(self) -> None:
        label = button_label("abcdefghij " * 12)
        self.assertFalse(label[:-1].endswith(" "))
        self.assertLessEqual(_utf16(label), BUTTON_LABEL_LIMIT)

    def test_a_one_unit_budget_still_returns_a_label(self) -> None:
        # Telegram refuses an empty button text, so the ellipsis alone is the floor.
        self.assertEqual(button_label("طرح بلند یک کاربر", 1), "…")

    def test_an_impossible_budget_is_rejected_loudly(self) -> None:
        for limit in (0, -3):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    button_label("هر متن", limit)


class PurchaseButtonLabelTests(unittest.IsolatedAsyncioTestCase):
    """The longest legal plan name must not cost the user their purchase screen."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "labels.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({7}),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()
        # A name the schema will accept (it keeps 60 characters) whose button
        # cannot fit next to a price.
        self.long_name = "طرح ویژهٔ پایان‌ترم با پشتیبانی و ویرایش دستی جزوه و کیفیت"
        self.price = 999_999_999

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp.cleanup()

    async def test_a_long_plan_name_keeps_the_button_sendable_and_priced(self) -> None:
        await self.bot.db.create_plan(
            code="longnameplan",
            name=self.long_name,
            hours=40,
            price_toman=self.price,
            validity_days=30,
            admin_id=7,
        )
        from gamas_bot.billing import format_toman

        raw = f"خرید {self.long_name} — {format_toman(self.price)}"
        self.assertGreater(_utf16(raw), BUTTON_LABEL_LIMIT, raw)  # the premise

        text, buttons = await self.bot._billing_plans()
        labels = _labels(buttons)
        for label in labels:
            self.assertLessEqual(_utf16(label), BUTTON_LABEL_LIMIT, label)
        pairs = _buttons(buttons)
        buy = next(label for label, data in pairs if data == "billing:buy:longnameplan")
        # shortened, but the decision number is still on the button
        self.assertTrue(buy.endswith("تومان"), buy)
        self.assertNotIn(self.long_name, buy)
        self.assertIn("…", buy)
        # the message body still carries the full name
        self.assertIn(self.long_name, text)

    async def test_the_seeded_catalog_needs_no_clamp_at_all(self) -> None:
        _text, buttons = await self.bot._billing_plans()
        for row in buttons:
            for button in row:
                label = str(getattr(button, "text", ""))
                # six seeded plans, nothing invented: the ellipsis must not appear
                self.assertNotIn("…", label, label)


class HealthPanelButtonLabelTests(unittest.IsolatedAsyncioTestCase):
    """A credential label is free text; the panel that lists keys must survive it."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "health.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({7}),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp.cleanup()

    async def test_a_long_credential_label_is_clamped_in_the_button_only(self) -> None:
        from gamas_bot.provider_health import HealthResult

        long_label = (
            "کلید اصلی حساب دوم سازمانی با محدودیت سهمیهٔ ویژه برای ساعات اوج و "
            "هماهنگی با مدیر مالی"
        )
        self.assertGreater(_utf16(long_label), BUTTON_LABEL_LIMIT, long_label)

        async def fake_check_all(force: bool = False) -> list[HealthResult]:
            return [
                HealthResult(
                    service="notes",
                    provider="openai_compatible",
                    credential_id=41,
                    source="database",
                    label=long_label,
                    masked="••••••••1111",
                    status="rate_limited",
                    http_status=429,
                    latency_ms=12,
                    checked_at="2026-10-11T00:00:00+00:00",
                    detail="too many requests",
                )
            ]

        self.bot.provider_health = SimpleNamespace(check_all=fake_check_all)
        event = SimpleNamespace(answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        await self.bot._show_provider_health(event)

        # `_edit_callback` passes the message positionally and the keyboard by name
        shown = event.edit.await_args.args[0]
        buttons = event.edit.await_args.kwargs["buttons"]
        labels = _labels(buttons)
        self.assertTrue(labels)
        for label in labels:
            self.assertLessEqual(_utf16(label), BUTTON_LABEL_LIMIT, label)
        test_button = next(label for label in labels if label.startswith("🧪 تست"))
        self.assertTrue(test_button.endswith("…"), test_button)
        self.assertIn("••••••••1111", shown)  # the panel text keeps the whole story
        self.assertNotIn("sk-secret", shown)


if __name__ == "__main__":
    unittest.main()
