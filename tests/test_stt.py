"""Tests for the v3 speech-to-text layer: parsing, failover, cooldowns."""

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from gamas_bot import config, stt
from gamas_bot.fileio import AsyncFilePayload
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
        self.post_kwargs: list[dict] = []
        self.get_calls: list[str] = []

    def post(self, url, **kwargs):
        self.post_calls.append(url)
        self.post_kwargs.append(kwargs)
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

    def test_trailing_both_punctuation_is_not_duplicated(self):
        results = [
            {"type": "word", "alternatives": [{"content": "واژه"}]},
            {"type": "punctuation", "alternatives": [{"content": "-", "attaches_to": "both"}]},
        ]
        text, words = _speechmatics_words(results)
        self.assertEqual(text, "واژه-")
        self.assertEqual([word.text for word in words], ["واژه-"])


class SpeechmaticsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end(self):
        settings = make_settings(stt_providers=("speechmatics",))
        results = [
            {"type": "word", "start_time": 0.0, "end_time": 0.5,
             "alternatives": [{"content": "درس"}]},
            {"type": "punctuation", "alternatives": [{"content": ".", "attaches_to": "previous"}]},
        ]
        session = speechmatics_flow(results)
        path = audio_file()
        raw = await stt._speechmatics(session, path, settings)
        audio_payload = session.post_kwargs[0]["data"]._fields[1][2]
        self.assertIsInstance(audio_payload, AsyncFilePayload)
        self.assertEqual(await audio_payload.as_bytes(), path.read_bytes())
        self.assertEqual(raw.text, "درس.")
        self.assertEqual(raw.engine, "speechmatics")
        self.assertEqual(raw.words[0].start, 0.0)
        # The transcript request asked for json-v2 (the timed format).
        self.assertEqual(session.get_kwargs.get("params"), {"format": "json-v2"})

    async def test_multilingual_model_requests_multilingual_detection(self):
        settings = make_settings(
            stt_providers=("speechmatics",),
            speechmatics_model="melia-1",
            stt_language="auto",
        )
        session = speechmatics_flow([
            {"type": "word", "alternatives": [{"content": "درس"}]},
        ])
        await stt._speechmatics(session, audio_file(), settings)
        form = session.post_kwargs[0]["data"]
        config = json.loads(form._fields[0][2])
        self.assertEqual(config["transcription_config"]["language"], "multi")

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



    async def test_speechmatics_job_cancellation_has_a_short_deadline(self):
        started = asyncio.Event()
        cancelled: list[bool] = []

        class HangingDelete:
            async def __aenter__(self):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise

            async def __aexit__(self, *_exc):
                return False

        class DeleteSession:
            def __init__(self):
                self.kwargs = None

            def delete(self, _url, **kwargs):
                self.kwargs = kwargs
                return HangingDelete()

        session = DeleteSession()
        with mock.patch.object(stt, "SPEECHMATICS_DELETE_TIMEOUT_SECONDS", 0.01):
            await stt._cancel_speechmatics_job(session, make_settings(), "job-cancel")
        self.assertTrue(started.is_set())
        self.assertEqual(cancelled, [True])
        self.assertAlmostEqual(session.kwargs["timeout"].total, 0.01)


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

    def test_punctuation_only_transcript_is_error(self):
        """Silence decoded as "." is not a transcript to deliver and bill."""
        payload = {
            "results": {"channels": [{"alternatives": [{"transcript": ".", "words": []}]}]}
        }
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

    def test_punctuation_only_text_is_error(self):
        with self.assertRaises(stt.STTError):
            _openai_transcript({"text": "…"})

    def test_persian_digits_alone_are_meaningful(self):
        raw = _openai_transcript({"text": "۱۲۳"})
        self.assertEqual(raw.text, "۱۲۳")


