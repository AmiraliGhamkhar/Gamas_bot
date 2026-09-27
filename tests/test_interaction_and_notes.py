from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gamas_bot.bot import FORMATS_TEXT, HELP_TEXT, PRIVACY_TEXT, WELCOME_TEXT, main_menu
from gamas_bot.config import Settings
from gamas_bot.progress import JobProgress, progress_bar, progress_text
from gamas_bot.structuring import (
    StructuringError,
    _endpoint,
    _provider_request,
    _provider_response,
)

from support import make_settings


class InlineMenuTests(unittest.TestCase):
    def test_help_copy_is_short_clear_and_command_free(self):
        self.assertLess(len(WELCOME_TEXT), 350)
        self.assertLess(len(HELP_TEXT), 650)
        self.assertIn("فایل صوتی، ویدیو یا PowerPoint", HELP_TEXT)
        self.assertIn("بدون صدا", HELP_TEXT)
        self.assertIn("فایل‌های موقت", PRIVACY_TEXT)
        self.assertIn("MP3", FORMATS_TEXT)
        self.assertNotIn("/help", WELCOME_TEXT + HELP_TEXT)

    def test_main_menu_contains_user_and_admin_callbacks(self):
        user_menu = main_menu(False)
        admin_menu = main_menu(True)
        user_callbacks = {
            button.type.data
            for row in user_menu
            for button in row
        }
        admin_callbacks = {
            button.type.data
            for row in admin_menu
            for button in row
        }
        self.assertIn(b"menu:create", user_callbacks)
        self.assertIn(b"menu:help", user_callbacks)
        self.assertNotIn(b"admin:home", user_callbacks)
        self.assertIn(b"admin:home", admin_callbacks)


class FakeEditableMessage:
    def __init__(self):
        self.edits: list[str] = []

    async def edit(self, text: str, **_kwargs):
        self.edits.append(text)


class FakeProgressEvent:
    def __init__(self):
        self.message = FakeEditableMessage()
        self.replies: list[str] = []
        self.responses: list[str] = []

    async def reply(self, text: str, **_kwargs):
        self.replies.append(text)
        return self.message

    async def respond(self, text: str, **_kwargs):
        self.responses.append(text)


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def test_bar_is_bounded_and_fixed_width(self):
        self.assertEqual(progress_bar(-20), "░" * 10)
        self.assertEqual(progress_bar(100), "█" * 10)
        self.assertEqual(len(progress_bar(55)), 10)
        self.assertIn("55٪", progress_text(55, "آزمایش"))

    async def test_reporter_edits_one_message_and_never_moves_backwards(self):
        event = FakeProgressEvent()
        progress = await JobProgress.create(event, submission_id=7)
        await progress.update(40, "مرحله اول")
        await progress.update(20, "مرحله دوم")
        await progress.complete()
        self.assertEqual(len(event.replies), 1)
        self.assertEqual(event.responses, [])
        self.assertIn("40٪", event.message.edits[0])
        self.assertIn("40٪", event.message.edits[1])
        self.assertIn("جزوه آماده شد", event.message.edits[-1])


class NoteProviderTests(unittest.TestCase):
    def test_exact_custom_endpoint_keeps_its_query_string(self):
        endpoint = (
            "https://example.test/openai/deployments/model/chat/completions"
            "?api-version=2026-01-01"
        )
        self.assertEqual(_endpoint(endpoint, "chat/completions"), endpoint)

    def test_openai_compatible_request_is_configurable(self):
        settings = make_settings(
            note_api_provider="openai_compatible",
            note_api_key="secret",
            note_api_base_url="https://openrouter.ai/api/v1",
            note_api_model="provider/model",
            note_api_extra_headers=(("X-Title", "Gamas"),),
        )
        url, headers, payload, params = _provider_request("متن", settings, "دستور: ")
        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(headers["X-Title"], "Gamas")
        self.assertEqual(payload["model"], "provider/model")
        self.assertIn("دستور: متن", payload["messages"][1]["content"])
        self.assertEqual(params, {})

    def test_anthropic_and_gemini_responses_are_normalized(self):
        self.assertEqual(
            _provider_response(
                {"content": [{"type": "text", "text": "جزوه"}]}, "anthropic"
            ),
            "جزوه",
        )
        self.assertEqual(
            _provider_response(
                {"candidates": [{"content": {"parts": [{"text": "جزوه"}]}}]},
                "gemini",
            ),
            "جزوه",
        )
        self.assertEqual(
            _provider_response(
                {"choices": [{"message": {"content": "جزوه"}}]},
                "openai_compatible",
            ),
            "جزوه",
        )
        with self.assertRaises(StructuringError):
            _provider_response(
                {"choices": [{"message": {"content": None}}]},
                "openai_compatible",
            )

    def test_provider_settings_are_loaded_and_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\n"
                "TELEGRAM_API_ID=1\n"
                "TELEGRAM_API_HASH=h\n"
                "SPEECHMATICS_API_KEY=stt\n"
                "NOTE_API_PROVIDER=openai\n"
                "NOTE_API_BASE_URL=https://example.test/v1\n"
                "NOTE_API_MODEL=my-model\n"
                'NOTE_API_EXTRA_HEADERS_JSON={"X-App":"Gamas"}\n'
                "LOG_FORMAT=json\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                settings = Settings.from_env(env)
        self.assertEqual(settings.note_api_provider, "openai_compatible")
        self.assertEqual(settings.note_api_model, "my-model")
        self.assertEqual(settings.note_api_extra_headers, (("X-App", "Gamas"),))
        self.assertEqual(settings.log_format, "json")


if __name__ == "__main__":
    unittest.main()
