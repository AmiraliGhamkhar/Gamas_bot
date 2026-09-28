from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gamas_bot.bot import FORMATS_TEXT, HELP_TEXT, PRIVACY_TEXT, WELCOME_TEXT, main_menu
from gamas_bot.config import Settings
from gamas_bot.progress import (
    SPINNER_FRAMES,
    JobProgress,
    progress_bar,
    progress_text,
    to_persian_digits,
)
from gamas_bot.structuring import (
    StructuringError,
    _endpoint,
    _provider_request,
    _provider_response,
    merge_structured_notes,
    parse_structured_notes,
    structure_transcript,
)

from support import fake_session_factory, make_settings, sample_notes_json


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
    def test_bar_is_bounded_fixed_width_and_carries_the_head(self):
        self.assertEqual(progress_bar(-20), "🚀" + "░" * 11)
        self.assertEqual(progress_bar(100), "█" * 12)
        self.assertEqual(progress_bar(100, head="🚀"), "█" * 12)
        self.assertEqual(len(progress_bar(55)), 12)
        self.assertIn("🚀", progress_bar(55))
        self.assertNotIn("🚀", progress_bar(100))

    def test_progress_text_is_animated_and_persian(self):
        self.assertIn("۵۵٪", progress_text(55, "آزمایش"))
        self.assertIn("🚀", progress_text(55, "آزمایش"))
        self.assertNotIn("55٪", progress_text(55, "آزمایش"))
        frames = {
            progress_text(55, "آزمایش", frame=frame)
            for frame in range(len(SPINNER_FRAMES))
        }
        self.assertGreater(len(frames), 1)
        self.assertIn("آزمایش", next(iter(frames)))
        self.assertEqual(to_persian_digits(100), "۱۰۰")

    async def test_reporter_edits_one_message_and_never_moves_backwards(self):
        event = FakeProgressEvent()
        progress = await JobProgress.create(event, submission_id=7)
        await progress.update(40, "مرحله اول")
        await progress.update(20, "مرحله دوم")
        await progress.complete()
        self.assertEqual(len(event.replies), 1)
        self.assertEqual(event.responses, [])
        self.assertIn("۴۰٪", event.message.edits[0])
        self.assertIn("۴۰٪", event.message.edits[1])
        self.assertIn("جزوه آماده شد", event.message.edits[-1])
        self.assertIn("🎉", event.message.edits[-1])
        # The animation ticker must stop once the job is complete.
        await progress.aclose()
        self.assertIsNone(progress._ticker)
        self.assertTrue(progress._done)

    async def test_animation_ticker_keeps_the_spinner_cycling(self):
        event = FakeProgressEvent()
        progress = await JobProgress.create(
            event, submission_id=8, animation_interval=0.01
        )
        await progress.update(50, "مرحله طولانی")
        edits_before = len(event.message.edits)
        # Long stages (STT!) stay visually alive: the ticker re-edits the
        # message with the next spinner frame while the stage is unchanged.
        await asyncio.sleep(0.08)
        try:
            self.assertGreater(len(event.message.edits), edits_before)
            frames = event.message.edits[edits_before:]
            self.assertTrue(any("۵۰٪" in frame for frame in frames))
            self.assertGreater(len(set(frames)), 1)  # frames really differ
        finally:
            await progress.aclose()
        stopped = len(event.message.edits)
        await asyncio.sleep(0.05)
        self.assertEqual(len(event.message.edits), stopped)

    async def test_animation_can_be_disabled(self):
        event = FakeProgressEvent()
        progress = await JobProgress.create(
            event, submission_id=9, animate=False, animation_interval=0.01
        )
        await progress.update(60, "مرحله")
        await asyncio.sleep(0.05)
        self.assertIsNone(progress._ticker)
        self.assertEqual(len(event.message.edits), 1)
        await progress.aclose()


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


