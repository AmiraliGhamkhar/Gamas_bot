"""Tests for the v3 speech-to-text layer: parsing, failover, cooldowns."""

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from gamas_bot import stt
from gamas_bot.stt import (
    PROVIDERS,
    STTAuthenticationError,
    STTConfigurationError,
    STTRequestError,
    STTTransientError,
    _deepgram_transcript,
    _normalize_language,
    _openai_transcript,
    _speechmatics_words,
    reset_cooldowns,
    transcribe,
)
from tests.support import make_settings


def audio_file(name: str = "x.wav") -> Path:
    """A real (tiny) file, because ``transcribe`` stats it."""
    import tempfile

    path = Path(tempfile.gettempdir()) / f"gamas-test-{name}"
    path.write_bytes(b"fake-audio")
    return path


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    async def read(self):
        return json.dumps(self._payload).encode()

    async def json(self, content_type=None):
        return self._payload


class _AsyncCM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return False


class FakeSession:
    """Scripted POST/GET responses; records every call."""

    def __init__(self, posts=None, gets=None):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.post_calls: list[str] = []
        self.get_calls: list[str] = []

    def post(self, url, **kwargs):
        self.post_calls.append(url)
        return _AsyncCM(self.posts.pop(0))

    def get(self, url, **kwargs):
        self.get_calls.append(url)
        self.get_kwargs = kwargs
        return _AsyncCM(self.gets.pop(0))


def speechmatics_flow(text_words):
    """Script the three Speechmatics calls: submit, poll (done), transcript."""
    return FakeSession(
        posts=[FakeResponse(202, {"id": "job-1"})],
        gets=[
            FakeResponse(200, {"job": {"status": "done"}}),
            FakeResponse(200, {"results": text_words}),
        ],
    )


class SpeechmaticsParsingTests(unittest.TestCase):
    def test_words_with_timings_and_punctuation_attachment(self):
        results = [
            {"type": "word", "start_time": 0.1, "end_time": 0.5,
             "alternatives": [{"content": "سلام"}]},
            {"type": "punctuation", "alternatives": [{"content": "،", "attaches_to": "previous"}]},
            {"type": "word", "start_time": 0.6, "end_time": 1.0,
             "alternatives": [{"content": "دوستان"}]},
            {"type": "punctuation", "alternatives": [{"content": ".", "attaches_to": "next"}]},
            {"type": "word", "start_time": 1.2, "end_time": 1.6,
             "alternatives": [{"content": "خوب"}]},
        ]
        text, words = _speechmatics_words(results)
        self.assertEqual(text, "سلام، دوستان .خوب")
        self.assertEqual(len(words), 3)
        self.assertEqual(words[0].text, "سلام،")
        self.assertEqual(words[0].start, 0.1)
        # attaches_to=next: the mark prefixes the following token
        self.assertEqual(words[1].text, "دوستان")
        self.assertEqual(words[2].text, ".خوب")

    def test_both_attachment_closes_previous_and_opens_next(self):
        results = [
            {"type": "word", "start_time": 0.0, "end_time": 0.4,
             "alternatives": [{"content": "اول"}]},
            {"type": "punctuation", "alternatives": [{"content": "-", "attaches_to": "both"}]},
            {"type": "word", "start_time": 0.5, "end_time": 0.9,
             "alternatives": [{"content": "دوم"}]},
        ]
        _text, words = _speechmatics_words(results)
        self.assertEqual(words[0].text, "اول-")
        self.assertEqual(words[1].text, "-دوم")

    def test_empty_results(self):
        self.assertEqual(_speechmatics_words([]), ("", []))


class SpeechmaticsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end(self):
        settings = make_settings(stt_providers=("speechmatics",))
        results = [
            {"type": "word", "start_time": 0.0, "end_time": 0.5,
             "alternatives": [{"content": "درس"}]},
            {"type": "punctuation", "alternatives": [{"content": ".", "attaches_to": "previous"}]},
        ]
        session = speechmatics_flow(results)
        raw = await stt._speechmatics(session, audio_file(), settings)
        self.assertEqual(raw.text, "درس.")
        self.assertEqual(raw.engine, "speechmatics")
        self.assertEqual(raw.words[0].start, 0.0)
        # The transcript request asked for json-v2 (the timed format).
        self.assertEqual(session.get_kwargs.get("params"), {"format": "json-v2"})

    async def test_rejected_job_is_request_error(self):
        settings = make_settings(stt_providers=("speechmatics",))
        session = FakeSession(
            posts=[FakeResponse(202, {"id": "job-2"})],
            gets=[FakeResponse(200, {"job": {"status": "rejected"}})],
        )
        with self.assertRaises(STTRequestError):
            await stt._speechmatics(session, audio_file(), settings)

    async def test_auth_error_on_submit(self):
        settings = make_settings(stt_providers=("speechmatics",))
        session = FakeSession(posts=[FakeResponse(401, {"error": {"message": "bad key"}})])
        with self.assertRaises(STTAuthenticationError):
            await stt._speechmatics(session, audio_file(), settings)


