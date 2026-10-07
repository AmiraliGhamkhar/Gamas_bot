"""Regression coverage for the accuracy-first transcription/notes hardening.

Covers: Gemini HTTP 400 diagnostics (sanitized, actionable, no keys/content),
OpenAI-compatible note and STT endpoints, STT provider switching/fallback and
upload-size routing, long-transcript chunk ordering and completeness, and
raw-transcript preservation when note generation fails.
"""

from __future__ import annotations


import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gamas_bot.bot import StudyBot
from gamas_bot.config import Settings
from gamas_bot.stt import STTError, Transcript, speechmatics_config, transcribe
from gamas_bot.structuring import (
    StructuringError,
    _extract_error_detail,
    _provider_request,
    _provider_response,
    split_transcript,
    structure_transcript,
)
from scripts.benchmark_stt import normalize_words

from support import FakeJobEvent, docx_text, make_settings, sample_notes_json, wav_bytes


class _FakeResponse:
    """Minimal aiohttp response double: status, headers, JSON/raw body."""

    def __init__(self, status: int, payload=None, raw: bytes | None = None, headers=None):
        self.status = status
        self.headers = headers or {}
        self._payload = payload
        self._raw = raw
        self.reads = 0
        self.json_calls = 0

    async def read(self):
        self.reads += 1
        if self._raw is not None:
            return self._raw
        return json.dumps(self._payload).encode("utf-8")

    async def json(self, content_type=None):
        self.json_calls += 1
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Minimal aiohttp session double used through the module's own factory."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, params=None, json=None, data=None):
        self.calls.append({"url": url, "payload": json, "params": params, "headers": headers})
        return self.responses.pop(0)


def _gemini_400_payload():
    return {
        "error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "API key not valid. Please pass a valid API key.",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "API_KEY_INVALID",
                    "domain": "googleapis.com",
                }
            ],
        }
    }


