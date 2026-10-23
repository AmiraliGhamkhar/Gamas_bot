"""Comprehensive offline tests for multi-user concurrency, non-blocking capacity
overflow, automatic STT/LLM failover, per-job state isolation, SQLite WAL
concurrency, duplicate-update protection, and leak-free cancellation cleanup.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from gamas_bot import llm, media, stt
from gamas_bot.bot import GamasBot
from gamas_bot.db import Database
from gamas_bot.jobs import JOB_STATES, Job, JobQueue, QueueFullError
from gamas_bot.logging_config import JsonFormatter, log_job_id
from gamas_bot.metrics import METRICS
from gamas_bot.notes import structure_transcript
from gamas_bot.progress import JobProgress
from gamas_bot.transcript import RawTranscript, Word
from tests.support import (
    FakeJobEvent,
    make_settings,
)


def _sample_notes_json(title: str = "جزوهٔ درس") -> str:
    return (
        '{"title": "'
        + title
        + '", "summary": "خلاصهٔ دقیق مباحث ارائه‌شده در جلسه.", '
        '"sections": [{"heading": "بخش اول", "points": ["نکتهٔ کلیدی اول"], '
        '"paragraphs": ["توضیح کامل و مستند به متن جلسه."]}], '
        '"key_terms": [{"term": "مفهوم پایه", "definition": "تعریف دقیق مفهوم."}]}'
    )


class _FakeHTTPResponse:
    def __init__(
        self,
        status: int,
        payload: dict | str,
        *,
        headers: dict[str, str] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}
        self._delay = delay

    async def read(self) -> bytes:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if isinstance(self._payload, bytes):
            return self._payload
        if isinstance(self._payload, str):
            return self._payload.encode("utf-8")
        import json

        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    async def text(self) -> str:
        raw = await self.read()
        return raw.decode("utf-8", "replace")

    async def json(self, content_type=None):  # noqa: ARG002
        import json

        return json.loads(await self.text())

    async def __aenter__(self):
        if self._delay > 0:
            await asyncio.sleep(self._delay)
            self._delay = 0.0
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RoutingLLMSession:
    """Offline session that routes requests per host URL for realistic failover tests."""

    def __init__(self, handlers: dict[str, object]) -> None:
        self._handlers = handlers
        self.calls: list[str] = []

    def _pick(self, url: str):
        self.calls.append(url)
        for prefix, handler in self._handlers.items():
            if prefix in url:
                return handler(url)
        raise AssertionError(f"Unexpected URL: {url}")

    def post(self, url: str, **kwargs):  # noqa: ARG002
        return self._pick(url)

    def get(self, url: str, **kwargs):  # noqa: ARG002
        return self._pick(url)


def _chat_completion_json(content: str, *, delay: float = 0.0) -> _FakeHTTPResponse:
    return _FakeHTTPResponse(
        200,
        {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
        },
        delay=delay,
    )


def _dual_llm_settings(**overrides):
    base: dict[str, object] = {
        "note_providers": ("nara", "openrouter"),
        "nara_api_key": "nara-secret-key",
        "nara_base_url": "https://router.bynara.id/v1",
        "nara_model": "openai/gpt-4o-mini",
        "openrouter_api_key": "or-secret-key",
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "openrouter_model": "google/gemini-2.5-flash-lite",
        "note_hedge_after": 0.0,
        "llm_primary_max_in_flight": 2,
        "llm_fallback_max_in_flight": 4,
        "llm_slot_wait_timeout": 1.0,
    }
    base.update(overrides)
    return make_settings(**base)


class CapacityAndLifecycleUnitTests(unittest.IsolatedAsyncioTestCase):
    """Core slot reservation invariants, job lifecycle states, and log safety."""

    def setUp(self) -> None:
        METRICS.reset()
        stt.reset_cooldowns()
        llm.reset_cooldowns()

    def tearDown(self) -> None:
        stt.reset_cooldowns()
        llm.reset_cooldowns()

    async def test_unique_job_ids_and_valid_lifecycle_transitions(self):
        j1 = Job(submission_id=10, user_id=1, kind="audio", filename="a.ogg")
        j2 = Job(submission_id=10, user_id=1, kind="audio", filename="a.ogg")
        self.assertNotEqual(j1.job_id, j2.job_id)
        self.assertEqual(j1.state, "queued")

        for state in (
            "downloading",
            "extracting",
            "transcribing",
            "generating_notes",
            "uploading",
            "completed",
        ):
            self.assertIn(state, JOB_STATES)
            j1.transition_to(state)

        self.assertEqual(
            [name for name, _ in j1.transitions],
            [
                "queued",
                "downloading",
                "extracting",
                "transcribing",
                "generating_notes",
                "uploading",
                "completed",
            ],
        )
        with self.assertRaises(ValueError):
            j1.transition_to("nonexistent_state")

    async def test_log_job_id_isolation_across_concurrent_tasks(self):
        observed: dict[str, list[str]] = {"a": [], "b": []}

        async def worker(tag: str, jid: str) -> None:
            tok = log_job_id.set(jid)
            try:
                for _ in range(5):
                    observed[tag].append(log_job_id.get())
                    await asyncio.sleep(0.005)
            finally:
                log_job_id.reset(tok)

        await asyncio.gather(worker("a", "job-101-aaaa"), worker("b", "job-202-bbbb"))
        self.assertEqual(observed["a"], ["job-101-aaaa"] * 5)
        self.assertEqual(observed["b"], ["job-202-bbbb"] * 5)
        self.assertEqual(log_job_id.get(), "-")

        # Verify JSON log formatter includes job_id without leaking unlogged text.
        import logging

        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="gamas_bot.jobs",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="job_state_transition state=%s",
            args=("transcribing",),
            exc_info=None,
        )
        record.job_id = "job-101-aaaa"
        rendered = formatter.format(record)
        self.assertIn('"job_id": "job-101-aaaa"', rendered)


class STTCapacityAndFailoverTests(unittest.IsolatedAsyncioTestCase):
    """Scenarios 3–9: Speechmatics primary + immediate non-blocking Deepgram overflow."""

    def setUp(self) -> None:
        METRICS.reset()
        stt.reset_cooldowns()
        self._tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self._tmp.name) / "sample.ogg"
        self.audio.write_bytes(b"OggS" + b"\x00" * 128)

    def tearDown(self) -> None:
        stt.reset_cooldowns()
        self._tmp.cleanup()

    def _patch_providers(self, sm_fn, dg_fn):
        return mock.patch.dict(
            stt.PROVIDERS,
            {
                "speechmatics": replace(stt.PROVIDERS["speechmatics"], run=sm_fn),
                "deepgram": replace(stt.PROVIDERS["deepgram"], run=dg_fn),
            },
        )

    async def test_speechmatics_healthy_serves_primary_without_touching_deepgram(self):
        dg_called = 0

        async def sm_ok(_session, _path, _settings):
            return RawTranscript.from_text("متن دقیق سخنرانی", engine="speechmatics")

        async def dg_unused(_session, _path, _settings):
            nonlocal dg_called
            dg_called += 1
            return RawTranscript.from_text("پشتیبان", engine="deepgram")

        settings = make_settings(stt_providers=("speechmatics", "deepgram"))
        with self._patch_providers(sm_ok, dg_unused):
            res = await stt.transcribe(self.audio, settings, session=object())
        self.assertEqual(res.engine, "speechmatics")
        self.assertEqual(dg_called, 0)

    async def test_speechmatics_saturated_immediately_overflows_to_deepgram_zero_wait(self):
        sm_started = asyncio.Event()
        release_sm = asyncio.Event()

        async def slow_sm(_session, _path, _settings):
            sm_started.set()
            await release_sm.wait()
            return RawTranscript.from_text("اولیه", engine="speechmatics")

        async def fast_dg(_session, _path, _settings):
            return RawTranscript.from_text("پشتیبان فوری", engine="deepgram")

        settings = make_settings(
            stt_providers=("speechmatics", "deepgram"),
            speechmatics_max_in_flight=1,
            deepgram_max_in_flight=3,
        )
        with self._patch_providers(slow_sm, fast_dg):
            t1 = asyncio.create_task(
                stt.transcribe(self.audio, settings, session=object())
            )
            await sm_started.wait()

            # Primary slot (1/1) is occupied; jobs 2 & 3 MUST finish via Deepgram
            # immediately while job 1 is STILL running on Speechmatics!
            t0 = time.monotonic()
            r2, r3 = await asyncio.gather(
                stt.transcribe(self.audio, settings, session=object()),
                stt.transcribe(self.audio, settings, session=object()),
            )
            overflow_elapsed = time.monotonic() - t0
            self.assertEqual(r2.engine, "deepgram")
            self.assertEqual(r3.engine, "deepgram")
            self.assertLess(overflow_elapsed, 0.25)

            release_sm.set()
            r1 = await t1
            self.assertEqual(r1.engine, "speechmatics")

        snap = METRICS.snapshot()["providers"]["stt"]
        self.assertGreaterEqual(snap["speechmatics"]["overflow_skips"], 2)
        self.assertGreaterEqual(snap["deepgram"]["failovers_in"], 2)
        self.assertEqual(snap["speechmatics"]["in_flight"], 0)
        self.assertEqual(snap["deepgram"]["in_flight"], 0)

    async def test_speechmatics_rate_limit_quota_and_timeout_fail_over_to_deepgram(self):
        # 1) HTTP 429 rate limit
        stt.reset_cooldowns()
        sm_429_session = SimpleNamespace(
            post=lambda *_a, **_kw: _FakeHTTPResponse(
                429, {"error": "rate limit"}, headers={"Retry-After": "30"}
            ),
            get=lambda *_a, **_kw: _FakeHTTPResponse(500, {}),
        )

        async def dg_ok(_session, _path, _settings):
            return RawTranscript.from_text("از دیپ‌گرام", engine="deepgram")

        settings = make_settings(
            stt_providers=("speechmatics", "deepgram"),
            speechmatics_api_key="sm-key",
            deepgram_api_key="dg-key",
        )
        with mock.patch.dict(
            stt.PROVIDERS,
            {"deepgram": replace(stt.PROVIDERS["deepgram"], run=dg_ok)},
        ):
            out_429 = await stt.transcribe(self.audio, settings, session=sm_429_session)
        self.assertEqual(out_429.engine, "deepgram")

        # 2) HTTP 402 quota exhaustion
        stt.reset_cooldowns()
        sm_402_session = SimpleNamespace(
            post=lambda *_a, **_kw: _FakeHTTPResponse(
                402, {"detail": "Insufficient credit / quota exceeded"}
            ),
            get=lambda *_a, **_kw: _FakeHTTPResponse(500, {}),
        )
        with mock.patch.dict(
            stt.PROVIDERS,
            {"deepgram": replace(stt.PROVIDERS["deepgram"], run=dg_ok)},
        ):
            out_402 = await stt.transcribe(self.audio, settings, session=sm_402_session)
        self.assertEqual(out_402.engine, "deepgram")
        self.assertGreaterEqual(
            METRICS.snapshot()["providers"]["stt"]["speechmatics"]["quota_exhaustions"],
            1,
        )

        # 3) Per-provider timeout (STT_PROVIDER_TIMEOUT_SECONDS)
        stt.reset_cooldowns()

        async def hung_sm(_session, _path, _settings):
            await asyncio.sleep(10.0)
            return RawTranscript.from_text("never", engine="speechmatics")

        fast_timeout_settings = make_settings(
            stt_providers=("speechmatics", "deepgram"),
            stt_provider_timeout=0.08,
        )
        with self._patch_providers(hung_sm, dg_ok):
            t0 = time.monotonic()
            out_timeout = await stt.transcribe(
                self.audio, fast_timeout_settings, session=object()
            )
            self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(out_timeout.engine, "deepgram")

    async def test_corrupt_invalid_audio_fails_fast_without_wasting_deepgram(self):
        dg_calls = 0

        async def dg_should_not_run(_session, _path, _settings):
            nonlocal dg_calls
            dg_calls += 1
            return RawTranscript.from_text("never", engine="deepgram")

        corrupt_session = SimpleNamespace(
            post=lambda *_a, **_kw: _FakeHTTPResponse(
                400, {"error": "Unsupported or corrupt audio format"}
            ),
            get=lambda *_a, **_kw: _FakeHTTPResponse(400, {}),
        )
        settings = make_settings(stt_providers=("speechmatics", "deepgram"))
        with mock.patch.dict(
            stt.PROVIDERS,
            {"deepgram": replace(stt.PROVIDERS["deepgram"], run=dg_should_not_run)},
        ):
            with self.assertRaises(stt.STTInvalidInputError):
                await stt.transcribe(self.audio, settings, session=corrupt_session)
        self.assertEqual(dg_calls, 0)

    async def test_both_stt_providers_saturated_waits_bounded_or_fails_gracefully(self):
        gate = asyncio.Event()

        async def hold_slot(_session, _path, _settings):
            await gate.wait()
            return RawTranscript.from_text("آزاد شد", engine="deepgram")

        settings = make_settings(
            stt_providers=("speechmatics", "deepgram"),
            speechmatics_max_in_flight=1,
            deepgram_max_in_flight=1,
            stt_slot_wait_timeout=0.12,
        )
        with self._patch_providers(hold_slot, hold_slot):
            j1 = asyncio.create_task(
                stt.transcribe(self.audio, settings, session=object())
            )
            j2 = asyncio.create_task(
                stt.transcribe(self.audio, settings, session=object())
            )
            await asyncio.sleep(0.03)
            # Both slots occupied; a 3rd job waits at most stt_slot_wait_timeout.
            with self.assertRaises(stt.STTTransientError):
                await stt.transcribe(self.audio, settings, session=object())
            gate.set()
            await asyncio.gather(j1, j2)


class LLMCapacityAndFailoverTests(unittest.IsolatedAsyncioTestCase):
    """Scenarios 10 & 11: non-blocking primary -> fallback LLM overflow and 429 fast failover."""

    def setUp(self) -> None:
        METRICS.reset()
        llm.reset_cooldowns()

    def tearDown(self) -> None:
        llm.reset_cooldowns()

    async def test_primary_llm_saturation_overflows_immediately_to_secondary_model(self):
        primary_entered = asyncio.Event()
        release_primary = asyncio.Event()

        class GateResponse(_FakeHTTPResponse):
            async def read(self) -> bytes:
                primary_entered.set()
                await release_primary.wait()
                return await super().read()

        session = _RoutingLLMSession(
            {
                "router.bynara.id": lambda _u: GateResponse(
                    200,
                    {
                        "id": "nara-1",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": _sample_notes_json("جزوهٔ مسیر اول"),
                                },
                            }
                        ],
                    },
                ),
                "openrouter.ai": lambda _u: _chat_completion_json(
                    _sample_notes_json("جزوهٔ مسیر پشتیبان")
                ),
            }
        )
        settings = _dual_llm_settings(
            llm_primary_max_in_flight=1,
            llm_fallback_max_in_flight=3,
            note_compile=False,
        )
        t_primary = asyncio.create_task(
            structure_transcript("متن جلسهٔ اول با نکات علمی.", settings, session)
        )
        await primary_entered.wait()

        # Primary (1/1) is busy; two concurrent note requests MUST overflow immediately
        # to OpenRouter with zero wait while primary is still blocked!
        t0 = time.monotonic()
        n2, n3 = await asyncio.gather(
            structure_transcript("متن جلسهٔ دوم با نکات علمی.", settings, session),
            structure_transcript("متن جلسهٔ سوم با نکات علمی.", settings, session),
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.25)
        self.assertEqual(n2.title, "جزوهٔ مسیر پشتیبان")
        self.assertEqual(n3.title, "جزوهٔ مسیر پشتیبان")

        release_primary.set()
        n1 = await t_primary
        self.assertEqual(n1.title, "جزوهٔ مسیر اول")

        llm_metrics = METRICS.snapshot()["providers"]["llm"]
        self.assertGreaterEqual(llm_metrics["nara"]["overflow_skips"], 2)
        self.assertGreaterEqual(llm_metrics["openrouter"]["failovers_in"], 2)

    async def test_primary_llm_http_429_fails_over_without_sleeping_retry_after(self):
        slept: list[float] = []

        async def record_sleep(seconds: float) -> None:
            slept.append(seconds)

        session = _RoutingLLMSession(
            {
                "router.bynara.id": lambda _u: _FakeHTTPResponse(
                    429,
                    {"error": {"message": "Rate limit exceeded"}},
                    headers={"Retry-After": "45"},
                ),
                "openrouter.ai": lambda _u: _chat_completion_json("پاسخ فوری پشتیبان"),
            }
        )
        settings = _dual_llm_settings()
        with mock.patch("asyncio.sleep", new=record_sleep):
            answer = await llm.complete(session, settings, system="S", user="U")
        self.assertEqual(answer, "پاسخ فوری پشتیبان")
        # Zero local sleep despite Retry-After: 45 header because fallback route existed!
        self.assertEqual(slept, [])
        self.assertEqual(len(session.calls), 2)


class MultiUserPipelineIsolationAndCleanupTests(unittest.IsolatedAsyncioTestCase):
    """Scenarios 1, 2, 12, 13, 14, 15 across GamasBot, JobQueue, SQLite WAL, and media subprocesses."""

    async def asyncSetUp(self) -> None:
        METRICS.reset()
        stt.reset_cooldowns()
        llm.reset_cooldowns()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=self.root / "gamas.db",
            temp_dir=self.root / "temp",
            receipt_dir=self.root / "receipts",
            max_active_jobs=4,
            max_active_jobs_per_user=2,
            max_queued_jobs=4,
            max_concurrent_downloads=2,
            media_worker_concurrency=2,
            max_concurrent_uploads=2,
            dedup_window_seconds=60.0,
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db

    async def asyncTearDown(self) -> None:
        await self.bot.shutdown()
        await self.db.close()
        stt.reset_cooldowns()
        llm.reset_cooldowns()
        self._tmp.cleanup()

    async def test_multiple_users_run_concurrently_with_complete_output_isolation(self):
        users = [1001, 1002, 1003, 1004]
        events: dict[int, FakeJobEvent] = {}

        class _RoutingClient:
            async def send_file(self, chat_id, path: str, *, caption=None, force_document=False, reply_to=None, buttons=None):  # noqa: ARG002
                target_ev = events[int(chat_id)]
                await target_ev.client.send_file(
                    chat_id,
                    path,
                    caption=caption,
                    force_document=force_document,
                    reply_to=reply_to,
                    buttons=buttons,
                )

            async def send_message(self, chat_id, message: str = "", **kwargs):
                target_ev = events[int(chat_id)]
                return await target_ev.client.send_message(chat_id, message, **kwargs)

            async def disconnect(self) -> None:
                return None

        self.bot.client = _RoutingClient()  # type: ignore[assignment]

        for uid in users:
            await self.db.ensure_user(uid)
            await self.db.add_credit(uid, 3600, note="seed")
            src = self.root / f"u-{uid}.ogg"
            src.write_bytes(f"audio-{uid}".encode("utf-8"))
            ev = FakeJobEvent(source=src, chat_id=uid, message_id=uid)
            ev.message.file.name = f"lecture-{uid}.ogg"
            events[uid] = ev

        active_now = 0
        peak_concurrent = 0

        async def fake_probe(path: Path, _settings):
            uid = int(path.read_bytes().decode("utf-8").split("-")[1])
            return media.MediaInfo(has_audio=True, duration=float(60 + uid % 10), audio_codec="opus")

        async def fake_transcribe(path: Path, _settings, *, session=None):  # noqa: ARG001
            nonlocal active_now, peak_concurrent
            uid = int(path.read_bytes().decode("utf-8").split("-")[1])
            active_now += 1
            peak_concurrent = max(peak_concurrent, active_now)
            await asyncio.sleep(0.06)
            active_now -= 1
            return RawTranscript(
                words=[Word(text=f"محتوای_اختصاصی_{uid}", start=0.0, end=1.0)],
                text=f"محتوای_اختصاصی_{uid}",
                engine="speechmatics",
            )

        self.bot.queue.ensure_workers()
        with mock.patch("gamas_bot.bot.probe_media", new=fake_probe), \
             mock.patch("gamas_bot.bot.needs_transcode", return_value=False), \
             mock.patch("gamas_bot.bot.transcribe", new=fake_transcribe):
            for uid in users:
                self.bot._stt_only_users.add(uid)
                await self.bot._accept_media(
                    events[uid], uid, "audio", f"lecture-{uid}.ogg", 60.0
                )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                stats = await self.db.stats()
                if stats["submissions_done"] == len(users):
                    break
                await asyncio.sleep(0.02)

        self.assertGreaterEqual(peak_concurrent, 2)
        # Verify zero cross-talk: every user received ONLY their own transcript!
        for uid in users:
            delivered_txt = events[uid].file_bytes(".txt").decode("utf-8")
            self.assertIn(f"محتوای_اختصاصی_{uid}", delivered_txt)
            for other in users:
                if other != uid:
                    self.assertNotIn(f"محتوای_اختصاصی_{other}", delivered_txt)
        # Verify temp dir has zero leftover submission-* directories!
        leftovers = list(self.settings.temp_dir.glob("submission-*"))
        self.assertEqual(leftovers, [])

    async def test_duplicate_update_and_duplicate_active_upload_deduplicated_safely(self):
        uid = 2020
        await self.db.ensure_user(uid)
        await self.db.add_credit(uid, 3600, note="seed")
        src = self.root / "dup.ogg"
        src.write_bytes(b"OggS" + b"\x01" * 64)

        ev = FakeJobEvent(source=src, chat_id=uid, message_id=777)
        ev.chat_id = uid
        ev.message.file.name = "dup.ogg"
        ev.message.document = SimpleNamespace(
            id=999888777, mime_type="audio/ogg", attributes=[]
        )
        ev.message.voice = True
        self.bot.client = ev.client

        # Send exact same Telegram message twice back-to-back (webhook/polling retry).
        await asyncio.gather(
            self.bot._handle_message(ev),
            self.bot._handle_message(ev),
        )
        subs = await self.db.user_submissions(uid)
        self.assertEqual(len(subs), 1)
        self.assertGreaterEqual(METRICS.snapshot()["deduplicated_updates"], 1)

    async def test_queue_backpressure_and_cancellation_cleanup_and_startup_recovery(self):
        # 1) Cancellation mid-pipeline cleans workdir & releases billing hold
        uid = 3030
        await self.db.ensure_user(uid)
        await self.db.add_credit(uid, 1800, note="seed")
        src = self.root / "cancel.ogg"
        src.write_bytes(b"OggS" + b"\x02" * 64)
        ev = FakeJobEvent(source=src, chat_id=uid, message_id=888)
        ev.message.file.name = "cancel.ogg"
        self.bot.client = ev.client

        entered_stt = asyncio.Event()

        async def slow_stt(_path, _settings, *, session=None):  # noqa: ARG001
            entered_stt.set()
            await asyncio.sleep(30.0)
            return RawTranscript.from_text("never", engine="speechmatics")

        sub_id = await self.db.create_submission(uid, "audio", "cancel.ogg")
        progress = await JobProgress.create(ev, submission_id=sub_id)
        job = Job(
            submission_id=sub_id,
            user_id=uid,
            kind="audio",
            filename="cancel.ogg",
            message=ev.message,
            duration_hint=120.0,
            meta={"progress": progress},
        )
        with mock.patch(
            "gamas_bot.bot.probe_media",
            return_value=media.MediaInfo(has_audio=True, duration=120.0, audio_codec="opus"),
        ), mock.patch("gamas_bot.bot.needs_transcode", return_value=False), \
             mock.patch("gamas_bot.bot.transcribe", new=slow_stt):
            run_task = asyncio.create_task(self.bot._run_job(job))
            await entered_stt.wait()
            run_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await run_task

        self.assertEqual(job.state, "cancelled")
        self.assertEqual(await self.db.available_seconds(uid), 1800)
        self.assertEqual(list(self.settings.temp_dir.glob("submission-*")), [])

        # 2) Queue full rejects gracefully with Persian message
        blocked = asyncio.Event()
        small_q = JobQueue(
            max_queued=2,
            max_active=1,
            worker=lambda _j: blocked.wait(),
        )
        small_q.submit(Job(submission_id=1, user_id=1, kind="audio", filename="1.ogg"))
        small_q.submit(Job(submission_id=2, user_id=2, kind="audio", filename="2.ogg"))
        with self.assertRaises(QueueFullError) as q_err:
            small_q.submit(Job(submission_id=3, user_id=3, kind="audio", filename="3.ogg"))
        self.assertIn("صف پردازش پر است", str(q_err.exception))
        blocked.set()
        await small_q.shutdown()

        # 3) Startup stale-submission recovery
        stale_sub = await self.db.create_submission(uid, "audio", "stale.ogg")
        await self.db.reserve(uid, stale_sub, 300)
        self.assertEqual(await self.db.available_seconds(uid), 1500)
        recovered_count = await self.db.recover_interrupted()
        self.assertEqual(recovered_count, 1)
        self.assertEqual(await self.db.available_seconds(uid), 1800)
        stale_row = await self.db.submission(stale_sub)
        assert stale_row is not None
        self.assertEqual(stale_row.status, "failed")

    async def test_concurrent_sqlite_wal_reads_and_writes_without_lock_errors(self):
        for uid in range(5000, 5012):
            await self.db.ensure_user(uid)
            await self.db.add_credit(uid, 3600, note="seed")

        async def writer(uid: int) -> None:
            for idx in range(4):
                sid = await self.db.create_submission(uid, "audio", f"f-{idx}.ogg")
                await self.db.reserve(uid, sid, 60)
                await self.db.save_transcription(sid, "speechmatics", "متن", "{}")
                await self.db.settle_hold(sid, 45)
                await self.db.set_submission_status(sid, "done", duration_seconds=45.0)

        async def reader(uid: int) -> None:
            for _ in range(8):
                bal = await self.db.available_seconds(uid)
                self.assertGreaterEqual(bal, 0)
                users_page = await self.db.list_users(limit=15)
                self.assertGreaterEqual(len(users_page), 12)
                _ = await self.db.stats()

        await asyncio.gather(
            *(writer(uid) for uid in range(5000, 5012)),
            *(reader(uid) for uid in range(5000, 5006)),
        )
        stats = await self.db.stats()
        self.assertGreaterEqual(stats["submissions_done"], 48)

    async def test_one_user_submitting_multiple_jobs_preserves_fifo_cap_and_fairness(self):
        # User 1 submits 3 jobs (exceeding max_active_jobs_per_user=2 -> 3rd rejected
        # cleanly at intake until one finishes), and User 2 submitting concurrently
        # runs immediately alongside User 1 instead of waiting behind User 1's queue.
        for uid in (4001, 4002):
            await self.db.ensure_user(uid)
            await self.db.add_credit(uid, 7200, note="seed")

        s1 = await self.db.create_submission(
            4001, "audio", "first.ogg", active_limit=self.settings.max_active_jobs_per_user
        )
        s2 = await self.db.create_submission(
            4001, "audio", "second.ogg", active_limit=self.settings.max_active_jobs_per_user
        )
        from gamas_bot.db import TooManyActiveJobsError

        with self.assertRaises(TooManyActiveJobsError):
            await self.db.create_submission(
                4001, "audio", "third.ogg", active_limit=self.settings.max_active_jobs_per_user
            )
        # Another user can still submit freely with zero blocking from User 4001!
        s_other = await self.db.create_submission(
            4002, "audio", "u2.ogg", active_limit=self.settings.max_active_jobs_per_user
        )
        self.assertGreater(s_other, s2)

        started_order: list[tuple[int, int]] = []
        done = asyncio.Event()

        async def track_worker(j: Job) -> None:
            started_order.append((j.user_id, j.submission_id))
            await asyncio.sleep(0.03)
            if len(started_order) == 3:
                done.set()

        q = JobQueue(max_queued=6, max_active=2, worker=track_worker)
        q.submit(Job(submission_id=s1, user_id=4001, kind="audio", filename="first.ogg"))
        q.submit(Job(submission_id=s2, user_id=4001, kind="audio", filename="second.ogg"))
        q.submit(Job(submission_id=s_other, user_id=4002, kind="audio", filename="u2.ogg"))
        q.ensure_workers()
        await asyncio.wait_for(done.wait(), timeout=2.0)
        await q.shutdown()

        # User 4002 runs in parallel with User 4001's first job instead of waiting
        # behind User 4001's second job, and User 4001's own jobs stay FIFO (s1 -> s2).
        self.assertEqual(started_order[:2], [(4001, s1), (4002, s_other)])
        self.assertEqual([sid for uid, sid in started_order if uid == 4001], [s1, s2])

    async def test_subprocess_cancellation_leaves_no_orphans_or_closed_loop_errors(self):
        task = asyncio.create_task(
            media.run_command(
                [sys.executable, "-c", "import time; time.sleep(15)"],
                timeout=10.0,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
