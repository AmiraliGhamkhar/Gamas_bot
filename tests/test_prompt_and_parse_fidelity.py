"""Fidelity guards: no silent text loss between a request and a booklet.

Each test pins one failure mode that could once drop or corrupt model text
without an error: prompt-boundary redaction of ordinary lesson words, a
non-payload JSON object being mistaken for the answer, silent truncation of a
field, a section dropped because a list arrived as a string, a table cell
sliced away, and a whole lecture line deleted by the backend scrubber.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from gamas_bot import llm
from gamas_bot.llm import LLMResponseError, _parse_json_object, extract_json_object
from gamas_bot.notes import (
    MAX_TEXT_CHARS,
    TABLE_CELL_MAX_CHARS,
    NoteSection,
    NoteTable,
    TRUNCATION_MARKER,
    _load_json_payload,
    scrub_backend_artifacts,
)
from tests.support import make_settings, sample_notes_json


class PromptSecretFidelityTests(unittest.TestCase):
    """The secret filter removes credentials, never ordinary lesson words."""

    def test_short_environment_value_does_not_rewrite_lesson_text(self):
        settings = make_settings(avalai_api_key="av-test", note_providers=("avalai",))
        with mock.patch.dict(os.environ, {"LOGIN_PASS": "test", "SIGNING_KEY": "demo"}, clear=False):
            sanitized = llm._sanitize_prompt_text(
                "In this lesson we test the algorithm with a demo dataset.", settings
            )
        self.assertEqual(sanitized, "In this lesson we test the algorithm with a demo dataset.")

    def test_real_credentials_are_still_removed(self):
        settings = make_settings(avalai_api_key="av-test", note_providers=("avalai",))
        with mock.patch.dict(os.environ, {"DB_PASSWORD": "hunter2"}, clear=False):
            sanitized = llm._sanitize_prompt_text(
                "PASSWORD=hunter2 and API_KEY=generic-api-secret-123", settings
            )
        self.assertNotIn("hunter2", sanitized)
        self.assertNotIn("generic-api-secret-123", sanitized)
        self.assertGreaterEqual(sanitized.count("[REDACTED]"), 2)

    def test_schema_like_assignments_in_a_lesson_survive(self):
        """"password: str" is a column type in a database lecture, not a secret."""
        settings = make_settings(avalai_api_key="av-test", note_providers=("avalai",))
        for text in (
            "password: str  # نوع داده",
            "PRIMARY KEY: student_id",
            "the key: value pair",
        ):
            with self.subTest(text=text):
                self.assertEqual(llm._sanitize_prompt_text(text, settings), text)

    def test_token_shaped_values_are_still_removed(self):
        settings = make_settings(avalai_api_key="av-test", note_providers=("avalai",))
        sanitized = llm._sanitize_prompt_text(
            "OpenRouter token sk-or-v1-abcdefghijklmnopqrstu", settings
        )
        self.assertNotIn("sk-or-v1", sanitized)


class JsonAnswerSelectionTests(unittest.TestCase):
    """A model answer must be the payload, never a placeholder or half an object."""

    def test_a_single_note_payload_still_parses(self):
        parsed = _parse_json_object(sample_notes_json())
        self.assertIn("sections", parsed)

    def test_echoed_contract_example_plus_real_answer_is_ambiguous(self):
        """Two complete answers cannot be silently resolved by "first one wins"."""
        echoed = '{"title":"عنوان کوتاه و علمی جزوه","sections":[{"heading":"عنوان فصل یا مبحث"}]}'
        real = json.dumps(
            {"title": "جزوهٔ فشار خون", "sections": [{"heading": "فشار خون", "paragraphs": ["متن"]}]},
            ensure_ascii=False,
        )
        with self.assertRaises(LLMResponseError):
            _parse_json_object(f"ساختار خواسته‌شده: {echoed}\n\nخروجی نهایی: {real}")

    def test_one_answer_among_prose_and_set_notation_is_still_found(self):
        raw = (
            "<think>Let S = {1, 2, 3} be our set.</think>\n"
            "در ریاضی مجموعهٔ {a, b} را در نظر بگیرید:\n"
            '{"sections": [{"heading": "مجموعه‌ها", "paragraphs": ["متن"]}]}'
        )
        self.assertEqual(json.loads(extract_json_object(raw))["sections"][0]["heading"], "مجموعه‌ها")

    def test_duplicate_top_level_key_is_an_error_not_a_silent_overwrite(self):
        raw = (
            '{"title":"جزوه","sections":[{"heading":"فصل یک","paragraphs":["متن یک"]}],'
            '"sections":[{"heading":"فصل دو","paragraphs":["متن دو"]}]}'
        )
        with self.assertRaises(LLMResponseError):
            _parse_json_object(raw)

    def test_duplicate_key_is_an_error_on_the_orjson_fast_path_too(self):
        raw = '{"a": 1, "a": 2}'
        with self.assertRaises(LLMResponseError):
            _parse_json_object(raw)


class BoundedTextFieldTests(unittest.TestCase):
    """Truncation is allowed; doing it invisibly is not."""

    def test_over_long_paragraph_keeps_a_visible_cut_mark_and_logs(self):
        payload = {"heading": "h", "paragraphs": ["الف " * 12_000]}
        with self.assertLogs("gamas_bot.notes", level="WARNING") as logs:
            section = NoteSection.from_payload(payload, 1)
        text = section.paragraphs[0]
        self.assertLessEqual(len(text), MAX_TEXT_CHARS)
        self.assertTrue(text.endswith(TRUNCATION_MARKER))
        self.assertTrue(any("truncated" in line for line in logs.output))

    def test_short_text_is_returned_untouched(self):
        payload = {"heading": "h", "paragraphs": ["متن کوتاه درس"]}
        section = NoteSection.from_payload(payload, 1)
        self.assertEqual(section.paragraphs[0], "متن کوتاه درس")
        self.assertNotIn(TRUNCATION_MARKER, section.paragraphs[0])


class WrongShapePayloadTests(unittest.TestCase):
    """A documented list sent as one value keeps its content instead of vanishing."""

    def test_section_with_a_string_paragraphs_field_is_kept(self):
        payload = {
            "title": "جزوه",
            "summary": "s",
            "sections": [
                {"heading": "فصل یک", "paragraphs": "یک پاراگراف کامل که مدل رشته فرستاد."},
                {"heading": "فصل دو", "paragraphs": ["پاراگراف دوم سالم"]},
            ],
        }
        notes_obj = _load_json_payload(payload)
        self.assertEqual([s.heading for s in notes_obj.sections], ["فصل یک", "فصل دو"])
        self.assertIn("رشته فرستاد", notes_obj.sections[0].paragraphs[0])

    def test_wrapped_text_items_are_unwrapped_not_rendered_as_python(self):
        section = NoteSection.from_payload(
            {"heading": "h", "bullets": [{"text": "نکتهٔ مهم"}, {"content": "نکتهٔ دوم"}]}, 1
        )
        self.assertEqual(section.bullets, ("نکتهٔ مهم", "نکتهٔ دوم"))

    def test_a_single_callout_object_is_accepted(self):
        section = NoteSection.from_payload(
            {"heading": "h", "callouts": {"kind": "نکته", "text": "یادآوری"}}, 1
        )
        self.assertEqual(section.callouts[0].text, "یادآوری")

    def test_table_row_wider_than_headers_keeps_the_surplus_cells(self):
        table = NoteTable.from_payload({"headers": ["a", "b"], "rows": [["1", "2", "3"]]})
        self.assertEqual(table.rows, [["1", "2 3"]])

    def test_long_table_cell_is_bounded_visibly(self):
        table = NoteTable.from_payload(
            {"headers": ["a"], "rows": [["ب" * (TABLE_CELL_MAX_CHARS + 50)]]}
        )
        self.assertLessEqual(len(table.rows[0][0]), TABLE_CELL_MAX_CHARS)
        self.assertTrue(table.rows[0][0].endswith(TRUNCATION_MARKER))


class BackendScrubFidelityTests(unittest.TestCase):
    """Scrubbing masks plumbing; it never deletes a teaching sentence."""

    def test_http_status_lecture_sentence_survives_intact(self):
        text = "کد وضعیت 429 یعنی Too Many Requests و سرور از rate limit استفاده می‌کند."
        self.assertEqual(scrub_backend_artifacts(text), text)

    def test_two_status_codes_in_one_sentence_do_not_delete_the_line(self):
        text = "خطای 503 Service Unavailable یعنی سرور موقتاً در دسترس نیست و 504 هم داریم."
        self.assertEqual(scrub_backend_artifacts(text), text)

    def test_a_raw_backend_report_is_masked_not_kept(self):
        scrubbed = scrub_backend_artifacts("HTTP 429 rate limit exceeded, retry after 30s")
        self.assertNotIn("429", scrubbed)
        self.assertIn("•••", scrubbed)

    def test_key_shaped_tokens_and_self_references_are_still_removed(self):
        self.assertNotIn("sk-abcdefghijklmnop123", scrub_backend_artifacts("کلید sk-abcdefghijklmnop123 است."))
        self.assertNotIn("OpenAI", scrub_backend_artifacts("generated by OpenAI in 2024"))

    def test_provider_product_name_inside_a_lesson_sentence_survives(self):
        text = "سرویس OpenAI API را برای این کار استفاده می‌کنیم."
        scrubbed = scrub_backend_artifacts(text)
        self.assertIn("استفاده می‌کنیم", scrubbed)


class UnknownFinishReasonTests(unittest.TestCase):
    """A finish reason this code does not know cannot prove a complete answer."""

    def test_openai_unknown_reason_raises(self):
        payload = {"choices": [{"message": {"content": "متن"}, "finish_reason": "incomplete"}]}
        with self.assertRaises(LLMResponseError):
            llm._parse_response(payload, "openai")

    def test_openai_normal_stop_is_accepted(self):
        payload = {"choices": [{"message": {"content": "متن"}, "finish_reason": "stop"}]}
        self.assertEqual(llm._parse_response(payload, "openai"), "متن")

    def test_gemini_other_reason_raises(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "متن"}]}, "finishReason": "OTHER"}]}
        with self.assertRaises(LLMResponseError):
            llm._parse_response(payload, "gemini")

    def test_gemini_stop_is_accepted(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "متن"}]}, "finishReason": "STOP"}]}
        self.assertEqual(llm._parse_response(payload, "gemini"), "متن")


class NotePayloadIntegrationTests(unittest.TestCase):
    """The strictness above works end to end on the real note loader."""

    def test_a_clean_answer_loads_with_every_section(self):
        parsed = _parse_json_object(sample_notes_json())
        loaded = _load_json_payload(parsed)
        self.assertEqual([section.heading for section in loaded.sections], ["بخش نخست"])
        self.assertTrue(loaded.has_content)

    def test_wrongly_typed_fields_survive_the_whole_loader(self):
        payload = {
            "title": "جزوهٔ آزمایشی",
            "summary": "خلاصه",
            "sections": [{"heading": "فصل", "paragraphs": "یک پاراگراف رشته‌ای مهم"}],
            "key_points": "نکتهٔ کلیدی کل درس",
        }
        loaded = _load_json_payload(payload)
        self.assertEqual(loaded.sections[0].paragraphs, ("یک پاراگراف رشته‌ای مهم",))
        self.assertEqual(loaded.key_points, ("نکتهٔ کلیدی کل درس",))