class MeaningfulTextTests(unittest.TestCase):
    def test_letters_and_digits_count(self):
        for text in ("سلام", "fa", "۱۲۳", "۴۰ کلمه"):
            self.assertTrue(stt._has_meaningful_text(text), text)

    def test_punctuation_and_whitespace_do_not_count(self):
        for text in ("", "   ", ".", "…", "!؟", "\u200c"):
            self.assertFalse(stt._has_meaningful_text(text), repr(text))


class LanguageNormalizationTests(unittest.TestCase):
    def test_speechmatics_passthrough_and_auto(self):
        self.assertEqual(_normalize_language("speechmatics", "fa", model="enhanced"), "fa")
        self.assertEqual(_normalize_language("speechmatics", "auto", model="enhanced"), "auto")
        self.assertEqual(_normalize_language("speechmatics", "auto", model="melia-1"), "multi")
        self.assertEqual(_normalize_language("speechmatics", "fa", model="melia-1"), "multi")
        self.assertEqual(_normalize_language("speechmatics", "multi", model="melia-1"), "multi")
        self.assertEqual(_normalize_language("speechmatics", "multi", model="enhanced"), "fa")
        self.assertEqual(_normalize_language("speechmatics", "ar_en"), "ar_en")

    def test_deepgram_passes_real_language_codes_through(self):
        # Persian is a documented Nova-3 language; the code must send it.
        self.assertEqual(_normalize_language("deepgram", "fa", model="nova-3"), "fa")
        # multi is a documented value of the multilingual Nova models.
        self.assertEqual(_normalize_language("deepgram", "multi", model="nova-3"), "multi")
        self.assertEqual(_normalize_language("deepgram", "Multi", model="nova-3"), "multi")

    def test_deepgram_auto_is_the_detection_parameter_not_an_omission(self):
        # Omitting `language` makes Deepgram default to English: detection has
        # its own parameter, signalled here with the "auto" sentinel.
        for keyword in ("auto", "detect", "AUTO"):
            self.assertEqual(_normalize_language("deepgram", keyword, model="nova-3"), "auto")

    def test_openai_auto_detection_omits_language(self):
        for keyword in ("auto", "detect", "multi"):
            self.assertEqual(_normalize_language("openai_compatible", keyword), "")
        self.assertEqual(_normalize_language("openai_compatible", "fa"), "fa")


def speechmatics_words(text: str = "سلام") -> list[dict]:
    """One json-v2 ``results`` entry, as the transcript endpoint returns it."""
    return [
        {"type": "word", "start_time": 0.0, "end_time": 0.5,
         "alternatives": [{"content": text}]},
    ]


