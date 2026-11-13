"""Regressions for note sensitivity routing, bounded repair and final acceptance.

Everything here is deterministic and offline: ``complete_json`` is either
mocked or served by a scripted HTTP double, so no provider quota is spent.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock
from urllib.parse import urlsplit

from gamas_bot import llm
from gamas_bot.bot_jobs import JobPipelineMixin
from gamas_bot.config import from_env
from gamas_bot.jobs import Job
from gamas_bot.notes import (
    MAX_NOTE_REPAIR_ATTEMPTS,
    SENSITIVITY_STANDARD_EDUCATIONAL,
    SENSITIVITY_UNKNOWN,
    NoteQualityError,
    StructuringError,
    classify_note_sensitivity,
    resolve_note_sensitivity,
    structure_document,
)
from tests.support import make_settings

CLEAN_LECTURE = (
    "امروز دربارهٔ فیزیولوژی کلیه صحبت می‌کنیم. "
    "نفرون واحد عملکردی کلیه است و روزانه ۵۰۰ میلی‌لیتر ادرار ساخته می‌شود."
)


def note_payload(*paragraphs: str, heading: str = "کلیه") -> dict:
    """A minimal payload that the note model accepts as a booklet."""
    return {
        "title": "فیزیولوژی کلیه",
        "summary": "خلاصهٔ درس",
        "sections": [{"heading": heading, "paragraphs": list(paragraphs)}],
    }


class SensitivityClassifierTests(unittest.TestCase):
    def test_ordinary_lecture_is_standard_educational(self):
        self.assertEqual(
            classify_note_sensitivity(CLEAN_LECTURE), SENSITIVITY_STANDARD_EDUCATIONAL
        )

    def test_identifiable_material_fails_closed(self):
        for material in (
            "شمارهٔ پرونده ۴۵۶۷۸ برای بیمار ثبت شد.",
            "کد ملی ۰۰۱۲۳۴۵۶۷۸ را وارد کنید.",
            "تماس با بیمار: 09121234567",
            "ایمیل بیمار ali@example.com است.",
            "Patient ID 1234567890 was recorded.",
            "",
        ):
            with self.subTest(material=material):
                self.assertEqual(classify_note_sensitivity(material), SENSITIVITY_UNKNOWN)

    def test_arabic_letter_variants_cannot_hide_an_identifier_marker(self):
        # Arabic YEH and KAF in the same wording must fold to the Persian marker.
        arabic = "شماره پروندهٔ بيمار ۴۵۶ ثبت شد."
        self.assertEqual(classify_note_sensitivity(arabic), SENSITIVITY_UNKNOWN)

    def test_declared_policy_overrides_the_classifier(self):
        document = "شمارهٔ پرونده ۴۵۶ ثبت شد."
        self.assertEqual(
            resolve_note_sensitivity(make_settings(note_data_sensitivity="auto"), document),
            SENSITIVITY_UNKNOWN,
        )
        for declared, expected in (
            ("internal", "INTERNAL"),
            ("sensitive", "SENSITIVE"),
            ("phi_or_pii", "PHI_OR_PII"),
            ("standard_educational", SENSITIVITY_STANDARD_EDUCATIONAL),
        ):
            with self.subTest(declared=declared):
                settings = make_settings(note_data_sensitivity=declared)
                self.assertEqual(resolve_note_sensitivity(settings, document), expected)

    def test_unknown_setting_value_resolves_to_auto(self):
        with mock.patch.dict(
            "os.environ", {"NOTE_DATA_SENSITIVITY": "not-a-label"}, clear=True
        ):
            # ``from_env`` clamps an unusable value instead of failing startup.
            self.assertEqual(
                from_env(env_file="/nonexistent").note_data_sensitivity, "auto"
            )


class SensitivityPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_and_repair_share_the_classified_label(self):
        source = (
            "شمارهٔ پرونده ۴۵۶۷۸ ثبت شد. "
            "نفرون واحد عملکردی کلیه است و روزانه ۵۰۰ میلی‌لیتر ادرار ساخته می‌شود."
        )
        generation = note_payload("نفرون واحد عملکردی کلیه است.")
        repair = note_payload(
            "نفرون واحد عملکردی کلیه است و روزانه ۵۰۰ میلی‌لیتر ادرار ساخته می‌شود."
        )
        calls: list[dict] = []

        async def fake_complete_json(_session, _settings, **kwargs):
            calls.append(kwargs)
            return repair if kwargs.get("task") == "BOOKLET_REPAIR" else generation

        settings = make_settings(
            note_strict_grounding=True,
            note_quality_gate="repair",
            note_compile=False,
        )
        with mock.patch("gamas_bot.notes.complete_json", new=fake_complete_json):
            notes = await structure_document(source, settings, object())
        self.assertTrue(notes.has_content)
        self.assertIn("BOOKLET_REPAIR", [call["task"] for call in calls])
        # The material carries a file number, so every stage stays UNKNOWN: the
        # community OpenRouter route is never eligible, not even for the repair.
        self.assertTrue(
            all(call["data_sensitivity"] == SENSITIVITY_UNKNOWN for call in calls),
            calls,
        )

    async def test_clean_lecture_is_explicitly_classified_for_every_stage(self):
        source = CLEAN_LECTURE
        calls: list[dict] = []

        async def fake_complete_json(_session, _settings, **kwargs):
            calls.append(kwargs)
            return note_payload(CLEAN_LECTURE)

        settings = make_settings(
            note_strict_grounding=True,
            note_quality_gate="repair",
            note_compile=False,
        )
        with mock.patch("gamas_bot.notes.complete_json", new=fake_complete_json):
            await structure_document(source, settings, object())
        self.assertTrue(calls)
        self.assertTrue(
            all(
                call["data_sensitivity"] == SENSITIVITY_STANDARD_EDUCATIONAL
                for call in calls
            ),
            calls,
        )


class FinalAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_booklet_that_keeps_an_unsupported_fact_is_rejected(self):
        source = "دوز شروع ۵۰۰ میلی‌گرم است و بیمار آن را با غذا مصرف می‌کند."
        fabricated = note_payload(
            "دوز شروع ۵۰۰ میلی‌گرم است و بیمار باید aspirin مصرف کند."
        )

        async def fake_complete_json(_session, _settings, **kwargs):
            return fabricated

        settings = make_settings(
            note_strict_grounding=True, note_quality_gate="repair", note_compile=False
        )
        with mock.patch("gamas_bot.notes.complete_json", new=fake_complete_json):
            with self.assertRaises(NoteQualityError) as ctx:
                await structure_document(source, settings, object())
        self.assertEqual(ctx.exception.reason, "unsupported_facts")
        self.assertGreaterEqual(ctx.exception.fabricated, 1)
        # The caller's contract is unchanged: this is a StructuringError, so the
        # transcript is kept and the job is reported as partial, never as a
        # delivered booklet.
        self.assertIsInstance(ctx.exception, StructuringError)

    async def test_warn_mode_observes_without_rejecting(self):
        source = "دوز شروع ۵۰۰ میلی‌گرم است."
        fabricated = note_payload("دوز شروع ۵۰۰ میلی‌گرم است و aspirin مصرف شود.")

        async def fake_complete_json(_session, _settings, **kwargs):
            return fabricated

        settings = make_settings(
            note_strict_grounding=True, note_quality_gate="warn", note_compile=False
        )
        with mock.patch("gamas_bot.notes.complete_json", new=fake_complete_json):
            notes = await structure_document(source, settings, object())
        self.assertTrue(notes.has_content)

    async def test_repair_attempts_are_bounded(self):
        source = (
            "دوز ۵۰۰ میلی‌گرم است. نسبت ۸.۵ درصد گزارش شد. مدت ۳۰ دقیقه بود."
        )
        generation = note_payload("دوز ۵۰۰ میلی‌گرم است.")
        repairs = [
            note_payload("دوز ۵۰۰ میلی‌گرم است. نسبت ۸.۵ درصد گزارش شد."),
            note_payload(
                "دوز ۵۰۰ میلی‌گرم است. نسبت ۸.۵ درصد گزارش شد. مدت ۳۰ دقیقه بود."
            ),
        ]
        repair_calls = 0

        async def fake_complete_json(_session, _settings, **kwargs):
            nonlocal repair_calls
            if kwargs.get("task") == "BOOKLET_REPAIR":
                payload = repairs[min(repair_calls, len(repairs) - 1)]
                repair_calls += 1
                return payload
            return generation

        settings = make_settings(
            note_strict_grounding=True, note_quality_gate="repair", note_compile=False
        )
        with mock.patch("gamas_bot.notes.complete_json", new=fake_complete_json):
            notes = await structure_document(source, settings, object())
        self.assertTrue(notes.has_content)
        self.assertLessEqual(repair_calls, MAX_NOTE_REPAIR_ATTEMPTS)
        self.assertEqual(repair_calls, MAX_NOTE_REPAIR_ATTEMPTS)


class JobStageReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_note_stages_become_real_job_states(self):
        job = Job(submission_id=3, user_id=7, kind="audio", filename="a.mp3")
        job.transition_to("generating_notes")
        progress = mock.AsyncMock()
        stage = JobPipelineMixin._note_stage_callback(object(), job, progress)

        await stage("validating")
        self.assertEqual(job.state, "validating_notes")
        progress.update.assert_awaited_with(88, mock.ANY)

        await stage("compiling")
        self.assertEqual(job.state, "compiling_notes")
        progress.update.assert_awaited_with(91, mock.ANY)

        await stage("unknown-stage")
        self.assertEqual(job.state, "compiling_notes")


class _Response:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status = status
        self._payload = payload or {}
        self.headers: dict[str, str] = {}

    async def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    async def json(self, content_type=None) -> dict:
        return self._payload


class _AsyncCM:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def __aenter__(self) -> _Response:
        return self.response

    async def __aexit__(self, *_exc) -> bool:
        return False


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _AsyncCM(self.responses.pop(0))


class ProviderDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    HOSTS = {
        "api.avalai.ir": "avalai",
        "generativelanguage.googleapis.com": "gemini",
        "openrouter.ai": "openrouter",
    }

    def settings(self, **overrides):
        values = {
            "note_providers": ("avalai", "gemini", "openrouter"),
            "avalai_api_key": "av-test",
            "gemini_api_key": "g-test",
            "openrouter_api_key": "or-test",
        }
        values.update(overrides)
        return make_settings(**values)

    async def test_route_policy_log_explains_exclusions(self):
        answer = _Response(
            200,
            {
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}
                ]
            },
        )
        session = _Session([answer])
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            await llm.complete(
                session,
                self.settings(),
                system="S",
                user="متن درس",
                task="BOOKLET_GENERATION",
                data_sensitivity=SENSITIVITY_UNKNOWN,
            )
        policy = "\n".join(line for line in logs.output if "LLM route policy" in line)
        self.assertIn("avalai:eligible", policy)
        self.assertIn("openrouter:blocked_by_unknown", policy)
        # No transcript text or credential reaches the diagnostic log line.
        self.assertNotIn("متن درس", policy)
        self.assertNotIn("av-test", policy)

    async def test_provider_failure_log_keeps_status_and_no_user_content(self):
        rejection = _Response(
            401, {"error": {"message": "invalid api key", "status": "UNAUTHENTICATED"}}
        )
        answer = _Response(
            200,
            {
                "candidates": [
                    {"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}
                ]
            },
        )
        session = _Session([rejection, answer])
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            result = await llm.complete(
                session,
                self.settings(),
                system="S",
                user="SECRET-LECTURE-TEXT",
                task="BOOKLET_GENERATION",
                data_sensitivity=SENSITIVITY_UNKNOWN,
            )
        self.assertEqual(result, "ok")
        captured = "\n".join(logs.output)
        failure_lines = [
            line for line in logs.output if "note provider failed" in line
        ]
        self.assertTrue(failure_lines)
        self.assertIn("detail=", failure_lines[0])
        self.assertIn("HTTP 401", failure_lines[0])
        self.assertIn("avalai", failure_lines[0])
        self.assertNotIn("SECRET-LECTURE-TEXT", captured)
        self.assertNotIn("av-test", captured)

    async def test_unknown_material_exhausts_only_the_eligible_chain(self):
        """With UNKNOWN material OpenRouter is not even attempted."""

        def rejection(status: int):
            return _Response(status, {"error": {"message": "synthetic failure"}})

        session = _Session([rejection(401), rejection(401)])
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            with self.assertRaises(llm.LLMAuthError):
                await llm.complete(
                    session,
                    self.settings(),
                    system="S",
                    user="متن",
                    task="BOOKLET_GENERATION",
                    data_sensitivity=SENSITIVITY_UNKNOWN,
                )
        attempted = [self.HOSTS[urlsplit(call["url"]).hostname] for call in session.calls]
        self.assertEqual(attempted, ["avalai", "gemini"])
        self.assertIn("openrouter:blocked_by_unknown", "\n".join(logs.output))

    async def test_standard_educational_material_keeps_all_three_routes(self):
        def rejection(status: int):
            return _Response(status, {"error": {"message": "synthetic failure"}})

        session = _Session([rejection(401), rejection(401), rejection(401)])
        with self.assertLogs("gamas_bot.llm", level="INFO"):
            with self.assertRaises(llm.LLMAuthError):
                await llm.complete(
                    session,
                    self.settings(),
                    system="S",
                    user="متن",
                    task="BOOKLET_GENERATION",
                    data_sensitivity=SENSITIVITY_STANDARD_EDUCATIONAL,
                )
        attempted = [self.HOSTS[urlsplit(call["url"]).hostname] for call in session.calls]
        self.assertEqual(attempted, ["avalai", "gemini", "openrouter"])


if __name__ == "__main__":
    unittest.main()