class GeminiHttp400Tests(unittest.IsolatedAsyncioTestCase):
    async def test_400_reports_sanitized_reason_and_is_not_retried(self):
        session = _FakeSession([_FakeResponse(400, _gemini_400_payload())])
        with patch(
            "gamas_bot.structuring.aiohttp.ClientSession", lambda **kwargs: session
        ), self.assertLogs("gamas_bot.structuring", level="ERROR") as logs:
            with self.assertRaises(StructuringError) as caught:
                await structure_transcript("متن درس", make_settings())
        message = str(caught.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("INVALID_ARGUMENT", message)
        self.assertIn("API_KEY_INVALID", message)
        # The config key and the transcript itself must never surface.
        self.assertNotIn("gemini-key", message)
        self.assertNotIn("gemini-key", "\n".join(logs.output))
        # HTTP 400 is a permanent configuration problem: retrying is noise.
        self.assertEqual(len(session.calls), 1)
        self.assertIn("API_KEY_INVALID", "\n".join(logs.output))

    async def test_error_detail_redacts_an_echoed_key(self):
        payload = {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "the credential gemini-key was rejected for gemini-key",
            }
        }
        session = _FakeSession([_FakeResponse(400, payload)])
        with patch(
            "gamas_bot.structuring.aiohttp.ClientSession", lambda **kwargs: session
        ), self.assertLogs("gamas_bot.structuring", level="ERROR") as logs:
            with self.assertRaises(StructuringError) as caught:
                await structure_transcript("متن", make_settings())
        self.assertNotIn("gemini-key", str(caught.exception))
        self.assertNotIn("gemini-key", "\n".join(logs.output))
        self.assertIn("***", str(caught.exception))

    async def test_non_json_error_body_is_never_logged_or_echoed(self):
        session = _FakeSession([
            _FakeResponse(400, raw="<html><body>SECRET-TRANSCRIPT</body></html>".encode())
        ])
        with patch(
            "gamas_bot.structuring.aiohttp.ClientSession", lambda **kwargs: session
        ), self.assertLogs("gamas_bot.structuring", level="ERROR") as logs:
            with self.assertRaises(StructuringError) as caught:
                await structure_transcript("متن", make_settings())
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertNotIn("SECRET-TRANSCRIPT", str(caught.exception))
        self.assertNotIn("SECRET-TRANSCRIPT", "\n".join(logs.output))

    async def test_openai_compatible_400_is_sanitized_and_not_retried(self):
        settings = make_settings(
            note_api_provider="openai_compatible",
            note_api_key="sk-test",
            note_api_base_url="https://api.example.test/v1",
            note_api_model="model",
        )
        payload = {
            "error": {
                "message": "Incorrect API key provided.",
                "type": "authentication_error",
                "code": "invalid_api_key",
            }
        }
        session = _FakeSession([_FakeResponse(401, payload)])
        with patch(
            "gamas_bot.structuring.aiohttp.ClientSession", lambda **kwargs: session
        ), self.assertLogs("gamas_bot.structuring", level="ERROR") as logs:
            with self.assertRaises(StructuringError) as caught:
                await structure_transcript("متن درس", settings)
        message = str(caught.exception)
        self.assertIn("authentication_error", message)
        self.assertIn("invalid_api_key", message)
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("sk-test", "\n".join(logs.output) + message)

    async def test_429_is_retried_and_then_succeeds(self):
        settings = make_settings(
            note_api_provider="openai_compatible",
            note_api_key="sk-test",
            note_api_base_url="https://api.example.test/v1",
            note_api_model="model",
        )
        ok = {"choices": [{"message": {"content": sample_notes_json()}}]}
        session = _FakeSession([
            _FakeResponse(429, {"error": {"message": "quota"}}, headers={"Retry-After": "0"}),
            _FakeResponse(200, ok),
        ])
        with patch(
            "gamas_bot.structuring.aiohttp.ClientSession", lambda **kwargs: session
        ), patch("gamas_bot.structuring.asyncio.sleep", new=AsyncMock()):
            result = await structure_transcript("متن درس", settings)
        self.assertEqual(result.title, "جزوهٔ آزمایشی")
        self.assertEqual(len(session.calls), 2)

    def test_error_detail_is_bounded_and_control_free(self):
        payload = {"error": {"message": "x\x01y\n" + "طولانی " * 200}}
        detail = _extract_error_detail(json.dumps(payload).encode(), None)
        self.assertLessEqual(len(detail), 180)
        self.assertNotIn("\x01", detail)
        self.assertNotIn("\n", detail)
        self.assertIsNone(_extract_error_detail(b"not-json", None))
        self.assertIsNone(_extract_error_detail(b"", None))
        self.assertIsNone(_extract_error_detail(b'{"no_error": true}', None))


class GeminiRequestShapeTests(unittest.TestCase):
    def test_models_prefix_is_normalized_not_double_encoded(self):
        settings = make_settings(note_api_model="models/gemini-2.5-flash-lite")
        url, _headers, _payload, _params = _provider_request("متن", settings, "دستور: ")
        self.assertEqual(
            url,
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash-lite:generateContent",
        )
        self.assertNotIn("%2F", url)
        self.assertEqual(url.count("/models/"), 1)

    def test_gemini_safety_block_reports_the_reason(self):
        with self.assertRaises(StructuringError) as caught:
            _provider_response({"promptFeedback": {"blockReason": "SAFETY"}}, "gemini")
        self.assertIn("SAFETY", str(caught.exception))
        with self.assertRaises(StructuringError):
            _provider_response({"candidates": []}, "gemini")


class STTRoutingExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compatible_stt_falls_back_after_native_failures(self):
        settings = make_settings(
            stt_openai_base_url="https://stt.example.test/v1",
            stt_openai_api_key="stt-key",
        )
        with tempfile.NamedTemporaryFile() as audio, patch(
            "gamas_bot.stt._speechmatics",
            new=AsyncMock(side_effect=STTError("خرابی اول")),
        ) as first, patch(
            "gamas_bot.stt._deepgram",
            new=AsyncMock(side_effect=STTError("خرابی دوم")),
        ) as second, patch(
            "gamas_bot.stt._openai_compatible_stt",
            new=AsyncMock(return_value=Transcript("openai_compatible", "متن نهایی", None)),
        ) as third:
            result = await transcribe(Path(audio.name), settings)
        self.assertEqual(result.engine, "openai_compatible")
        self.assertEqual(result.text, "متن نهایی")
        for mocked in (first, second, third):
            mocked.assert_awaited_once()

    async def test_oversized_file_skips_speechmatics_and_uses_deepgram(self):
        with tempfile.NamedTemporaryFile() as audio:
            audio.truncate(1_000_000_000)  # sparse: reports 1 GB without the bytes
            with patch(
                "gamas_bot.stt._speechmatics",
                new=AsyncMock(side_effect=AssertionError("must not be attempted")),
            ) as speechmatics, patch(
                "gamas_bot.stt._deepgram",
                new=AsyncMock(return_value=Transcript("deepgram", "متن", 0.9)),
            ) as deepgram:
                result = await transcribe(Path(audio.name), make_settings())
        speechmatics.assert_not_awaited()
        deepgram.assert_awaited_once()
        self.assertEqual(result.engine, "deepgram")

    async def test_file_larger_than_every_limit_fails_before_any_attempt(self):
        settings = make_settings(stt_openai_base_url="https://stt.example.test/v1")
        with tempfile.NamedTemporaryFile() as audio:
            audio.truncate(2_100_000_000)
            with patch(
                "gamas_bot.stt._speechmatics",
                new=AsyncMock(side_effect=AssertionError("must not be attempted")),
            ) as speechmatics, patch(
                "gamas_bot.stt._deepgram",
                new=AsyncMock(side_effect=AssertionError("must not be attempted")),
            ) as deepgram, patch(
                "gamas_bot.stt._openai_compatible_stt",
                new=AsyncMock(side_effect=AssertionError("must not be attempted")),
            ) as openai_stt, self.assertRaises(STTError) as caught:
                await transcribe(Path(audio.name), settings)
        self.assertIn("گیگابایت", str(caught.exception))
        speechmatics.assert_not_awaited()
        deepgram.assert_not_awaited()
        openai_stt.assert_not_awaited()

    async def test_no_configured_stt_engine_reports_configuration_missing(self):
        settings = make_settings(speechmatics_api_key=None, deepgram_api_key=None)
        with tempfile.NamedTemporaryFile() as audio:
            with self.assertRaises(STTError) as caught:
                await transcribe(Path(audio.name), settings)
        self.assertIn("کلید یا نشانی", str(caught.exception))

    async def test_primary_switch_routes_the_first_attempt(self):
        with tempfile.NamedTemporaryFile() as audio:
            with patch(
                "gamas_bot.stt._deepgram",
                new=AsyncMock(return_value=Transcript("deepgram", "متن", 0.9)),
            ) as deepgram, patch(
                "gamas_bot.stt._speechmatics",
                new=AsyncMock(side_effect=AssertionError("fallback not needed")),
            ):
                result = await transcribe(
                    Path(audio.name), make_settings(stt_primary="deepgram")
                )
        deepgram.assert_awaited_once()
        self.assertEqual(result.engine, "deepgram")