class SpeechmaticsLongPollTests(unittest.IsolatedAsyncioTestCase):
    """``wait`` on the submit is what removes the poll *and* the transcript GET."""

    async def test_submit_asks_for_the_long_poll_and_the_timed_format(self):
        settings = make_settings(speechmatics_model="enhanced", stt_language="fa")
        session = speechmatics_flow(speechmatics_words())
        await stt._speechmatics(session, audio_file("wait.wav"), settings)
        params = session.post_kwargs[0]["params"]
        self.assertEqual(params, {"wait": "25", "format": "json-v2"})

    async def test_long_poll_can_be_disabled(self):
        settings = make_settings(stt_status_long_poll=0.0)
        session = speechmatics_flow(speechmatics_words())
        await stt._speechmatics(session, audio_file("nowait.wav"), settings)
        # Only the format remains; no wait parameter is invented.
        self.assertEqual(session.post_kwargs[0]["params"], {"format": "json-v2"})

    async def test_an_inline_transcript_ends_the_job_without_any_get(self):
        settings = make_settings()
        session = FakeSession(
            posts=[
                FakeResponse(
                    201,
                    {
                        "id": "job-9",
                        "status": "done",
                        "json-v2": {"results": speechmatics_words("متن آماده")},
                    },
                )
            ]
        )
        raw = await stt._speechmatics(session, audio_file("inline.wav"), settings)
        self.assertEqual(raw.text, "متن آماده")
        self.assertEqual(session.get_calls, [])

    async def test_an_empty_inline_document_fails_like_an_empty_transcript(self):
        settings = make_settings()
        session = FakeSession(
            posts=[
                FakeResponse(
                    201,
                    {"id": "job-10", "status": "done", "json-v2": {"results": []}},
                )
            ]
        )
        # The inline document is the transcript: an empty one is the same
        # "nothing was transcribed" answer the transcript endpoint would give,
        # so the chain fails over instead of shipping an empty file.
        with self.assertRaises(stt.STTError):
            await stt._speechmatics(session, audio_file("empty.wav"), settings)
        self.assertEqual(session.get_calls, [])

    async def test_done_without_an_inline_document_skips_the_status_poll(self):
        settings = make_settings()
        session = FakeSession(
            posts=[FakeResponse(201, {"id": "job-11", "status": "done"})],
            gets=[FakeResponse(200, {"results": speechmatics_words("بدون وضعیت")})],
        )
        raw = await stt._speechmatics(session, audio_file("nopoll.wav"), settings)
        self.assertEqual(raw.text, "بدون وضعیت")
        self.assertEqual(len(session.get_calls), 1)
        self.assertTrue(session.get_calls[0].endswith("/transcript"))

    async def test_created_still_polls_and_fetches_the_transcript(self):
        settings = make_settings(stt_poll_interval=1.0)
        session = FakeSession(
            posts=[FakeResponse(201, {"id": "job-12", "status": "created"})],
            gets=[
                FakeResponse(200, {"job": {"status": "done"}}),
                FakeResponse(200, {"results": speechmatics_words("دوباره")}),
            ],
        )
        raw = await stt._speechmatics(session, audio_file("poll.wav"), settings)
        self.assertEqual(raw.text, "دوباره")
        self.assertEqual(len(session.get_calls), 2)

    async def test_a_rejected_submit_fails_without_polling(self):
        settings = make_settings()
        session = FakeSession(
            posts=[FakeResponse(201, {"id": "job-13", "status": "rejected"})]
        )
        with self.assertRaises(STTRequestError):
            await stt._speechmatics(session, audio_file("rejected.wav"), settings)
        self.assertEqual(session.get_calls, [])


class ProviderRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_deepgram_persian_request_sends_language_and_model(self):
        """Persian: model=nova-3 with language=fa is what the API documents."""
        settings = make_settings(
            deepgram_model="nova-3", stt_language="fa",
        )
        payload = {
            "results": {"channels": [{"alternatives": [{"transcript": "سلام", "words": []}]}]}
        }
        session = FakeSession(posts=[FakeResponse(200, payload)])
        raw = await stt._deepgram(session, audio_file("fa.wav"), settings)
        self.assertEqual(raw.text, "سلام")
        params = session.post_kwargs[0]["params"]
        self.assertEqual(params["model"], "nova-3")
        self.assertEqual(params["language"], "fa")
        self.assertNotIn("detect_language", params)

    async def test_deepgram_auto_asks_for_language_detection(self):
        settings = make_settings(deepgram_model="nova-3", stt_language="auto")
        payload = {
            "results": {"channels": [{"alternatives": [{"transcript": "متن", "words": []}]}]}
        }
        session = FakeSession(posts=[FakeResponse(200, payload)])
        await stt._deepgram(session, audio_file("auto.wav"), settings)
        params = session.post_kwargs[0]["params"]
        self.assertEqual(params["detect_language"], "true")
        self.assertNotIn("language", params)

    async def test_deepgram_multi_requests_the_documented_multilingual_value(self):
        settings = make_settings(deepgram_model="nova-3", stt_language="multi")
        payload = {
            "results": {"channels": [{"alternatives": [{"transcript": "متن", "words": []}]}]}
        }
        session = FakeSession(posts=[FakeResponse(200, payload)])
        await stt._deepgram(session, audio_file("multi.wav"), settings)
        self.assertEqual(session.post_kwargs[0]["params"]["language"], "multi")

    async def test_speechmatics_submits_the_configured_model_and_language(self):
        settings = make_settings(speechmatics_model="enhanced", stt_language="fa")
        session = speechmatics_flow([
            {"type": "word", "start_time": 0.0, "end_time": 0.5,
             "alternatives": [{"content": "سلام"}]},
        ])
        await stt._speechmatics(session, audio_file("sm.wav"), settings)
        form = session.post_kwargs[0]["data"]
        config = json.loads(form._fields[0][2])
        self.assertEqual(config["transcription_config"]["model"], "enhanced")
        self.assertEqual(config["transcription_config"]["language"], "fa")
        self.assertEqual(session.post_calls[0], "https://example.test/v2/jobs")
        # The transcript request must ask for the timed format.
        self.assertEqual(session.get_kwargs["params"], {"format": "json-v2"})

    async def test_deepgram_accepts_a_full_listen_endpoint_and_sends_audio_mime(self):
        settings = make_settings(
            deepgram_base_url="https://dg.example/v1/listen?tenant=course",
            stt_language="fa",
        )
        payload = {
            "results": {"channels": [{"alternatives": [{"transcript": "متن", "words": []}]}]}
        }
        session = FakeSession(posts=[FakeResponse(200, payload)])
        path = audio_file("deepgram.wav")
        await stt._deepgram(session, path, settings)
        self.assertIsInstance(session.post_kwargs[0]["data"], AsyncFilePayload)
        self.assertEqual(await session.post_kwargs[0]["data"].as_bytes(), path.read_bytes())
        self.assertEqual(
            session.post_calls[0], "https://dg.example/v1/listen?tenant=course"
        )
        self.assertEqual(session.post_kwargs[0]["headers"]["Content-Type"], "audio/wav")

    async def test_openai_falls_back_only_for_verbose_json_rejection(self):
        settings = make_settings(
            stt_openai_base_url="https://asr.example/v1",
            stt_openai_api_key="stt-key",
        )
        session = FakeSession([
            FakeResponse(400, {"error": {"message": "verbose_json is not supported"}}),
            FakeResponse(200, {"text": "متن ساده"}),
        ])
        raw = await stt._openai_compatible(session, audio_file("openai.wav"), settings)
        self.assertEqual(raw.text, "متن ساده")
        self.assertEqual(len(session.post_calls), 2)
        self.assertEqual(
            session.post_calls[0], "https://asr.example/v1/audio/transcriptions"
        )

    async def test_openai_auto_detection_omits_invalid_auto_language_field(self):
        settings = make_settings(
            stt_openai_base_url="https://asr.example/v1",
            stt_openai_api_key="stt-key",
            stt_language="auto",
        )
        session = FakeSession([FakeResponse(200, {"text": "متن"})])
        path = audio_file("openai.wav")
        await stt._openai_compatible(session, path, settings)
        fields = session.post_kwargs[0]["data"]._fields
        names = [field[0].get("name") for field in fields]
        self.assertNotIn("language", names)
        file_value = next(field[2] for field in fields if field[0].get("name") == "file")
        self.assertIsInstance(file_value, AsyncFilePayload)
        self.assertEqual(await file_value.as_bytes(), path.read_bytes())

    async def test_openai_does_not_retry_unrelated_bad_request(self):
        settings = make_settings(
            stt_openai_base_url="https://asr.example/v1",
            stt_openai_api_key="stt-key",
        )
        session = FakeSession([
            FakeResponse(400, {"error": {"message": "invalid audio file"}}),
        ])
        with self.assertRaises(STTRequestError):
            await stt._openai_compatible(session, audio_file("openai.wav"), settings)
        self.assertEqual(len(session.post_calls), 1)


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

    async def test_unusable_provider_response_fails_over(self):
        settings = make_settings(stt_providers=("speechmatics", "deepgram"))
        calls = []

        async def malformed_run(_session, _path, _settings):
            calls.append("speechmatics")
            raise stt.STTError("bad response schema")

        async def deepgram_run(_session, _path, _settings):
            calls.append("deepgram")
            return stt.RawTranscript.from_text("متن", engine="deepgram")

        with mock.patch.dict(
            stt.PROVIDERS,
            {
                "speechmatics": self.provider_with_run("speechmatics", malformed_run),
                "deepgram": self.provider_with_run("deepgram", deepgram_run),
            },
        ):
            raw = await transcribe(audio_file(), settings, session=object())
        self.assertEqual(raw.engine, "deepgram")
        self.assertEqual(calls, ["speechmatics", "deepgram"])

    async def test_a_network_error_fails_over_without_a_second_upload(self):
        """A submit is never repeated: the next provider gets a fresh upload."""
        settings = make_settings(stt_providers=("speechmatics", "deepgram"))
        calls = []

        async def broken_run(_session, _path, _settings):
            calls.append("speechmatics")
            raise stt.aiohttp.ClientConnectionError("temporary connection issue")

        async def working_run(_session, _path, _settings):
            calls.append("deepgram")
            return stt.RawTranscript.from_text("متن", engine="deepgram")

        with mock.patch.dict(
            stt.PROVIDERS,
            {
                "speechmatics": self.provider_with_run("speechmatics", broken_run),
                "deepgram": self.provider_with_run("deepgram", working_run),
            },
        ):
            with mock.patch.object(asyncio, "sleep", new=_noop_sleep):
                raw = await transcribe(audio_file(), settings, session=object())
        self.assertEqual(raw.text, "متن")
        # Exactly one attempt per provider: the 4-hour upload happened once.
        self.assertEqual(calls, ["speechmatics", "deepgram"])

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

    async def test_openai_limit_is_applied_before_upload(self):
        settings = make_settings(
            stt_providers=("openai_compatible",),
            stt_openai_base_url="https://asr.example/v1",
            stt_openai_api_key="key",
            stt_openai_max_upload=5,
        )
        calls = []

        async def should_not_run(_session, _path, _settings):
            calls.append(True)
            return stt.RawTranscript.from_text("unexpected", engine="openai_compatible")

        spec = replace(PROVIDERS["openai_compatible"], run=should_not_run)
        with mock.patch.dict(stt.PROVIDERS, {"openai_compatible": spec}):
            with self.assertRaises(stt.STTError):
                await transcribe(audio_file("over-limit.wav"), settings, session=object())
        self.assertEqual(calls, [])

    async def test_a_transient_submit_failure_fails_over_instead_of_re_uploading(self):
        settings = make_settings(stt_providers=("speechmatics", "deepgram"))
        calls = []

        async def throttled_run(_session, _path, _settings):
            calls.append("speechmatics")
            raise STTTransientError("hiccup", retry_after_seconds=3600.0)

        async def working_run(_session, _path, _settings):
            calls.append("deepgram")
            return stt.RawTranscript.from_text("ok", engine="deepgram")

        with mock.patch.dict(
            stt.PROVIDERS,
            {
                "speechmatics": self.provider_with_run("speechmatics", throttled_run),
                "deepgram": self.provider_with_run("deepgram", working_run),
            },
        ):
            with mock.patch.object(asyncio, "sleep", new=_noop_sleep):
                raw = await transcribe(audio_file(), settings, session=object())
        self.assertEqual(raw.text, "ok")
        self.assertEqual(calls, ["speechmatics", "deepgram"])

    async def test_a_transient_status_read_is_retried_within_the_deadline(self):
        """The only retries left are idempotent GETs: no upload is repeated."""
        settings = make_settings(
            stt_providers=("speechmatics",), stt_poll_interval=0.01
        )
        delays: list[float] = []

        async def record_sleep(delay, *_args, **_kwargs):
            delays.append(delay)

        session = FakeSession(
            posts=[FakeResponse(202, {"id": "job-1"})],
            gets=[
                FakeResponse(429, {"error": "slow down"}, {"Retry-After": "3600"}),
                FakeResponse(200, {"job": {"status": "done"}}),
                FakeResponse(200, {"results": [
                    {"type": "word", "start_time": 0.0, "end_time": 0.4,
                     "alternatives": [{"content": "سلام"}]},
                ]}),
            ],
        )
        with mock.patch.object(asyncio, "sleep", new=record_sleep):
            raw = await stt._speechmatics(session, audio_file(), settings)
        self.assertEqual(raw.text, "سلام")
        # One bounded retry of the status read, and the hour-long Retry-After
        # was clamped instead of parking the job.
        self.assertEqual(len(delays), 1)
        self.assertLessEqual(delays[0], stt.MAX_RETRY_AFTER_SECONDS)
        self.assertEqual(len(session.post_calls), 1)

    async def test_a_transient_submit_error_of_the_last_provider_is_reported(self):
        settings = make_settings(stt_providers=("speechmatics",))
        attempts = {"n": 0}

        async def throttled_run(_session, _path, _settings):
            attempts["n"] += 1
            raise STTTransientError("slow down", retry_after_seconds=3600.0)

        spec = replace(PROVIDERS["speechmatics"], run=throttled_run)
        with mock.patch.dict(stt.PROVIDERS, {"speechmatics": spec}):
            with mock.patch.object(asyncio, "sleep", new=_noop_sleep):
                with self.assertRaises(stt.STTError):
                    await transcribe(audio_file(), settings, session=object())
        self.assertEqual(attempts["n"], 1)

    async def test_configured_order_is_respected(self):
        settings = make_settings(stt_providers=("deepgram", "speechmatics"))
        calls = []

        async def run(slug):
            async def _run(_session, _path, _settings):
                calls.append(slug)
                return stt.RawTranscript.from_text("متن", engine=slug)
            return _run

        with mock.patch.dict(
            stt.PROVIDERS,
            {
                "speechmatics": self.provider_with_run("speechmatics", await run("speechmatics")),
                "deepgram": self.provider_with_run("deepgram", await run("deepgram")),
            },
        ):
            raw = await transcribe(audio_file(), settings, session=object())
        self.assertEqual(calls, ["deepgram"])
        self.assertEqual(raw.engine, "deepgram")

    async def test_all_providers_in_cooldown_says_so_instead_of_silence(self):
        settings = make_settings(stt_providers=("speechmatics", "deepgram"))
        stt._set_cooldown("speechmatics", 60)
        stt._set_cooldown("deepgram", 60)
        with self.assertRaises(stt.STTError) as ctx:
            await transcribe(audio_file(), settings, session=object())
        message = str(ctx.exception)
        self.assertIn("Speechmatics", message)
        self.assertIn("Deepgram", message)


