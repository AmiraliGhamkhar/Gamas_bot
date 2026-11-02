"""Copy-contract tests for the centralized Persian UX module (workstream A)."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from gamas_bot import texts
from gamas_bot.telegram_text import TELEGRAM_TEXT_LIMIT


ROOT = Path(__file__).resolve().parent.parent / "gamas_bot"
MIGRATED_MODULES = (
    "bot.py",
    "bot_wallet.py",
    "bot_support.py",
    "subscription.py",
    "progress.py",
)


def _persian_literals(path: Path) -> list[tuple[int, str]]:
    """String constants containing Persian letters, with their source lines."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any("\u0621" <= char <= "\u064a" for char in node.value):
                found.append((getattr(node, "lineno", 0), node.value))
    return found


class MainAndHelpCopyTests(unittest.TestCase):
    def test_welcome_screen_is_deterministic_and_within_one_message(self):
        first = texts.main_screen(
            first_start=True,
            balance="۲۵ ساعت",
            office_enabled=True,
            notes_configured=True,
        )
        second = texts.main_screen(
            first_start=True,
            balance="۲۵ ساعت",
            office_enabled=True,
            notes_configured=True,
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("🎓 به گاماس خوش آمدید\n\n"))
        self.assertIn("💳 اعتبار باقی‌مانده: ۲۵ ساعت", first)
        self.assertLessEqual(len(first.encode("utf-16-le")) // 2, TELEGRAM_TEXT_LIMIT)

    def test_returning_screen_uses_the_main_menu_heading(self):
        text = texts.main_screen(
            first_start=False,
            balance="۱ ساعت",
            office_enabled=False,
            notes_configured=False,
        )
        self.assertTrue(text.startswith("منوی اصلی\n\n"))
        self.assertNotIn("آفیس رایگان", text)
        self.assertIn("متن گفتار و سند", text)

    def test_help_is_one_short_screen_and_keeps_the_billing_contract(self):
        text = texts.help_screen(office_enabled=True)
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(text.startswith("ℹ️ راهنمای گاماس\n\n"))
        self.assertLessEqual(len(lines), 15)
        self.assertIn("اعتباری کم نمی‌شود", text)
        self.assertIn("خطای ساخت جزوه", text)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, TELEGRAM_TEXT_LIMIT)


class GateAndProgressCopyTests(unittest.TestCase):
    def test_gate_copy_names_the_channel_and_the_check_button(self):
        message = texts.SUBSCRIPTION_REQUIRED_TEMPLATE.format(
            channel_url="https://t.me/gamas_AI"
        )
        self.assertIn("کانال گاماس بپیوندید", message)
        self.assertIn("https://t.me/gamas_AI", message)
        self.assertIn("✅ بررسی عضویت", message)
        self.assertEqual(texts.BTN_CHANNEL_JOIN, "🔗 عضویت در کانال")

    def test_confirmation_and_unavailable_copy_are_distinct(self):
        self.assertIn("تأیید شد", texts.SUBSCRIPTION_CONFIRMED)
        self.assertIn("در دسترس نیست", texts.SUBSCRIPTION_UNAVAILABLE)
        self.assertNotIn("تأیید شد", texts.SUBSCRIPTION_UNAVAILABLE)

    def test_progress_headlines_say_what_is_happening(self):
        self.assertIn("در حال پردازش فایل", texts.PROGRESS_DEFAULT_HEADLINE)
        self.assertIn("آماده شد", texts.PROGRESS_WORK_DONE)
        self.assertNotEqual(texts.PROGRESS_DEFAULT_FAIL, texts.PROGRESS_WORK_FAIL)


class CopyCentralizationGuardTests(unittest.TestCase):
    """Migrated modules must not carry Persian string literals any more."""

    def test_migrated_modules_have_no_persian_literals(self):
        offenders: list[str] = []
        for name in MIGRATED_MODULES:
            for line, value in _persian_literals(ROOT / name):
                offenders.append(f"{name}:{line}: {value[:40]}")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