class STTMetricsLoggingTests(unittest.IsolatedAsyncioTestCase):
    """The extra STT logging must be metric-rich but content-free."""

    async def test_successful_attempt_logs_metrics_without_transcript_text(self):
        transcript_text = "این یک متن آزمایشی برای سنجش لاگ‌هاست"
        with tempfile.NamedTemporaryFile() as audio:
            with patch(
                "gamas_bot.stt._speechmatics",
                new=AsyncMock(return_value=Transcript("speechmatics", transcript_text, 0.87)),
            ), self.assertLogs("gamas_bot.stt", level="INFO") as logs:
                result = await transcribe(Path(audio.name), make_settings(stt_fallback_enabled=False))
        self.assertEqual(result.engine, "speechmatics")
        joined = "\n".join(logs.output)
        self.assertIn("STT job started", joined)
        self.assertIn("candidate_engines=['speechmatics']", joined)
        self.assertIn("STT attempt completed", joined)
        self.assertIn("confidence=0.870", joined)
        self.assertIn(f"text_chars={len(transcript_text)}", joined)
        self.assertIn(f"text_words={len(transcript_text.split())}", joined)
        self.assertIn("STT job finished", joined)
        self.assertIn("engine=speechmatics", joined)
        self.assertIn("total_elapsed_seconds=", joined)
        # Metrics only: the transcript itself must never be logged.
        self.assertNotIn(transcript_text, joined)

    async def test_failure_logs_sanitized_detail_and_low_confidence_logs_threshold(self):
        with tempfile.NamedTemporaryFile() as audio:
            with patch(
                "gamas_bot.stt._speechmatics",
                new=AsyncMock(return_value=Transcript("speechmatics", "متن کم‌اعتماد", 0.30)),
            ), patch(
                "gamas_bot.stt._deepgram",
                new=AsyncMock(side_effect=STTError("سرویس پاسخ نداد")),
            ), self.assertLogs("gamas_bot.stt", level="WARNING") as logs:
                result = await transcribe(Path(audio.name), make_settings())
        self.assertEqual(result.engine, "speechmatics")
        joined = "\n".join(logs.output)
        self.assertIn("Low STT confidence", joined)
        self.assertIn("threshold 0.650", joined)
        self.assertIn("STT provider failed", joined)
        self.assertIn("provider=deepgram", joined)
        self.assertIn("detail=سرویس پاسخ نداد", joined)
        self.assertIn("error_type=STTError", joined)

    async def test_speechmatics_polling_and_job_lifecycle_are_logged(self):
        from gamas_bot.stt import _speechmatics

        polls = {"count": 0, "transcript_downloads": 0}

        class FakeResponse:
            def __init__(self, status, payload):
                self.status = status
                self._payload = payload

            async def json(self, content_type=None):
                return self._payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeSession:
            def post(self, url, **_kwargs):
                # Job submission: the first poll flips the job to "done".
                return FakeResponse(200, {"id": "job-123", "job": {"status": "running"}})

            def get(self, url, **_kwargs):
                if "/transcript" in url:
                    polls["transcript_downloads"] += 1
                    return FakeResponse(
                        200,
                        {
                            "results": [
                                {
                                    "type": "word",
                                    "alternatives": [{"content": "سلام", "confidence": 0.9}],
                                }
                            ]
                        },
                    )
                polls["count"] += 1
                status = "done" if polls["count"] >= 1 else "running"
                return FakeResponse(200, {"job": {"status": status}})

        settings = make_settings(stt_poll_interval=0.01)
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio, self.assertLogs(
            "gamas_bot.stt", level="DEBUG"
        ) as logs:
            transcript = await _speechmatics(FakeSession(), Path(audio.name), settings)
        self.assertEqual(transcript.text, "سلام")
        self.assertEqual(polls["transcript_downloads"], 1)
        joined = "\n".join(logs.output)
        self.assertIn("Speechmatics job submitted", joined)
        self.assertIn("job_id=job-123", joined)
        self.assertIn("upload_elapsed_seconds=", joined)
        self.assertIn("Speechmatics job poll", joined)
        self.assertIn("status=done", joined)
        self.assertIn("Speechmatics job finished", joined)
        self.assertIn("Speechmatics transcript downloaded", joined)
        self.assertIn("result_items=", joined)


class _SessionCtx:
    def __init__(self, inner):
        self._inner = inner

    async def __aenter__(self):
        return self._inner

    async def __aexit__(self, *exc):
        return False


class OpenAIStTShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_targets_audio_transcriptions_with_bearer(self):
        from gamas_bot.stt import _openai_compatible_stt

        captured: dict = {}
        response = _FakeResponse(200, {"text": "سلام دنیا"})

        def post(url, headers=None, params=None, data=None):
            captured.update({"url": url, "headers": headers})
            return response

        session = SimpleNamespace(post=post)
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            result = await _openai_compatible_stt(
                session,
                Path(audio.name),
                make_settings(
                    stt_openai_base_url="https://stt.example.test/v1",
                    stt_openai_api_key="stt-key",
                    stt_openai_model="whisper-large-v3",
                ),
            )
        self.assertEqual(captured["url"], "https://stt.example.test/v1/audio/transcriptions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer stt-key")
        self.assertEqual(result.engine, "openai_compatible")
        self.assertEqual(result.text, "سلام دنیا")
        self.assertIsNone(result.confidence)

    async def test_error_body_is_not_read_before_raising(self):
        from gamas_bot.stt import _openai_compatible_stt

        response = _FakeResponse(400, {"error": {"message": "SECRET body"}})
        session = SimpleNamespace(post=lambda *args, **kwargs: response)
        with tempfile.NamedTemporaryFile() as audio:
            with self.assertRaises(STTError) as caught:
                await _openai_compatible_stt(
                    session,
                    Path(audio.name),
                    make_settings(stt_openai_base_url="https://stt.example.test/v1"),
                )
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertEqual(response.reads, 0)
        self.assertEqual(response.json_calls, 0)

    async def test_empty_text_is_an_error(self):
        from gamas_bot.stt import _openai_compatible_stt

        session = SimpleNamespace(post=lambda *args, **kwargs: _FakeResponse(200, {"text": " "}))
        with tempfile.NamedTemporaryFile() as audio:
            with self.assertRaises(STTError):
                await _openai_compatible_stt(
                    session,
                    Path(audio.name),
                    make_settings(stt_openai_base_url="https://stt.example.test/v1"),
                )


class SpeechmaticsAccuracyConfigTests(unittest.TestCase):
    def test_default_config_requests_the_high_accuracy_model(self):
        config = speechmatics_config(make_settings())
        self.assertEqual(config["transcription_config"]["model"], "enhanced")
        self.assertEqual(config["transcription_config"]["language"], "fa")
        self.assertNotIn("additional_vocab", config["transcription_config"])

    def test_custom_vocabulary_is_sent_verbatim(self):
        settings = make_settings(speechmatics_additional_vocab=("Metformin", "MRI", "HbA1c"))
        config = speechmatics_config(settings)
        self.assertEqual(
            config["transcription_config"]["additional_vocab"],
            [{"content": "Metformin"}, {"content": "MRI"}, {"content": "HbA1c"}],
        )


class ChunkOrderAndCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_are_structured_and_joined_in_chronological_order(self):
        chunks = [f"جملهٔ {index} از درس." for index in range(1, 6)]
        seen: list[str] = []

        async def fake_chunk(chunk, settings, session, prompt=None, **_kwargs):
            seen.append(chunk)
            body = chunk.split("]\n\n", 1)[-1]  # drop the positional prefix
            return json.dumps(
                {
                    "title": "جزوه",
                    "sections": [
                        {"heading": f"بخش {body.split()[1]}", "paragraphs": [body]}
                    ],
                },
                ensure_ascii=False,
            )

        with patch(
            "gamas_bot.structuring.split_transcript", return_value=chunks
        ), patch(
            "gamas_bot.structuring._structure_chunk", side_effect=fake_chunk
        ), patch(
            "gamas_bot.structuring.aiohttp.ClientSession", lambda **kwargs: _FakeSession([])
        ):
            # Chunk order is what this test pins, so the two global-context
            # passes (orientation + final compilation) are switched off; they
            # are covered by tests/test_global_compilation.py.
            result = await structure_transcript(
                "متن طولانی", make_settings(note_global_context_enabled=False)
            )
        # Chunks carry a positional context prefix; the transcript itself must
        # still arrive in order, exactly once each.
        prefix = "[بخش ۱ از ۵ این درس — ادامهٔ درس در بخش بعدی می‌آید]\n\n"
        self.assertEqual(seen[0], prefix + chunks[0])
        self.assertTrue(all(chunk in item for chunk, item in zip(chunks, seen, strict=True)))
        self.assertEqual(
            [section.heading for section in result.sections],
            [f"بخش {index}" for index in range(1, 6)],
        )
        self.assertEqual(
            [section.paragraphs[0].split("]\n\n", 1)[-1] for section in result.sections],
            chunks,
        )

    def test_mixed_medical_text_survives_chunking_complete_and_ordered(self):
        sentence = (
            "بیمار مبتلا به دیابت نوع دوم با Metformin 500 mg هر هشت ساعت شروع شد، "
            "دوز Insulin برابر ۱۰ واحد تزریق شد و HbA1c برابر 7.2٪ گزارش شد؛ MRI طبیعی بود. "
        )
        text = (sentence * 400).strip()
        pieces = split_transcript(text)
        self.assertGreater(len(pieces), 1)
        # Token-by-token equality proves completeness and chronological order.
        self.assertEqual(" ".join(pieces).split(), text.split())
        joined = " ".join(pieces)
        self.assertEqual(joined.count("Metformin"), 400)
        self.assertEqual(joined.count("Insulin"), 400)
        self.assertEqual(joined.count("500 mg"), 400)
        self.assertEqual(joined.count("HbA1c"), 400)

    def test_persian_normalization_preserves_terms_numbers_and_drug_names(self):
        words = normalize_words("Metformin دوز ۵۰۰ میلی‌گرم؛ HbA1c برابر 7.2٪")
        self.assertIn("Metformin", words)
        self.assertIn("HbA1c", words)
        self.assertIn("۵۰۰", words)
        self.assertIn("میلی‌گرم", words)
        self.assertIn("دوز", words)


