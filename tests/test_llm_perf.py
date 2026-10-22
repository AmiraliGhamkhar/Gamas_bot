"""Phase 2/3 performance-layer tests: transport, streaming, hedging, office.

No network: every provider is a fake session/response double.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from gamas_bot import llm, office_ai
from gamas_bot.office_ai import plan_deck
from gamas_bot.bot import GamasBot
from gamas_bot.config import note_routes
from gamas_bot.llm import (
    LLMOutputBudgetError,
    LLMTransientError,
    complete,
)
from tests.support import make_settings
from tests.test_llm import FakeResponse, routed_settings


async def _noop(*_a, **_k):
    return None


class _AsyncCM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return False


class _StreamContent:
    """An async line iterator; ``hang_first`` never yields a first byte."""

    def __init__(self, lines, hang_first: bool = False):
        self._lines = list(lines)
        self._hang_first = hang_first

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._hang_first:
            await asyncio.sleep(30)
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class StreamingResponse:
    def __init__(self, lines, hang_first: bool = False):
        self.status = 200
        self.headers = {}
        self.content = _StreamContent(lines, hang_first=hang_first)

    async def read(self):
        return b""


def sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n".encode()


def delta(text: str, finish=None):
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


class DispatchSession:
    """Routes by URL substring; records every call."""

    def __init__(self, handlers):
        self.handlers = handlers  # list[(needle, response-or-callable)]
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        for needle, response in self.handlers:
            if needle in url:
                return _AsyncCM(response() if callable(response) else response)
        raise AssertionError(f"no handler for {url}")

    def get(self, url, **kwargs):
        return self.post(url, **kwargs)


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **kw):
        return make_settings(note_ttfb_timeout=0.5, **kw)

    async def test_multi_delta_reassembly_and_usage_logged(self):
        lines = [
            b": OPENROUTER PROCESSING",
            sse(delta("Hel")),
            sse(delta("lo")),
            sse({**delta("", finish="stop"),
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}),
            b"data: [DONE]",
        ]
        session = DispatchSession([("note.example", StreamingResponse(lines))])
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            result = await complete(session, self.settings(), system="S", user="U")
        self.assertEqual(result, "Hello")
        usage_lines = [line for line in logs.output if "note provider usage" in line]
        self.assertTrue(usage_lines and "output_tokens=5" in usage_lines[0])
        self.assertIn("ttfb_ms=", usage_lines[0])
        self.assertIn("elapsed_ms=", usage_lines[0])
        # Streaming was requested only on this opted-in path.
        self.assertTrue(session.calls[0]["json"]["stream"])

    async def test_length_finish_reason_is_output_budget_error(self):
        lines = [sse(delta("partial", finish="length")), b"data: [DONE]"]
        session = DispatchSession([("note.example", lambda: StreamingResponse(lines))])
        with self.assertRaises(LLMOutputBudgetError):
            await complete(session, self.settings(), system="S", user="U")
        self.assertEqual(len(session.calls), 1)  # never resent unchanged

    async def test_streamed_error_object_maps_to_transient(self):
        lines = [sse({"error": {"code": 429, "message": "slow down"}})]
        session = DispatchSession([("note.example", lambda: StreamingResponse(lines))])
        settings = self.settings()
        route = note_routes(settings)[0]
        with mock.patch("asyncio.sleep", new=_noop):
            with self.assertRaises(LLMTransientError):
                await llm._complete_on_route(
                    route, session, settings, system="S", user="U"
                )

    async def test_ttfb_threshold_failovers_without_same_provider_retry(self):
        settings = routed_settings(note_ttfb_timeout=0.05)
        backup_lines = [sse(delta("backup", finish="stop"))]
        session = DispatchSession([
            ("bynara", lambda: StreamingResponse([], hang_first=True)),
            ("openrouter", lambda: StreamingResponse(list(backup_lines))),
        ])
        result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "backup")
        nara_calls = [c for c in session.calls if "bynara" in c["url"]]
        self.assertEqual(len(nara_calls), 1)


class HedgeResponse(FakeResponse):
    def __init__(self, text, delay, cancelled: list):
        super().__init__(200, {"choices": [{"message": {"content": text}}]})
        self._delay = delay
        self._cancelled = cancelled

    async def read(self):
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self._cancelled.append(True)
            raise
        return json.dumps(self._payload).encode()


class HedgingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_primary_fast_backup_wins_and_primary_cancelled(self):
        cancelled: list = []
        session = DispatchSession([
            ("bynara", lambda: HedgeResponse("primary", 5.0, cancelled)),
            ("openrouter", lambda: HedgeResponse("backup", 0.01, cancelled)),
        ])
        settings = routed_settings(note_hedge_after=0.05)
        result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "backup")
        self.assertEqual(cancelled, [True])

    async def test_fast_primary_failure_stays_sequential(self):
        cancelled: list = []
        session = DispatchSession([
            ("bynara", FakeResponse(401, {"error": {"message": "bad key"}})),
            ("openrouter", lambda: HedgeResponse("backup", 0.0, cancelled)),
        ])
        settings = routed_settings(note_hedge_after=5.0)
        result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "backup")
        self.assertEqual(len(session.calls), 2)  # one per provider, no race


class TransportTests(unittest.TestCase):
    def test_orjson_fallback_parses_identically(self):
        body = json.dumps({"a": [1, 2], "b": "متن"}).encode()
        self.assertEqual(llm._loads(body), {"a": [1, 2], "b": "متن"})
        stdlib_loads = lambda b: json.loads(b.decode("utf-8", "replace"))  # noqa: E731
        self.assertEqual(stdlib_loads(body), llm._loads(body))

    def test_connector_kwargs_reach_session_constructor(self):
        bot = GamasBot.__new__(GamasBot)
        bot.settings = make_settings(llm_prewarm=False)
        bot._http = None
        with mock.patch("gamas_bot.bot.aiohttp") as fake_aiohttp:
            fake_aiohttp.ClientSession.return_value.closed = False
            session = bot._http_session()
        self.assertIs(session, fake_aiohttp.ClientSession.return_value)
        _args, kwargs = fake_aiohttp.ClientSession.call_args
        self.assertIn("connector", kwargs)
        self.assertIn("timeout", kwargs)
        fake_aiohttp.TCPConnector.assert_called_once_with(
            limit=32, limit_per_host=8, ttl_dns_cache=300, keepalive_timeout=300.0
        )


class PlanDeckConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    TEXT = "\n".join(
        part
        for i in range(1, 5)
        for part in (f"# بخش {i}", f"سطر شماره {i}")
    )

    async def test_chunks_run_concurrently_and_order_is_preserved(self):
        active = 0
        max_active = 0

        async def fake_complete_json(session, settings, *, system, user):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            # Later chunks finish first; gather must still preserve order.
            index = int(user.rsplit("سطر شماره ", 1)[1].split("\n", 1)[0].strip())
            await asyncio.sleep(0.05 * (5 - index))
            active -= 1
            return {"sections": [{"title": f"بخش {index}", "lines": [f"سطر شماره {index}"]}]}

        with mock.patch.object(office_ai, "complete_json", fake_complete_json):
            slides = await plan_deck(self.TEXT, settings=make_settings(), session=object())
        assert slides is not None
        self.assertGreater(max_active, 1)
        self.assertEqual([s.title for s in slides], ["بخش 1", "بخش 2", "بخش 3", "بخش 4"])

    async def test_dropped_lines_failure_still_triggers(self):
        async def fake_complete_json(session, settings, *, system, user):
            return {"sections": [{"title": "بخش", "lines": ["یک", "دو", "سه", "چهار"]}]}

        with mock.patch.object(office_ai, "complete_json", fake_complete_json):
            slides = await plan_deck(self.TEXT, settings=make_settings(), session=object())
        self.assertIsNone(slides)


class PipelinePerfOptimizationsTests(unittest.IsolatedAsyncioTestCase):
    def test_mtproto_part_size_configured_to_512kb(self):
        from telethon import utils as telethon_utils

        from gamas_bot.bot import _max_mtproto_part_size_kb

        self.assertEqual(_max_mtproto_part_size_kb(0), 64)
        self.assertEqual(_max_mtproto_part_size_kb(1024), 512)
        self.assertEqual(_max_mtproto_part_size_kb(50 * 1024 * 1024), 512)
        self.assertEqual(telethon_utils.get_appropriated_part_size(1024 * 1024), 512)

    def test_stt_adaptive_poll_interval_ramps_to_steady_state(self):
        from gamas_bot.stt import _adaptive_poll_interval

        self.assertAlmostEqual(_adaptive_poll_interval(2.0, 1), 0.5)
        self.assertAlmostEqual(_adaptive_poll_interval(2.0, 2), 1.0)
        self.assertAlmostEqual(_adaptive_poll_interval(2.0, 3), 1.5)
        self.assertAlmostEqual(_adaptive_poll_interval(2.0, 4), 2.0)
        self.assertAlmostEqual(_adaptive_poll_interval(2.0, 10), 2.0)

    async def test_prewarm_warms_both_llm_and_stt_hosts_concurrently(self):
        warmed: list[str] = []

        class _HeadResp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def read(self):
                return b""

        class _HeadSession:
            def head(self, url, **_kwargs):
                warmed.append(url)
                return _HeadResp()

        bot = GamasBot.__new__(GamasBot)
        bot.settings = make_settings(
            note_providers=("gemini",),
            gemini_api_key="gem-key",
            stt_providers=("deepgram",),
            deepgram_api_key="dg-key",
        )
        bot._http = _HeadSession()  # type: ignore[assignment]
        await bot._prewarm_llm()
        self.assertEqual(len(warmed), 2)
        self.assertTrue(any("generativelanguage.googleapis.com" in u for u in warmed))
        self.assertTrue(any("deepgram.com" in u for u in warmed))

    async def test_gemini_sse_streaming_collects_chunks_and_filters_thoughts(self):
        lines = [
            b'data: {"candidates":[{"content":{"parts":[{"thought":true,"text":"thinking..."}]}}]}\n',
            b'data: {"candidates":[{"content":{"parts":[{"text":"hello "}]}}]}\n',
            b'data: {"candidates":[{"content":{"parts":[{"text":"world"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":2}}\n',
        ]
        posted_urls: list[str] = []

        class _GemStreamSession:
            def post(self, url, **_kwargs):
                posted_urls.append(url)
                return _AsyncCM(StreamingResponse(lines))

        settings = make_settings(
            note_providers=("gemini",),
            gemini_api_key="gem-key",
            note_ttfb_timeout=2.0,
        )
        result = await complete(_GemStreamSession(), settings, system="S", user="U")
        self.assertEqual(result, "hello world")
        self.assertEqual(len(posted_urls), 1)
        self.assertIn(":streamGenerateContent", posted_urls[0])
        self.assertIn("alt=sse", posted_urls[0])

    async def test_compile_in_groups_runs_concurrently_and_preserves_order(self):
        from gamas_bot import notes as notes_mod
        from gamas_bot.notes import LectureContext, NoteSection, StructuredNotes

        sections = tuple(
            NoteSection(
                heading=f"بخش {i}",
                paragraphs=(f"متن آموزشی بخش {i} دربارهٔ شبکه و پروتکل.",),
            )
            for i in range(1, 5)
        )
        merged = StructuredNotes(title="جزوه", sections=sections)
        active = 0
        max_active = 0

        async def fake_compile_once(
            _session, _settings, _context, group, *, mode, presentation, budget
        ):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            first_heading = group.sections[0].heading
            idx = int(first_heading.split()[-1])
            await asyncio.sleep(0.03 * (5 - idx))
            active -= 1
            return group

        settings = make_settings(note_concurrency=4, note_strict_grounding=False)
        with (
            mock.patch.object(
                notes_mod,
                "compile_groups",
                return_value=[
                    StructuredNotes(title="جزوه", sections=(s,)) for s in sections
                ],
            ),
            mock.patch.object(notes_mod, "_compile_once", fake_compile_once),
        ):
            assembled = await notes_mod._compile_in_groups(
                merged,
                LectureContext(),
                "متن منبع",
                settings,
                object(),  # type: ignore[arg-type]
                mode="full",
                presentation=False,
                budget=500,
            )
        self.assertGreater(max_active, 1)
        self.assertEqual(
            [s.heading for s in assembled.sections],
            ["بخش 1", "بخش 2", "بخش 3", "بخش 4"],
        )

    def test_source_evidence_index_cache_hits_and_matches_audit(self):
        from gamas_bot.notes import NoteSection, NoteTable, PracticalTip, StructuredNotes, gate_notes
        from gamas_bot.notes_evidence import build_source_index

        build_source_index.cache_clear()
        source = (
            "بیمار باید روزانه 500 mg متفورمین مصرف کند و قند خون ناشتا زیر 110 mg/dL بماند."
        )
        notes = StructuredNotes(
            title="جزوه",
            sections=(
                NoteSection(
                    heading="درمان",
                    paragraphs=(
                        "بیمار باید روزانه 500 mg متفورمین مصرف کند و قند خون ناشتا زیر 110 mg/dL بماند.",
                    ),
                    table=NoteTable(
                        headers=("دارو", "دوز"),
                        rows=(("متفورمین", "500 mg"),),
                    ),
                ),
            ),
            practical_tips=(
                PracticalTip(
                    text="روزانه 500 mg متفورمین مصرف شود.",
                    basis="بیمار باید روزانه 500 mg متفورمین مصرف کند",
                ),
            ),
        )
        gated1, report1 = gate_notes(notes, source, strict=True)
        info_after_first = build_source_index.cache_info()
        gated2, report2 = gate_notes(notes, source, strict=True)
        info_after_second = build_source_index.cache_info()
        self.assertEqual(gated1, gated2)
        self.assertEqual(report1, report2)
        self.assertGreater(info_after_second.hits, info_after_first.hits)


if __name__ == "__main__":
    unittest.main()
