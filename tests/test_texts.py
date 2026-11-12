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
        self.assertTrue(first.startswith("به گاماس خوش آمدید\n\n"))
        self.assertIn("اعتبار: ۲۵ ساعت", first)
        # One invitation, stated in one sentence.
        self.assertIn("فایل درس را همین‌جا بفرستید", first)
        self.assertLessEqual(len(first.encode("utf-16-le")) // 2, TELEGRAM_TEXT_LIMIT)

    def test_returning_screen_uses_the_main_menu_heading(self):
        text = texts.main_screen(
            first_start=False,
            balance="۱ ساعت",
            office_enabled=False,
            notes_configured=False,
        )
        self.assertTrue(text.startswith("فایل درس بعدی را همین‌جا بفرستید."))
        self.assertNotIn("آفیس", text)
        self.assertNotIn("کتابخانه", text)
        self.assertIn("اعتبار: ۱ ساعت", text)
        # The returning screen is shorter than the welcome screen: the pitch has
        # already been made, so all that is left is the next file and the balance.
        self.assertLess(len(text), 200)

    def test_raw_only_welcome_does_not_promise_generated_learning_outputs(self):
        text = texts.main_screen(
            first_start=True,
            balance="۱ ساعت",
            office_enabled=False,
            notes_configured=False,
        )
        self.assertIn("متن استخراج‌شده در کتابخانه می‌ماند", text)
        self.assertNotIn("جزوه و تمرین آماده می‌شوند", text)

    def test_home_screen_does_not_use_streaks_to_pressure_return_visits(self):
        one = texts.main_screen(
            first_start=False, balance="۱ ساعت", office_enabled=False,
            notes_configured=False, streak_days=1,
        )
        two = texts.main_screen(
            first_start=False, balance="۱ ساعت", office_enabled=False,
            notes_configured=False, streak_days=20,
        )
        self.assertNotIn("🔥", one)
        self.assertNotIn("پشت‌سرهم", two)

    def test_help_is_one_short_screen_and_keeps_the_billing_contract(self):
        text = texts.help_screen(office_enabled=True, notes_configured=True)
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(text.startswith("ℹ️ راهنمای گاماس\n\n"))
        self.assertLessEqual(len(lines), 15)
        self.assertIn("اعتباری کم نمی‌شود", text)
        self.assertIn("خطای ساخت جزوه", text)
        self.assertIn(texts.OFFICE_CREDIT_NOTICE, text)
        self.assertIn("سقف ورودی و محدودیت ظرفیت", text)
        self.assertIn("صدای PowerPoint خودکار پردازش نمی‌شود", text)
        self.assertIn(texts.DOCUMENT_OCR_OFF_NOTICE, text)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, TELEGRAM_TEXT_LIMIT)

    def test_help_availability_copy_matches_raw_only_deployment(self):
        text = texts.help_screen(office_enabled=False, notes_configured=False)
        self.assertIn("ساخت خودکار جزوه", text)
        self.assertIn("فعلاً فعال نیست", text)
        self.assertNotIn("نخستین آزمون", text)
        self.assertNotIn("ساخت آزمون و فلش‌کارت هوش مصنوعی اعتبار کم می‌کند", text)
        self.assertIn("اعتباری کم نمی‌شود", text)

    def test_results_empty_copy_reflects_raw_only_deployment(self):
        text = texts.results_hub_empty(notes_configured=False)
        self.assertIn("متن گفتار ذخیره شود", text)
        self.assertNotIn("جزوه، متن خام و نکات کلیدی", text)
        self.assertEqual(
            texts.results_hub_empty(notes_configured=True), texts.RESULTS_HUB_EMPTY
        )

    def test_help_explains_optional_third_party_ocr_when_enabled(self):
        text = texts.help_screen(
            office_enabled=False,
            notes_configured=False,
            ocr_enabled=True,
        )
        self.assertIn(texts.DOCUMENT_OCR_ON_NOTICE, text)
        self.assertIn("سرویس بیرونی", text)
        self.assertIn(texts.DOCUMENT_EMBEDDED_IMAGES_NOTICE, text)
        self.assertNotIn(texts.DOCUMENT_OCR_OFF_NOTICE, text)

    def test_empty_states_and_learning_copy_match_feature_availability(self):
        self.assertNotIn("جزوه به‌صورت خودکار", texts.library_empty(notes_configured=False))
        self.assertIn("متن گفتار ذخیره شود", texts.results_hub_empty(notes_configured=False))
        self.assertIn("فعلاً فعال نیست", texts.documents_empty(notes_configured=False))
        self.assertIn("کارت‌های دستی", texts.flashcard_empty(notes_configured=False))
        self.assertNotIn("از یک جزوه بسازید", texts.flashcard_empty(notes_configured=False))
        self.assertIn("فعلاً فعال نیست", texts.leaderboard_empty(notes_configured=False))
        self.assertIn("اولین آزمون", texts.leaderboard_empty(notes_configured=True))

    def test_referral_rules_explain_first_start_eligibility(self):
        self.assertIn("نخستین ورود دوست", texts.REFERRALS_RULES)


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