class SettingsExtensionTests(unittest.TestCase):
    def _from_env(self, body: str) -> Settings:
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text(body, encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                return Settings.from_env(env)

    def test_speechmatics_model_accepts_documented_batch_models_only(self):
        base = (
            "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
            "DEEPGRAM_API_KEY=d\n"
        )
        settings = self._from_env(base + "SPEECHMATICS_MODEL=standard\n")
        self.assertEqual(settings.speechmatics_operating_point, "standard")
        # The legacy name keeps working and stays in sync with the new one.
        self.assertEqual(settings.speechmatics_model, "standard")
        self.assertEqual(self._from_env(base).speechmatics_operating_point, "enhanced")
        # The multilingual batch models are documented values too.
        for value in ("melia-1", "oak-1"):
            with self.subTest(value=value):
                self.assertEqual(
                    self._from_env(
                        base + f"SPEECHMATICS_OPERATING_POINT={value}\n"
                    ).speechmatics_operating_point,
                    value,
                )
        with self.assertRaises(ValueError):
            self._from_env(base + "SPEECHMATICS_OPERATING_POINT=ultra\n")

    def test_additional_vocab_is_split_and_deduplicated(self):
        settings = self._from_env(
            "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\nDEEPGRAM_API_KEY=d\n"
            "SPEECHMATICS_ADDITIONAL_VOCAB=Metformin، MRI,Metformin؛ HbA1c\n"
        )
        self.assertEqual(settings.speechmatics_additional_vocab, ("Metformin", "MRI", "HbA1c"))

    def test_vocab_limit_is_enforced(self):
        terms = ",".join(f"term{i}" for i in range(20_001))
        with self.assertRaises(ValueError):
            self._from_env(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
                f"DEEPGRAM_API_KEY=d\nSPEECHMATICS_ADDITIONAL_VOCAB={terms}\n"
            )

    def test_openai_stt_is_a_selectable_primary_and_satisfies_runtime_checks(self):
        settings = self._from_env(
            "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
            "STT_PRIMARY=openai_compatible\nSTT_OPENAI_BASE_URL=http://127.0.0.1:8080/v1\n"
            "STT_OPENAI_MODEL=whisper-large-v3\n"
        )
        self.assertEqual(settings.stt_primary, "openai_compatible")
        self.assertEqual(settings.stt_openai_model, "whisper-large-v3")
        settings.validate_runtime()  # one STT endpoint is enough, no key required
        with self.assertRaises(ValueError):
            self._from_env(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_API_ID=1\nTELEGRAM_API_HASH=h\n"
                "STT_OPENAI_BASE_URL=ftp://invalid/v1\n"
            )

    def test_runtime_still_requires_one_stt_engine(self):
        settings = make_settings(speechmatics_api_key=None, deepgram_api_key=None)
        with self.assertRaises(ValueError):
            settings.validate_runtime()


class TranscriptPreservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_failure_still_delivers_and_stores_the_raw_transcript(self):
        full_text = (
            "درس فارماکولوژی: Metformin 500 mg هر هشت ساعت، دوز ۲۵ واحد Insulin "
            "و پایش HbA1c هر سه ماه توصیه شد."
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = make_settings(
                database_path=root / "bot.sqlite3",
                session_path=root / "session",
                temp_dir=root / "tmp",
            )
            bot = StudyBot(settings)
            await bot.db.open()
            try:
                source = root / "class.mp3"
                source.write_bytes(wav_bytes(1.0))
                user = await bot.db.upsert_user(101, "student")
                submission_id = await bot.db.create_submission(
                    user["id"], "f1", None, "class.mp3", None, source_type="audio"
                )
                event = FakeJobEvent(source)
                with patch(
                    "gamas_bot.bot.transcribe",
                    new=AsyncMock(return_value=Transcript("deepgram", full_text, 0.93)),
                ) as stt, patch(
                    "gamas_bot.bot.structure_transcript",
                    new=AsyncMock(
                        side_effect=StructuringError(
                            "سرویس gemini خطای HTTP 400 داد. (INVALID_ARGUMENT; API_KEY_INVALID)"
                        )
                    ),
                ):
                    await bot._process_submission(event, submission_id, "class.mp3", "audio")
                stt.assert_awaited_once()
                # The transcript is delivered as files (plain Word + raw text)
                # and the notice explains the degraded result.
                self.assertEqual(len(event.files), 2)
                self.assertIn(full_text, event.file_bytes(".txt").decode("utf-8"))
                self.assertIn(full_text, docx_text(event.file_bytes(".docx")))
                captions = "\n".join(caption for caption, _path in event.files)
                self.assertIn("نتوانستم متن را به شکل جزوهٔ ساختارمند دربیاورم", captions)
                # The successful transcript must remain available in storage too.
                async with bot.db._lock:
                    cursor = await bot.db._db().execute(
                        "SELECT raw_transcript, structured_text FROM transcriptions "
                        "WHERE submission_id=?",
                        (submission_id,),
                    )
                    raw, structured = (await cursor.fetchone())
                self.assertEqual(raw, full_text)
                self.assertIn(full_text, structured)
                stats = await bot.db.stats()
                self.assertEqual(stats["done"], 1)
                self.assertEqual(stats["failed"], 0)
            finally:
                await bot.db.close()


class BenchmarkProviderCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_openai_stt_is_benchmarked_alongside_native_engines(self):
        import csv

        from scripts.benchmark_stt import run

        sample = SimpleNamespace(
            name="short.wav",
            suffix=".wav",
            is_file=lambda: True,
            stat=lambda: SimpleNamespace(st_size=1_000),
        )
        sample.with_suffix = lambda suffix: SimpleNamespace(exists=lambda: False)
        settings = make_settings(stt_openai_base_url="https://stt.example.test/v1")

        async def fake_transcribe(_audio, run_settings):
            return Transcript(run_settings.stt_primary, "متن", 0.9)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "results.csv"
            with patch(
                "scripts.benchmark_stt.Settings.from_env", return_value=settings
            ), patch(
                "scripts.benchmark_stt.transcribe", side_effect=fake_transcribe
            ):
                await run(SimpleNamespace(audio=[sample], output=output))
            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(
            [(row["engine"], row["status"]) for row in rows],
            [("speechmatics", "ok"), ("deepgram", "ok"), ("openai_compatible", "ok")],
        )


if __name__ == "__main__":
    unittest.main()