class DeepgramParsingTests(unittest.TestCase):
    def test_words_with_punctuated_forms_and_confidence(self):
        payload = {
            "results": {"channels": [{"alternatives": [{
                "transcript": "سلام دوستان. امروز درس شبکه است.",
                "words": [
                    {"word": "سلام", "punctuated_word": "سلام", "start": 0.0, "end": 0.4},
                    {"word": "دوستان.", "punctuated_word": "دوستان.", "start": 0.5, "end": 1.0},
                    {"word": "امروز", "punctuated_word": "امروز", "start": 3.0, "end": 3.4},
                ],
                "confidence": 0.93,
            }]}]}
        }
        raw = _deepgram_transcript(payload)
        self.assertEqual(raw.engine, "deepgram")
        self.assertEqual(raw.confidence, 0.93)
        self.assertEqual(len(raw.words), 3)
        self.assertEqual(raw.words[1].text, "دوستان.")
        self.assertEqual(raw.words[2].start, 3.0)

    def test_malformed_payload_is_contract_error(self):
        with self.assertRaises(stt.STTError):
            _deepgram_transcript({"unexpected": True})

    def test_empty_transcript_is_error(self):
        payload = {"results": {"channels": [{"alternatives": [{"transcript": "", "words": []}]}]}}
        with self.assertRaises(stt.STTError):
            _deepgram_transcript(payload)


class OpenAICompatibleParsingTests(unittest.TestCase):
    def test_plain_text_shape(self):
        raw = _openai_transcript({"text": " سلام دنیا "})
        self.assertEqual(raw.text, "سلام دنیا")
        self.assertEqual(raw.words, [])

    def test_verbose_segments_shape(self):
        payload = {
            "text": "ignored when segments exist",
            "segments": [
                {"text": "بخش اول", "start": 0.0, "end": 2.5},
                {"text": "بخش دوم", "start": 8.0, "end": 10.0},
            ],
        }
        raw = _openai_transcript(payload)
        self.assertEqual(len(raw.words), 2)
        self.assertEqual(raw.words[0].text, "بخش اول")
        self.assertEqual(raw.words[1].start, 8.0)

    def test_non_dict_payload_is_error(self):
        with self.assertRaises(stt.STTError):
            _openai_transcript(["not", "a", "dict"])


class LanguageNormalizationTests(unittest.TestCase):
    def test_speechmatics_passthrough_and_auto(self):
        self.assertEqual(_normalize_language("speechmatics", "fa", model="enhanced"), "fa")
        self.assertEqual(_normalize_language("speechmatics", "auto", model="enhanced"), "auto")
        self.assertEqual(_normalize_language("speechmatics", "auto", model="melia-1"), "fa")
        self.assertEqual(_normalize_language("speechmatics", "ar_en"), "ar_en")

    def test_deepgram_multi_omits_language(self):
        self.assertEqual(_normalize_language("deepgram", "multi", model="nova-2"), "")
        self.assertEqual(_normalize_language("deepgram", "fa", model="nova-2"), "fa")


class FailoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_cooldowns()

    def tearDown(self):
        reset_cooldowns()

    @staticmethod
    def provider_with_run(slug, run):
        return replace(PROVIDERS[slug], run=run)

    async def test_order_respected_and_success_returned(self):
        settings = make_settings(stt_providers=("speechmatics",))
        calls = []

        async def run(_session, _path, _settings):
            calls.append("speechmatics")
            return stt.RawTranscript.from_text("متن", engine="speechmatics")

        with mock.patch.dict(
            stt.PROVIDERS, {"speechmatics": self.provider_with_run("speechmatics", run)}
        ):
            raw = await transcribe(audio_file(), settings, session=object())
        self.assertEqual(raw.text, "متن")
        self.assertEqual(calls, ["speechmatics"])

    async def test_auth_failure_fails_over_to_next_provider(self):
        settings = make_settings(stt_providers=("speechmatics", "deepgram"))
        calls = []

        async def speechmatics_run(_session, _path, _settings):
            calls.append("speechmatics")
            raise STTAuthenticationError("auth failed")

        async def deepgram_run(_session, _path, _settings):
            calls.append("deepgram")
            return stt.RawTranscript.from_text("از دیپ‌گرم", engine="deepgram")

        with mock.patch.dict(
            stt.PROVIDERS,
            {
                "speechmatics": self.provider_with_run("speechmatics", speechmatics_run),
                "deepgram": self.provider_with_run("deepgram", deepgram_run),
            },
        ):
            raw = await transcribe(audio_file(), settings, session=object())
        self.assertEqual(calls, ["speechmatics", "deepgram"])
        self.assertEqual(raw.text, "از دیپ‌گرم")
        # the auth failure put speechmatics on cooldown
        self.assertGreater(stt._cooldown_remaining("speechmatics"), 0)

    async def test_all_failures_reported(self):
        settings = make_settings(stt_providers=("speechmatics", "deepgram"))

        async def failing_run(_session, _path, _settings):
            raise STTRequestError("no")

        with mock.patch.dict(
            stt.PROVIDERS,
            {
                "speechmatics": self.provider_with_run("speechmatics", failing_run),
                "deepgram": self.provider_with_run("deepgram", failing_run),
            },
        ):
            with self.assertRaises(stt.STTError) as ctx:
                await transcribe(audio_file(), settings, session=object())
        self.assertIn("no", str(ctx.exception))

    async def test_cooldown_skips_provider(self):
        settings = make_settings(stt_providers=("speechmatics",))
        stt._set_cooldown("speechmatics", 60)
        try:
            with self.assertRaises((stt.STTError, STTConfigurationError)):
                await transcribe(audio_file(), settings, session=object())
        finally:
            reset_cooldowns()

    async def test_no_providers_configured(self):
        settings = make_settings(
            stt_providers=(),
            speechmatics_api_key="",
            deepgram_api_key="",
            stt_openai_base_url="",
        )
        with self.assertRaises(STTConfigurationError):
            await transcribe(audio_file(), settings, session=object())

    async def test_unconfigured_provider_is_skipped(self):
        settings = make_settings(
            stt_providers=("speechmatics", "deepgram"), speechmatics_api_key=""
        )
        calls = []

        async def deepgram_run(_session, _path, _settings):
            calls.append("deepgram")
            return stt.RawTranscript.from_text("ok", engine="deepgram")

        with mock.patch.dict(
            stt.PROVIDERS, {"deepgram": self.provider_with_run("deepgram", deepgram_run)}
        ):
            raw = await transcribe(audio_file(), settings, session=object())
        self.assertEqual(calls, ["deepgram"])
        self.assertEqual(raw.text, "ok")

    async def test_oversize_file_skips_provider(self):
        settings = make_settings(stt_providers=("speechmatics",))
        spec = replace(PROVIDERS["speechmatics"], max_upload=10)
        with mock.patch.dict(stt.PROVIDERS, {"speechmatics": spec}):
            with mock.patch.object(Path, "stat") as stat:
                stat.return_value = mock.Mock(st_size=999)
                with self.assertRaises(stt.STTError):
                    await transcribe(audio_file(), settings, session=object())

    async def test_transient_retries_then_succeeds(self):
        settings = make_settings(stt_providers=("speechmatics",))
        attempts = {"n": 0}

        async def flaky_run(_session, _path, _settings):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise STTTransientError("hiccup")
            return stt.RawTranscript.from_text("ok", engine="speechmatics")

        spec = replace(PROVIDERS["speechmatics"], run=flaky_run)
        with mock.patch.dict(stt.PROVIDERS, {"speechmatics": spec}):
            with mock.patch.object(asyncio, "sleep", new=_noop_sleep):
                raw = await transcribe(audio_file(), settings, session=object())
        self.assertEqual(raw.text, "ok")
        self.assertEqual(attempts["n"], 2)


async def _noop_sleep(*_args, **_kwargs):
    return None


class ErrorMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_mapping(self):
        response = FakeResponse(
            429, {"error": {"message": "slow down"}}, headers={"Retry-After": "2"}
        )
        error = await stt._error_from_response("Test", response)
        self.assertIsInstance(error, STTTransientError)

        response = FakeResponse(401, {"error": {"message": "invalid key"}})
        error = await stt._error_from_response("Test", response)
        self.assertIsInstance(error, STTAuthenticationError)

        response = FakeResponse(400, {"error": {"message": "bad audio"}})
        error = await stt._error_from_response("Test", response)
        self.assertIsInstance(error, STTRequestError)

        response = FakeResponse(503, {})
        error = await stt._error_from_response("Test", response)
        self.assertIsInstance(error, STTTransientError)


if __name__ == "__main__":
    unittest.main()
