"""Offline performance/reliability tests: transport, streaming, no-hedging, office.

No network: every provider is a fake session/response double.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from gamas_bot import llm, office_ai
from gamas_bot.office_ai import plan_deck
from gamas_bot.bot import GamasBot
from gamas_bot.config import from_env, note_routes
from gamas_bot.capacity import pool_for
from gamas_bot.llm import (
    LLMOutputBudgetError,
    LLMResponseError,
    LLMTransientError,
    complete,
    complete_json,
)
from tests.support import make_settings
from tests.test_llm import routed_settings


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
        self.read_count = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.read_count += 1
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


class _BoundedContent:
    def __init__(self, body: bytes):
        self.body = body
        self.position = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        start = self.position
        self.position = min(len(self.body), start + size)
        return self.body[start:self.position]


class _BoundedBodyResponse:
    def __init__(self, body: bytes):
        self.content = _BoundedContent(body)
        self.headers = {}

    async def read(self):
        raise AssertionError("bounded stream reader should be used")


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
        return make_settings(
            avalai_api_key="avalai-test-key", note_ttfb_timeout=0.5, **kw
        )

    async def test_multi_delta_reassembly_and_usage_logged(self):
        lines = [
            b": OPENROUTER PROCESSING",
            sse(delta("Hel")),
            sse(delta("lo")),
            sse({**delta("", finish="stop"),
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}),
            b"data: [DONE]",
        ]
        session = DispatchSession([("api.avalai.ir", StreamingResponse(lines))])
        with self.assertLogs("gamas_bot.llm", level="INFO") as logs:
            result = await complete(session, self.settings(), system="S", user="U")
        self.assertEqual(result, "Hello")
        usage_lines = [line for line in logs.output if "note provider usage" in line]
        self.assertTrue(usage_lines and "output_tokens=5" in usage_lines[0])
        self.assertIn("ttfb_ms=", usage_lines[0])
        self.assertIn("elapsed_ms=", usage_lines[0])
        # Streaming was requested only on this opted-in path.
        self.assertTrue(session.calls[0]["json"]["stream"])

    async def test_streaming_stops_reading_as_soon_as_the_body_cap_is_crossed(self):
        settings = routed_settings(note_ttfb_timeout=0.5, note_hedge_after=0.0)
        route = note_routes(settings)[0]
        response = StreamingResponse([
            sse(delta("first")),
            sse(delta("never read")),
        ])
        session = DispatchSession([("api.avalai.ir", response)])
        with mock.patch.object(llm, "MAX_LLM_RESPONSE_BYTES", 4):
            with self.assertRaises(LLMResponseError):
                await llm._complete_streaming_on_route(
                    route,
                    session,
                    settings,
                    url="https://api.avalai.ir/v1/chat/completions",
                    headers={},
                    payload={},
                    timeout_seconds=10.0,
                )
        self.assertEqual(response.content.read_count, 1)

    async def test_zero_ttfb_timeout_disables_the_first_byte_guard(self):
        settings = routed_settings(note_ttfb_timeout=0.0, note_hedge_after=0.0)
        route = note_routes(settings)[0]
        session = DispatchSession([
            ("api.avalai.ir", StreamingResponse([sse(delta("fast")), b"data: [DONE]"]))
        ])
        result = await llm._complete_streaming_on_route(
            route,
            session,
            settings,
            url="https://api.avalai.ir/v1/chat/completions",
            headers={},
            payload={},
            timeout_seconds=10.0,
        )
        self.assertEqual(result, "fast")

    async def test_nonstreaming_body_reader_stops_at_one_byte_over_the_cap(self):
        response = _BoundedBodyResponse(b"{\"content\":\"too large\"}")
        with mock.patch.object(llm, "MAX_LLM_RESPONSE_BYTES", 5):
            with self.assertRaises(LLMResponseError):
                await llm._read_json_body(response)
        self.assertEqual(response.content.position, 6)
        self.assertEqual(response.content.read_sizes, [6])

    async def test_structured_output_composes_with_streaming(self):
        """Both halves are shipped defaults, so the pair must work together.

        A fresh install has ``NOTE_TTFB_TIMEOUT_SECONDS=25`` *and*
        ``NOTE_STRUCTURED_OUTPUT=1``; a note request is therefore streamed and
        schema-constrained at the same time. This pins that combination: the
        streamed POST still carries ``response_format``, and the reassembled
        deltas parse as the JSON object the schema describes.
        """
        expected = {"sections": [{"heading": "مقدمه"}]}
        lines = [sse(delta(json.dumps(expected, ensure_ascii=False))), b"data: [DONE]"]
        session = DispatchSession([("api.avalai.ir", lambda: StreamingResponse(lines))])
        schema = {
            "type": "object",
            "properties": {
                "sections": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["sections"],
        }
        result = await complete_json(
            session,
            self.settings(note_structured_output=True),
            system="S",
            user="U",
            schema=schema,
        )
        self.assertEqual(result, expected)
        sent = session.calls[0]["json"]
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertEqual(sent["response_format"]["json_schema"]["name"], "gamas_notes")

    async def test_length_finish_reason_is_output_budget_error(self):
        lines = [sse(delta("partial", finish="length")), b"data: [DONE]"]
        session = DispatchSession([("api.avalai.ir", lambda: StreamingResponse(lines))])
        with self.assertRaises(LLMOutputBudgetError):
            await complete(session, self.settings(), system="S", user="U")
        self.assertEqual(len(session.calls), 1)  # never resent unchanged

    async def test_streamed_error_object_maps_to_transient(self):
        lines = [sse({"error": {"code": 429, "message": "slow down"}})]
        session = DispatchSession([("api.avalai.ir", lambda: StreamingResponse(lines))])
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
            ("avalai", lambda: StreamingResponse([], hang_first=True)),
            ("openrouter", lambda: StreamingResponse(list(backup_lines))),
        ])
        result = await complete(
            session, settings, system="S", user="U",
            data_sensitivity="STANDARD_EDUCATIONAL",
        )
        self.assertEqual(result, "backup")
        avalai_calls = [c for c in session.calls if "api.avalai.ir" in c["url"]]
        self.assertEqual(len(avalai_calls), 1)


class ShippedDefaultWiringTests(unittest.IsolatedAsyncioTestCase):
    """The defaults a fresh install really has must reach the transport.

    ``ShippedDefaultTests`` pins the numbers; these tests prove the numbers are
    wired: a default install streams (25 s first-byte guard) while still asking
    for the schema, and a cheap first provider is *not* doubled by the 60 s
    hedge because it finishes long before the delay.
    """

    def settings(self, **extra):
        environment = {
            # Fresh-install surface: AvalAI primary + OpenRouter fallback.
            "NOTE_PROVIDERS": "avalai,openrouter",
            "AVALAI_API_KEY": "avalai-key",
            "OPENROUTER_API_KEY": "or-key",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            return from_env(env_file="/nonexistent")

    async def test_a_fresh_install_streams_and_sends_the_schema(self):
        settings = self.settings()
        self.assertEqual(settings.note_ttfb_timeout, 25.0)
        expected = {"sections": [{"heading": "مقدمه"}]}
        lines = [sse(delta(json.dumps(expected, ensure_ascii=False))), b"data: [DONE]"]
        session = DispatchSession([("api.avalai.ir", lambda: StreamingResponse(lines))])
        result = await complete_json(
            session,
            settings,
            system="S",
            user="U",
            schema={"type": "object", "properties": {"sections": {"type": "array"}}},
        )
        self.assertEqual(result, expected)
        sent = session.calls[0]["json"]
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["response_format"]["type"], "json_schema")

    async def test_the_default_install_never_hedges_a_healthy_request(self):
        """Hedging is disabled at the shipped default (NOTE_HEDGE_AFTER_SECONDS=0):
        a cheap first provider answers alone and the fallback is never consulted."""
        settings = self.settings()
        self.assertEqual(settings.note_hedge_after, 0.0)
        self.assertEqual(settings.note_concurrency, 3)
        lines = [sse(delta("fast", finish="stop")), b"data: [DONE]"]
        session = DispatchSession([("api.avalai.ir", lambda: StreamingResponse(lines))])
        result = await complete(session, settings, system="S", user="U")
        self.assertEqual(result, "fast")
        self.assertEqual(len(session.calls), 1)


class NoHedgingTests(unittest.IsolatedAsyncioTestCase):
    """Healthy calls never race a backup, even with a stale nonzero hedge knob.

    Patch only the provider I/O boundary: public dispatch, local capacity and
    the real temporary SQLite quota ledger all run normally.
    """

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def settings(self, name, *, hedge_after=0.001):
        return routed_settings(
            db_path=self.root / f"{name}.sqlite3",
            note_hedge_after=hedge_after,
            llm_primary_max_in_flight=1,
            llm_fallback_max_in_flight=1,
        )

    def assert_released(self, settings):
        pool = pool_for("llm", settings)
        self.assertEqual(pool.in_flight("avalai"), 0)
        self.assertEqual(pool.in_flight("openrouter"), 0)
        with sqlite3.connect(settings.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM llm_quota_reservations").fetchone()[0], 0)

    @staticmethod
    def invoke(entry, settings):
        return getattr(llm, entry)(
            object(), settings, system="Synthetic instruction", user="Synthetic lesson",
            data_sensitivity="STANDARD_EDUCATIONAL",
        )

    async def test_slow_healthy_primary_is_not_hedged_with_default_or_stale_knob(self):
        for entry, seam in (("complete", "_complete_on_route"), ("complete_json", "_complete_json_on_route")):
            for delay in (0.0, 0.001):
                with self.subTest(entry=entry, hedge_after=delay):
                    settings = self.settings(f"{entry}-{delay}", hedge_after=delay)
                    entered, release = asyncio.Event(), asyncio.Event()
                    calls = []
                    expected = {"ok": True} if entry == "complete_json" else "primary answer"

                    async def provider(route, *_args, calls=calls, entered=entered, release=release, expected=expected, **_kwargs):
                        calls.append(route.slug)
                        if route.slug == "avalai":
                            entered.set()
                            await release.wait()
                        return expected

                    with mock.patch.object(llm, seam, side_effect=provider):
                        job = asyncio.create_task(self.invoke(entry, settings))
                        try:
                            await asyncio.wait_for(entered.wait(), timeout=2.0)
                            # Let several stale hedge deadlines elapse while
                            # the first provider still owns its slot.
                            await asyncio.sleep(0.03)
                            self.assertEqual(calls, ["avalai"])
                            self.assertFalse(job.done())
                            self.assertEqual(pool_for("llm", settings).in_flight("openrouter"), 0)
                            release.set()
                            self.assertEqual(await asyncio.wait_for(job, timeout=2.0), expected)
                        finally:
                            release.set()
                            if not job.done():
                                job.cancel()
                            await asyncio.gather(job, return_exceptions=True)
                    self.assertEqual(calls, ["avalai"])
                    self.assert_released(settings)

    async def test_backup_starts_only_after_primary_failure_and_slot_release(self):
        for entry, seam in (("complete", "_complete_on_route"), ("complete_json", "_complete_json_on_route")):
            with self.subTest(entry=entry):
                settings = self.settings(entry)
                entered, release = asyncio.Event(), asyncio.Event()
                events = []
                expected = {"ok": True} if entry == "complete_json" else "backup answer"

                async def provider(route, *_args, events=events, entered=entered, release=release, settings=settings, expected=expected, **_kwargs):
                    events.append(f"{route.slug}:start")
                    try:
                        if route.slug == "avalai":
                            entered.set()
                            await release.wait()
                            raise LLMResponseError("synthetic primary failure")
                        self.assertEqual(pool_for("llm", settings).in_flight("avalai"), 0)
                        return expected
                    finally:
                        events.append(f"{route.slug}:stop")

                with mock.patch.object(llm, seam, side_effect=provider):
                    job = asyncio.create_task(self.invoke(entry, settings))
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=2.0)
                        await asyncio.sleep(0.03)
                        self.assertEqual(events, ["avalai:start"])
                        release.set()
                        self.assertEqual(await asyncio.wait_for(job, timeout=2.0), expected)
                    finally:
                        release.set()
                        if not job.done():
                            job.cancel()
                        await asyncio.gather(job, return_exceptions=True)
                self.assertEqual(events, ["avalai:start", "avalai:stop", "openrouter:start", "openrouter:stop"])
                self.assert_released(settings)

    async def test_cancelled_primary_releases_local_and_sqlite_slots_without_starting_backup(self):
        for entry, seam in (("complete", "_complete_on_route"), ("complete_json", "_complete_json_on_route")):
            with self.subTest(entry=entry):
                settings = self.settings(entry)
                entered = asyncio.Event()
                calls = []
                expected = {"ok": True} if entry == "complete_json" else "next answer"

                async def provider(route, *_args, calls=calls, entered=entered, expected=expected, **_kwargs):
                    calls.append(route.slug)
                    if len(calls) == 1:
                        entered.set()
                        await asyncio.Event().wait()
                    return expected

                with mock.patch.object(llm, seam, side_effect=provider):
                    job = asyncio.create_task(self.invoke(entry, settings))
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=2.0)
                        job.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(job, timeout=2.0)
                    finally:
                        if not job.done():
                            job.cancel()
                        await asyncio.gather(job, return_exceptions=True)
                    self.assertEqual(calls, ["avalai"])
                    self.assert_released(settings)
                    self.assertEqual(await asyncio.wait_for(self.invoke(entry, settings), timeout=2.0), expected)
                self.assertEqual(calls, ["avalai", "avalai"])
                self.assert_released(settings)


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

        async def fake_complete_json(session, settings, *, system, user, **_kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            # Later chunks finish first; gather must still preserve order.
            index = int(user.rsplit("سطر شماره ", 1)[1].split("\n", 1)[0].strip())
            await asyncio.sleep(0.05 * (5 - index))
            active -= 1
            return {"sections": [{"title": f"بخش {index}", "lines": [f"سطر شماره {index}"]}]}

        with mock.patch.object(office_ai, "complete_json", fake_complete_json):
            slides = await plan_deck(
                self.TEXT,
                settings=make_settings(avalai_api_key="avalai-test-key"),
                session=object(),
            )
        assert slides is not None
        self.assertGreater(max_active, 1)
        self.assertEqual([s.title for s in slides], ["بخش 1", "بخش 2", "بخش 3", "بخش 4"])

    async def test_dropped_lines_failure_still_triggers(self):
        async def fake_complete_json(session, settings, *, system, user, **_kwargs):
            return {"sections": [{"title": "بخش", "lines": ["یک", "دو", "سه", "چهار"]}]}

        with mock.patch.object(office_ai, "complete_json", fake_complete_json):
            slides = await plan_deck(
                self.TEXT,
                settings=make_settings(avalai_api_key="avalai-test-key"),
                session=object(),
            )
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
            _session, _settings, _context, group, *, mode, presentation, budget, source_locators=None
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