class UploadCeilingTests(unittest.IsolatedAsyncioTestCase):
    """Documented provider ceilings are enforced before a byte is uploaded."""

    def setUp(self):
        reset_cooldowns()

    def tearDown(self):
        reset_cooldowns()

    def test_ceilings_are_the_conservative_documented_values(self):
        # Speechmatics Batch rejects a POST body of 1 GB or more; Deepgram
        # accepts at most 2 GB of pre-recorded audio.
        self.assertEqual(config.SPEECHMATICS_MAX_UPLOAD, 1_000_000_000)
        self.assertEqual(config.DEEPGRAM_MAX_UPLOAD, 2_000_000_000)
        self.assertEqual(PROVIDERS["speechmatics"].max_upload, config.SPEECHMATICS_MAX_UPLOAD)
        self.assertEqual(PROVIDERS["deepgram"].max_upload, config.DEEPGRAM_MAX_UPLOAD)

    async def test_file_over_the_speechmatics_ceiling_skips_it_before_upload(self):
        settings = make_settings(stt_providers=("speechmatics", "deepgram"))
        calls = []

        async def speechmatics_run(_session, _path, _settings):
            calls.append("speechmatics")
            return stt.RawTranscript.from_text("unexpected", engine="speechmatics")

        async def deepgram_run(_session, _path, _settings):
            calls.append("deepgram")
            return stt.RawTranscript.from_text("از دیپ‌گرم", engine="deepgram")

        with mock.patch.dict(
            stt.PROVIDERS,
            {
                "speechmatics": replace(
                    PROVIDERS["speechmatics"], run=speechmatics_run
                ),
                "deepgram": replace(PROVIDERS["deepgram"], run=deepgram_run),
            },
        ):
            with mock.patch.object(Path, "stat") as stat:
                stat.return_value = mock.Mock(st_size=1_500_000_000)
                raw = await transcribe(audio_file("big.wav"), settings, session=object())
        self.assertEqual(calls, ["deepgram"])
        self.assertEqual(raw.engine, "deepgram")