class StrictJsonPromptTests(unittest.TestCase):
    def test_openai_compatible_request_carries_the_strict_system_prompt(self):
        settings = make_settings(
            note_api_provider="openai_compatible",
            note_api_key="secret",
            note_api_base_url="https://api.example.test/v1",
            note_api_model="model",
        )
        _url, _headers, payload, _params = _provider_request("متن", settings, "دستور: ")
        system_message, user_message = payload["messages"]
        self.assertEqual(system_message["role"], "system")
        self.assertIn("JSON", system_message["content"])
        self.assertIn("sections", system_message["content"])
        self.assertIn("callouts", system_message["content"])
        self.assertEqual(user_message["role"], "user")
        self.assertIn("دستور: متن", user_message["content"])
        # JSON mode is opt-in: unsupported gateways must not receive it.
        self.assertNotIn("response_format", payload)

    def test_openai_compatible_json_mode_is_opt_in(self):
        settings = make_settings(
            note_api_provider="openai_compatible",
            note_api_key="secret",
            note_api_base_url="https://api.example.test/v1",
            note_api_model="model",
            note_api_json_mode=True,
        )
        _url, _headers, payload, _params = _provider_request("متن", settings, "دستور: ")
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_gemini_request_uses_system_instruction_and_native_json_mode(self):
        settings = make_settings()
        _url, _headers, payload, _params = _provider_request("متن", settings, "دستور: ")
        self.assertIn("JSON", payload["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(
            payload["generationConfig"]["responseMimeType"], "application/json"
        )

    def test_anthropic_request_carries_the_system_prompt(self):
        settings = make_settings(note_api_provider="anthropic", note_api_key="secret")
        _url, _headers, payload, _params = _provider_request("متن", settings, "دستور: ")
        self.assertIn("JSON", payload["system"])
        self.assertIn("متن", payload["messages"][0]["content"])


class StructuredNotesTests(unittest.TestCase):
    def test_valid_payload_is_parsed_and_normalised(self):
        notes = parse_structured_notes(
            '{"title": "فارماکولوژی", "summary": "خلاصه", '
            '"sections": [{"heading": "Metformin", "paragraphs": ["خط اول"], '
            '"bullets": ["دوز"], "key_points": ["کلیوی"], '
            '"table": {"headers": ["دارو", "دوز"], "rows": [["Metformin", "500 mg"]]}, '
            '"callouts": [{"kind": "هشدار", "text": "قطع در AKI"}, {"kind": "نامعلوم", "text": "بدون نوع"}]}], '
            '"key_points": ["HbA1c"], "glossary": [{"term": "HbA1c", "definition": "هموگلوبین گلیکوزیله"}]}'
        )
        self.assertEqual(notes.display_title, "فارماکولوژی")
        self.assertEqual(len(notes.sections), 1)
        section = notes.sections[0]
        self.assertEqual(section.table.headers, ["دارو", "دوز"])
        self.assertEqual(section.callouts[0].kind, "هشدار")
        self.assertEqual(section.callouts[1].kind, "نکته")  # unknown kinds normalise
        self.assertEqual(notes.glossary[0].term, "HbA1c")
        self.assertIn("HbA1c", notes.to_markdown())
        self.assertIn("فارماکولوژی", notes.to_json())

    def test_code_fences_and_trailing_commas_are_repaired(self):
        raw = '```json\n{"title": "تست", "sections": [{"heading": "الف", "bullets": ["ب"],}],}\n```'
        notes = parse_structured_notes(raw)
        self.assertEqual(notes.title, "تست")
        self.assertEqual(notes.sections[0].bullets, ("ب",))

    def test_prose_around_json_is_tolerated(self):
        raw = 'بله، این جزوه است:\n{"title": "تست", "sections": [{"heading": "الف", "paragraphs": ["ب"]}]}\nامیدوارم مفید باشد.'
        notes = parse_structured_notes(raw)
        self.assertEqual(notes.title, "تست")

    def test_invalid_json_raises_a_structuring_error(self):
        with self.assertRaises(StructuringError):
            parse_structured_notes("این متن JSON نیست")
        with self.assertRaises(StructuringError):
            parse_structured_notes('{"title": "تست", "sections": [}]}')

    def test_empty_structure_is_rejected(self):
        with self.assertRaises(StructuringError):
            parse_structured_notes('{"title": "تست", "sections": []}')

    def test_merge_preserves_chunk_order_and_deduplicates_nothing(self):
        first = parse_structured_notes('{"title": "جزوه", "sections": [{"heading": "الف", "bullets": ["۱"]}]}')
        second = parse_structured_notes('{"title": "دیگری", "summary": "خلاصه", "sections": [{"heading": "ب", "bullets": ["۲"]}]}')
        merged = merge_structured_notes([first, second])
        self.assertEqual(merged.title, "جزوه")
        self.assertEqual(merged.summary, "خلاصه")
        self.assertEqual([section.heading for section in merged.sections], ["الف", "ب"])

    def test_fallback_headings_are_synthesised(self):
        notes = parse_structured_notes('{"title": "تست", "sections": [{"bullets": ["مورد"]}]}')
        self.assertEqual(notes.sections[0].heading, "بخش 1")


class StructuringRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_json_triggers_exactly_one_repair_pass(self):
        calls: list[str] = []

        async def fake_chunk(chunk, settings, session, prompt=None, **_kwargs):
            calls.append(chunk)
            if len(calls) == 1:
                return "متأسفم، این متن JSON نیست."
            return sample_notes_json()

        with patch(
            "gamas_bot.structuring._structure_chunk", side_effect=fake_chunk
        ), patch(
            "gamas_bot.structuring.aiohttp.ClientSession", fake_session_factory
        ):
            notes = await structure_transcript("متن درس", make_settings())
        self.assertEqual(len(calls), 2)
        self.assertEqual(notes.title, "جزوهٔ آزمایشی")

    async def test_persistent_garbage_fails_with_a_clear_error(self):
        async def fake_chunk(chunk, settings, session, prompt=None, **_kwargs):
            return "هنوز JSON نیست"

        with patch(
            "gamas_bot.structuring._structure_chunk", side_effect=fake_chunk
        ), patch(
            "gamas_bot.structuring.aiohttp.ClientSession", fake_session_factory
        ):
            with self.assertRaises(StructuringError) as caught:
                await structure_transcript("متن درس", make_settings())
        self.assertIn("JSON", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
