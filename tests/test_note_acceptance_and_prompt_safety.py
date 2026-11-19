"""Regressions for prompt safety, bounded repair and final acceptance.

Everything here is deterministic and offline: ``complete_json`` is either
mocked or served by a scripted HTTP double, so no provider quota is spent.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from gamas_bot import llm
from gamas_bot.bot_jobs import JobPipelineMixin
from gamas_bot.jobs import Job
from gamas_bot.notes import MAX_NOTE_REPAIR_ATTEMPTS, structure_document
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


class FinalAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unrepairable_unsupported_fact_is_removed_and_delivered_degraded(self):
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
            notes = await structure_document(source, settings, object())
        self.assertEqual(notes.quality_state, "RECOVERED")
        self.assertNotIn("aspirin", notes.to_json().lower())
        self.assertIn("۵۰۰", notes.to_json())

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
        statements = ["دوز ۵۰۰ میلی‌گرم است.", "نسبت ۸.۵ درصد گزارش شد.", "مدت ۳۰ دقیقه بود."]
        generation = note_payload(statements[0])
        repairs = [note_payload(statements[0]), note_payload(statements[0])]
        # An accepted coverage repair appends missing facts; the already-safe
        # paragraph remains independently present in each response.
        for count, repair in enumerate(repairs, 2):
            repair["sections"][0]["paragraphs"] = statements[:count]
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
        self.assertEqual(repair_calls, 1)  # multiple issues on one node never trigger a second edit


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


class PromptRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_and_identifiable_educational_source_is_not_rejected_or_masked(self):
        source = (
            "درس فرضی: Patient ID 1234567890، نام Dr. Ahmadi و "
            "نشانی patient@example.org در مثال آموزشی ذکر شده است."
        )
        answer = _Response(
            200,
            {
                "choices": [
                    {
                        "message": {"content": json.dumps(note_payload(source), ensure_ascii=False)},
                        "finish_reason": "stop",
                    }
                ]
            },
        )
        settings = make_settings(
            note_providers=("avalai",),
            avalai_api_key="av-test",
            note_compile=False,
        )
        session = _Session([answer])
        notes = await structure_document(source, settings, session)
        self.assertTrue(notes.has_content)
        self.assertEqual(len(session.calls), 1)
        prompt = session.calls[0]["json"]["messages"][-1]["content"]
        for source_fragment in (
            "Patient ID 1234567890",
            "Dr. Ahmadi",
            "patient@example.org",
        ):
            with self.subTest(fragment=source_fragment):
                self.assertIn(source_fragment, prompt)
        self.assertNotIn("[REDACTED]", prompt)

    async def test_normalized_mixed_educational_prompt_masks_only_fake_credentials(self):
        from gamas_bot.document_model import NormalizedDocument

        fake_key = "sk-or-v1-abcdefghijklmno123456789"
        fake_token = "123456:AAEAAabcdeFGHIJKLMNopqrs12345"
        educational = (
            "Synthetic case: Patient ID 765432109, student@example.invalid, Dr. Example.",
            "تاریخ ۱۴۰۵/۰۳/۱۲، کمیت ۱۲٫۵ mg و پیوند‌واژه در تمرین حفظ می‌شوند.",
            "Educational labels: anonymous survey, privacy, medical history, employment law.",
        )
        raw = "\ufeff" + "\r\n".join((*educational, f"API_KEY={fake_key}", f"Telegram token {fake_token}"))
        source = NormalizedDocument.from_source_text(raw, source_type="txt")
        answer = _Response(200, {"choices": [{
            "message": {"content": json.dumps(note_payload(educational[1]), ensure_ascii=False)},
            "finish_reason": "stop",
        }]})
        settings = make_settings(note_providers=("avalai",), avalai_api_key="av-test", note_compile=False)
        session = _Session([answer])
        notes = await structure_document(source, settings, session)
        self.assertEqual(len(session.calls), 1)
        prompt = session.calls[0]["json"]["messages"][-1]["content"]
        self.assertIn("\n".join(educational), prompt)
        self.assertNotIn(fake_key, prompt)
        self.assertNotIn(fake_token, prompt)
        self.assertGreaterEqual(prompt.count("[REDACTED]"), 2)
        self.assertIn(fake_key, source.text)  # redaction is transport-only
        self.assertIn(fake_token, source.text)
        self.assertEqual(notes.input_ranges[-1].end_offset, len(source.text))


class PromptSecretProtectionTests(unittest.TestCase):
    def test_credentials_are_removed_from_prompt_but_educational_text_survives(self):
        provider_secrets = {
            "avalai": "configured-avalai-secret-123",
            "gemini": "configured-gemini-secret-456",
            "openrouter": "configured-openrouter-secret-789",
        }
        telegram_token = "123456:AAEAAabcdeFGHIJKLMNopqrs12345"
        api_hash = "configured-telegram-api-hash"
        environment_secret = "custom-environment-secret-value"
        settings = make_settings(
            note_providers=("avalai",),
            avalai_api_key=provider_secrets["avalai"],
            gemini_api_key=provider_secrets["gemini"],
            openrouter_api_key=provider_secrets["openrouter"],
            bot_token=telegram_token,
            api_hash=api_hash,
        )
        user = "\n".join(
            (
                "API_KEY=generic-api-secret-123",
                "PASSWORD=hunter2",
                "Authorization: Bearer provider-token-abcdefghi",
                "OpenRouter token sk-or-v1-abcdefghijklmnopqrstu",
                f"Telegram token {telegram_token}",
                f"Inactive provider keys: {provider_secrets['gemini']} {provider_secrets['openrouter']}",
                f"Telegram hash: {api_hash}",
                f"Other environment value: {environment_secret}",
                "DATABASE_URL=postgres://service:db-secret@example.invalid/app",
                "Educational example: Patient ID 1234567890, Dr. Ahmadi, patient@example.org.",
            )
        )
        route = llm.note_routes(settings)[0]
        with mock.patch.dict("os.environ", {"CUSTOM_AUTH_TOKEN": environment_secret}):
            _url, headers, payload = llm._build_request_for(
                route,
                settings,
                system=f"Use the configured key {provider_secrets['avalai']} only for authentication.",
                user=user,
            )

        prompt = json.dumps(payload, ensure_ascii=False)
        for secret in (
            *provider_secrets.values(),
            telegram_token,
            api_hash,
            environment_secret,
            "generic-api-secret-123",
            "hunter2",
            "provider-token-abcdefghi",
            "sk-or-v1-abcdefghijklmnopqrstu",
            "db-secret",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, prompt)
        self.assertIn("Patient ID 1234567890", prompt)
        self.assertIn("Dr. Ahmadi", prompt)
        self.assertIn("patient@example.org", prompt)
        self.assertEqual(headers["Authorization"], f"Bearer {provider_secrets['avalai']}")


class ProviderDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
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
                make_settings(
                    note_providers=("avalai", "gemini"),
                    avalai_api_key="av-test",
                    gemini_api_key="g-test",
                ),
                system="S",
                user="SECRET-LECTURE-TEXT",
                task="BOOKLET_GENERATION",
            )
        self.assertEqual(result, "ok")
        captured = "\n".join(logs.output)
        failure_lines = [line for line in logs.output if "note provider failed" in line]
        self.assertTrue(failure_lines)
        self.assertIn("detail=", failure_lines[0])
        self.assertIn("HTTP 401", failure_lines[0])
        self.assertIn("avalai", failure_lines[0])
        self.assertNotIn("SECRET-LECTURE-TEXT", captured)
        self.assertNotIn("av-test", captured)


class ProseClaimGateTests(unittest.IsolatedAsyncioTestCase):
    SOURCE = (
        "در این جلسه دربارهٔ فیزیولوژی کلیه صحبت کردیم. "
        "نفرون واحد عملکردی کلیه است و آب و نمک خون را تنظیم می‌کند."
    )

    async def test_an_invented_persian_claim_is_removed_without_losing_valid_content(self):
        payload = {
            "title": "کلیه",
            "sections": [
                {
                    "heading": "فیزیولوژی",
                    "paragraphs": [
                        "نفرون واحد عملکردی کلیه است و آب خون را تنظیم می‌کند.",
                        "کلیه‌ها با ترشح هورمون اریتروپویتین تولید گلبول قرمز را "
                        "افزایش می‌دهند.",
                    ],
                }
            ],
        }

        async def fake_complete_json(_session, _settings, **_kwargs):
            return payload

        settings = make_settings(
            note_strict_grounding=True, note_quality_gate="repair", note_compile=False
        )
        with mock.patch("gamas_bot.notes.complete_json", new=fake_complete_json):
            notes = await structure_document(self.SOURCE, settings, object())
        # Retain the verified portion of the affected section.
        self.assertEqual(notes.quality_state, "DEGRADED")
        body = " ".join(notes.sections[0].paragraphs)
        self.assertIn("نفرون", body)
        self.assertNotIn("اریتروپویتین", body)


class CompleteBookletGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_lost_number_is_reported_without_replacing_a_safe_draft(self):
        source = "دوز ۵۰۰ میلی‌گرم است و نسبت ۸.۵ درصد گزارش شد."
        payload = {
            "title": "دوز",
            "sections": [{"heading": "دوز", "paragraphs": ["دوز دارو بررسی شد."]}],
        }

        async def fake_complete_json(_session, _settings, **_kwargs):
            return payload

        reasons: list[str] = []

        async def on_incomplete(reason: str) -> None:
            reasons.append(reason)

        settings = make_settings(
            note_strict_grounding=True, note_quality_gate="repair", note_compile=False
        )
        with mock.patch("gamas_bot.notes.complete_json", new=fake_complete_json):
            notes = await structure_document(
                source, settings, object(), on_incomplete=on_incomplete
            )
        self.assertTrue(notes.has_content)
        self.assertEqual(reasons, ["missing_numbers"])
        self.assertEqual(notes.quality_state, "DEGRADED")
        self.assertFalse(notes.verification_report.passed)
        self.assertIn("دوز دارو بررسی شد.", notes.to_markdown())


class CompileStageNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_compile_is_not_reported_as_a_running_stage(self):
        stages: list[str] = []

        async def on_stage(name: str) -> None:
            stages.append(name)

        async def fake_complete_json(_session, _settings, **_kwargs):
            return note_payload(CLEAN_LECTURE)

        # Two chunks so the code actually reaches the compile decision.
        settings = make_settings(
            note_strict_grounding=False,
            note_compile=False,
            note_chunk_chars=300,
        )
        source = (CLEAN_LECTURE + " ") * 12
        with mock.patch("gamas_bot.notes.complete_json", new=fake_complete_json):
            await structure_document(source, settings, object(), on_stage=on_stage)
        self.assertNotIn("compiling", stages)


class ProviderEchoRedactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_provider_body_that_echoes_the_lecture_is_not_logged(self):
        rejection = _Response(
            400, {"error": {"message": "invalid request: نفرون واحد عملکردی کلیه"}}
        )
        answer = _Response(
            200,
            {
                "candidates": [
                    {"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}
                ]
            },
        )
        settings = make_settings(
            note_providers=("avalai", "gemini"),
            avalai_api_key="av-test",
            gemini_api_key="g-test",
        )
        session = _Session([rejection, answer])
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            result = await llm.complete(
                session, settings, system="S", user="نفرون واحد عملکردی کلیه",
                task="BOOKLET_GENERATION",
            )
        self.assertEqual(result, "ok")
        captured = "\n".join(logs.output)
        self.assertIn("HTTP 400", captured)
        self.assertNotRegex(captured, r"[\u0600-\u06FF]")


if __name__ == "__main__":
    unittest.main()