async def _noop_sleep(*_args, **_kwargs):
    return None


class ProviderFailureTests(unittest.IsolatedAsyncioTestCase):
    """Malformed and empty provider answers are errors, never transcripts."""

    def setUp(self):
        reset_cooldowns()

    def tearDown(self):
        reset_cooldowns()

    async def test_non_json_body_is_a_schema_error(self):
        class Broken(FakeResponse):
            async def json(self, content_type=None):
                raise ValueError("not json")

        settings = make_settings(stt_providers=("deepgram",))
        session = FakeSession(posts=[Broken(200)])
        with self.assertRaises(stt.STTError):
            await stt._deepgram(session, audio_file("broken.wav"), settings)

    async def test_deepgram_unsupported_language_is_a_request_error_then_failover(self):
        """nova-2 + language=fa: the provider rejects it, Deepgram is skipped."""
        settings = make_settings(
            stt_providers=("deepgram", "openai_compatible"),
            deepgram_model="nova-2",
            stt_openai_base_url="https://asr.example/v1",
            stt_openai_api_key="key",
        )
        session = FakeSession([
            FakeResponse(400, {"err_msg": "language not available for model"}),
            FakeResponse(200, {"text": "متن"}),
        ])
        raw = await transcribe(audio_file("fa.wav"), settings, session=session)
        self.assertEqual(raw.engine, "openai_compatible")
        self.assertEqual(raw.text, "متن")
        self.assertEqual(len(session.post_calls), 2)

    async def test_speechmatics_empty_results_is_an_error(self):
        settings = make_settings(stt_providers=("speechmatics",))
        session = speechmatics_flow([])
        with self.assertRaises(stt.STTError):
            await stt._speechmatics(session, audio_file("empty.wav"), settings)

    async def test_speechmatics_unexpected_status_keeps_polling_until_timeout(self):
        settings = make_settings(
            stt_providers=("speechmatics",), stt_job_timeout=60.0, stt_poll_interval=1.0
        )
        session = FakeSession(
            posts=[FakeResponse(202, {"id": "job-9"})],
            gets=[
                FakeResponse(200, {"job": {"status": "running"}}),
                FakeResponse(200, {"job": {"status": "running"}}),
            ],
        )
        ticks = iter([0.0, 0.5, 200.0])

        def clock() -> float:
            # deadline = 0.0 + 60; the second loop check is already past it.
            return next(ticks, 200.0)

        with mock.patch.object(stt, "time", SimpleNamespace(monotonic=clock)):
            with mock.patch.object(asyncio, "sleep", new=_noop_sleep):
                with self.assertRaises(STTTransientError):
                    await stt._speechmatics(session, audio_file("slow.wav"), settings)
        self.assertEqual(len(session.get_calls), 1)

    async def test_missing_job_id_is_a_schema_error(self):
        settings = make_settings(stt_providers=("speechmatics",))
        session = FakeSession(posts=[FakeResponse(202, {"job": {}})])
        with self.assertRaises(stt.STTError):
            await stt._speechmatics(session, audio_file("nojob.wav"), settings)

    async def test_punctuation_only_answer_fails_over_instead_of_being_delivered(self):
        """A "." for silence must not become a delivered, billed transcript."""
        settings = make_settings(stt_providers=("deepgram", "speechmatics"))
        session = FakeSession(
            posts=[FakeResponse(200, {"results": {"channels": [{"alternatives": [
                {"transcript": ".", "words": []}
            ]}]}})]
        )
        calls: list[str] = []

        async def speechmatics_run(_session, _path, _settings):
            calls.append("speechmatics")
            return stt.RawTranscript.from_text("متن واقعی", engine="speechmatics")

        with mock.patch.dict(
            stt.PROVIDERS,
            {"speechmatics": replace(PROVIDERS["speechmatics"], run=speechmatics_run)},
        ):
            raw = await transcribe(audio_file("silence.wav"), settings, session=session)
        self.assertEqual(calls, ["speechmatics"])
        self.assertEqual(raw.engine, "speechmatics")
        self.assertEqual(raw.text, "متن واقعی")

    async def test_every_provider_empty_raises_with_the_reason(self):
        settings = make_settings(stt_providers=("deepgram",))
        session = FakeSession(
            posts=[FakeResponse(200, {"results": {"channels": [{"alternatives": [
                {"transcript": "", "words": []}
            ]}]}})]
        )
        with self.assertRaises(stt.STTError) as ctx:
            await transcribe(audio_file("silent.wav"), settings, session=session)
        self.assertIn("Deepgram", str(ctx.exception))


class PipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """A real audio file through the failover driver into a real document."""

    def setUp(self):
        reset_cooldowns()

    def tearDown(self):
        reset_cooldowns()

    async def test_persian_audio_reaches_a_non_empty_document(self):
        from gamas_bot.transcript import build_document
        from tests.support import wav_bytes

        path = Path(audio_file("pipeline.wav")).with_name("gamas-test-pipeline.wav")
        path.write_bytes(wav_bytes(duration=0.2))
        settings = make_settings(
            stt_providers=("deepgram",),
            stt_language="fa",
            deepgram_model="nova-3",
        )
        payload = {
            "results": {"channels": [{"alternatives": [{
                "transcript": "سلام دوستان. امروز درس شبکه است.",
                "confidence": 0.91,
                "words": [
                    {"punctuated_word": "سلام", "start": 0.0, "end": 0.4},
                    {"punctuated_word": "دوستان.", "start": 0.5, "end": 1.0},
                    {"punctuated_word": "امروز", "start": 2.0, "end": 2.4},
                    {"punctuated_word": "درس", "start": 2.5, "end": 2.8},
                    {"punctuated_word": "شبکه", "start": 2.9, "end": 3.2},
                    {"punctuated_word": "است.", "start": 3.3, "end": 3.6},
                ],
            }]}]}
        }
        session = FakeSession(posts=[FakeResponse(200, payload)])
        raw = await transcribe(path, settings, session=session)
        self.assertEqual(raw.engine, "deepgram")
        self.assertTrue(raw.text.strip())
        self.assertEqual(session.post_kwargs[0]["params"]["language"], "fa")

        document = build_document(raw)
        text = document.plain()
        self.assertTrue(text.strip())
        self.assertIn("سلام دوستان.", text)
        self.assertTrue(document.paragraphs)


class ErrorMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_mapping(self):
        response = FakeResponse(
            429, {"error": {"message": "slow down"}}, headers={"Retry-After": "2"}
        )
        error = await stt._error_from_response("Test", response)
        self.assertIsInstance(error, STTTransientError)
        self.assertEqual(error.retry_after_seconds, 2.0)

        response = FakeResponse(408, {})
        error = await stt._error_from_response("Test", response)
        self.assertIsInstance(error, STTTransientError)

        response = FakeResponse(401, {"error": {"message": "invalid key secret-stt-key"}})
        error = await stt._error_from_response("Test", response, api_key="secret-stt-key")
        self.assertIsInstance(error, STTAuthenticationError)
        self.assertNotIn("secret-stt-key", str(error))

        response = FakeResponse(400, {"error": {"message": "bad audio"}})
        error = await stt._error_from_response("Test", response)
        self.assertIsInstance(error, STTRequestError)

        response = FakeResponse(503, {})
        error = await stt._error_from_response("Test", response)
        self.assertIsInstance(error, STTTransientError)


if __name__ == "__main__":
    unittest.main()
