"""Reproducible multi-user concurrency & automatic provider failover benchmark.

Exercises the real pipeline components offline (zero external network calls,
zero paid API keys required):

1. Stage 1 — simulated Telegram download I/O under ``MAX_CONCURRENT_DOWNLOADS``;
2. Stages 2 & 3 — real ``gamas_bot.media_worker`` subprocess audio extraction &
   normalization (PyAV) under ``MEDIA_WORKER_CONCURRENCY``;
3. Stage 4 — ``gamas_bot.stt.transcribe`` against a simulated cluster where
   Speechmatics enforces hard concurrency=2 (returning HTTP 429 on overload) and
   Deepgram serves overflow traffic;
4. Stage 5 — ``gamas_bot.notes.structure_transcript`` + real ``build_notes_docx``
   against a simulated cluster where primary LLM enforces hard concurrency=2
   (returning HTTP 429 + ``Retry-After: 0.7`` on overload) and secondary LLM
   serves overflow traffic;
5. Stage 6 — simulated Telegram upload under ``MAX_CONCURRENT_UPLOADS`` plus
   real WAL SQLite billing reserve/settle and transcript persistence.

Run:
    .venv/bin/python -m scripts.concurrency_bench
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime
import json
import math
from pathlib import Path
import struct
import tempfile
import time
import tracemalloc
import wave

from gamas_bot import llm, media, stt
from gamas_bot.db import Database
from gamas_bot.docx_export import (
    DocumentMeta,
    build_notes_docx,
    resolve_design,
    resolve_fonts,
)
from gamas_bot.jobs import Job, JobQueue
from gamas_bot.metrics import METRICS, peak_rss_bytes
from gamas_bot.notes import structure_transcript
from gamas_bot.transcript import RawTranscript, Word, build_document
from tests.support import make_settings


def _write_synthetic_wav(path: Path, seconds: float = 3.0, rate: int = 16000) -> None:
    total_frames = int(seconds * rate)
    frame_block = b"".join(
        struct.pack("<h", int(9000 * math.sin(2.0 * math.pi * 440.0 * i / rate)))
        for i in range(rate // 10)
    )
    payload = frame_block * max(1, total_frames // (rate // 10))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(payload)


class _UpstreamResponse:
    def __init__(
        self,
        status: int,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = headers or {}

    async def read(self) -> bytes:
        return self._raw

    async def text(self) -> str:
        return self._raw.decode("utf-8", "replace")

    async def json(self, content_type=None):  # noqa: ARG002
        return json.loads(self._raw.decode("utf-8"))


class _ClusterSimulator:
    """Realistic upstream provider simulator enforcing strict upstream concurrency caps."""

    def __init__(
        self,
        *,
        sm_latency: float = 0.90,
        sm_limit: int = 2,
        dg_latency: float = 0.45,
        llm1_latency: float = 0.85,
        llm1_limit: int = 2,
        llm2_latency: float = 0.45,
    ) -> None:
        self.sm_latency = sm_latency
        self.sm_limit = sm_limit
        self.dg_latency = dg_latency
        self.llm1_latency = llm1_latency
        self.llm1_limit = llm1_limit
        self.llm2_latency = llm2_latency

        self.sm_active = 0
        self.llm1_active = 0
        self.counts = {
            "sm_requests": 0,
            "sm_ok": 0,
            "sm_429": 0,
            "dg_requests": 0,
            "dg_ok": 0,
            "llm1_requests": 0,
            "llm1_ok": 0,
            "llm1_429": 0,
            "llm2_requests": 0,
            "llm2_ok": 0,
        }

    async def speechmatics_run(self, _session, _path: Path, _settings) -> RawTranscript:
        self.counts["sm_requests"] += 1
        if self.sm_active >= self.sm_limit:
            self.counts["sm_429"] += 1
            raise stt.STTTransientError(
                "Speechmatics HTTP 429 concurrent job limit",
                retry_after_seconds=60.0,
            )
        self.sm_active += 1
        try:
            await asyncio.sleep(self.sm_latency)
            self.counts["sm_ok"] += 1
            return RawTranscript(
                words=[
                    Word(text="مباحث", start=0.0, end=0.5),
                    Word(text="پیشرفته", start=0.5, end=1.0),
                    Word(text="شبکه", start=1.0, end=1.5),
                ],
                text="مباحث پیشرفته شبکه و سیستم‌های توزیع‌شده در مهندسی کامپیوتر.",
                engine="speechmatics",
            )
        finally:
            self.sm_active -= 1

    async def deepgram_run(self, _session, _path: Path, _settings) -> RawTranscript:
        self.counts["dg_requests"] += 1
        await asyncio.sleep(self.dg_latency)
        self.counts["dg_ok"] += 1
        return RawTranscript(
            words=[
                Word(text="مباحث", start=0.0, end=0.5),
                Word(text="پیشرفته", start=0.5, end=1.0),
                Word(text="شبکه", start=1.0, end=1.5),
            ],
            text="مباحث پیشرفته شبکه و سیستم‌های توزیع‌شده در مهندسی کامپیوتر.",
            engine="deepgram",
        )

    def post(self, url: str, **_kwargs):
        cluster = self

        class _Ctx:
            async def __aenter__(self_inner):
                notes_body = json.dumps(
                    {
                        "title": "جزوهٔ مهندسی سیستم‌های توزیع‌شده",
                        "summary": "مرور جامع معماری هم‌زمانی و تحمل خطا.",
                        "sections": [
                            {
                                "heading": "مفاهیم پایه",
                                "points": ["ایزوله‌سازی کارها", "جایگزینی خودکار سرویس‌ها"],
                                "paragraphs": [
                                    "سیستم‌های توزیع‌شده با جداسازی ظرفیت مراحل از گلوگاه جلوگیری می‌کنند."
                                ],
                            }
                        ],
                        "key_terms": [
                            {
                                "term": "Failover",
                                "definition": "هدایت خودکار ترافیک به سرویس پشتیبان.",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
                if "router.bynara.id" in url:
                    cluster.counts["llm1_requests"] += 1
                    if cluster.llm1_active >= cluster.llm1_limit:
                        cluster.counts["llm1_429"] += 1
                        return _UpstreamResponse(
                            429,
                            {"error": {"message": "Rate limit exceeded"}},
                            headers={"Retry-After": "0.7"},
                        )
                    cluster.llm1_active += 1
                    try:
                        await asyncio.sleep(cluster.llm1_latency)
                        cluster.counts["llm1_ok"] += 1
                    finally:
                        cluster.llm1_active -= 1
                else:
                    cluster.counts["llm2_requests"] += 1
                    await asyncio.sleep(cluster.llm2_latency)
                    cluster.counts["llm2_ok"] += 1
                return _UpstreamResponse(
                    200,
                    {
                        "id": "bench-cmpl",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": notes_body},
                            }
                        ],
                    },
                )

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return _Ctx()


async def run_full_pipeline_benchmark(
    *,
    num_jobs: int = 6,
    max_active_jobs: int = 6,
    max_downloads: int = 3,
    media_concurrency: int = 2,
    max_uploads: int = 3,
    speechmatics_slots: int = 2,
    deepgram_slots: int = 4,
    llm_primary_slots: int = 2,
    llm_fallback_slots: int = 4,
) -> dict[str, object]:
    METRICS.reset()
    stt.reset_cooldowns()
    llm.reset_cooldowns()
    tracemalloc.start()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed_wav = root / "seed.wav"
        _write_synthetic_wav(seed_wav, seconds=3.0)

        settings = make_settings(
            db_path=root / "bench.db",
            temp_dir=root / "work",
            receipt_dir=root / "receipts",
            stt_providers=("speechmatics", "deepgram"),
            speechmatics_api_key="bench-sm",
            deepgram_api_key="bench-dg",
            speechmatics_max_in_flight=speechmatics_slots,
            deepgram_max_in_flight=deepgram_slots,
            note_providers=("nara", "openrouter"),
            nara_api_key="bench-nara",
            nara_base_url="https://router.bynara.id/v1",
            nara_model="openai/gpt-4o-mini",
            openrouter_api_key="bench-or",
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_model="google/gemini-2.5-flash-lite",
            note_hedge_after=0.0,
            note_compile=False,
            llm_primary_max_in_flight=llm_primary_slots,
            llm_fallback_max_in_flight=llm_fallback_slots,
            max_active_jobs=max_active_jobs,
            max_queued_jobs=max(8, num_jobs),
            max_concurrent_downloads=max_downloads,
            media_worker_concurrency=media_concurrency,
            max_concurrent_uploads=max_uploads,
            audio_format_policy="opus",
        )
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        db = Database(settings.db_path)
        await db.connect()

        cluster = _ClusterSimulator()
        dl_sem = asyncio.Semaphore(settings.max_concurrent_downloads)
        media_sem = asyncio.Semaphore(settings.media_worker_concurrency)
        ul_sem = asyncio.Semaphore(settings.max_concurrent_uploads)

        per_job_records: list[dict[str, float]] = []
        done_event = asyncio.Event()

        async def worker(job: Job) -> None:
            t_job = time.monotonic()
            jdir = settings.temp_dir / job.job_id
            jdir.mkdir(parents=True, exist_ok=True)
            try:
                # Stage 1: Download
                job.transition_to("downloading")
                t0 = time.monotonic()
                async with dl_sem:
                    await asyncio.sleep(0.10)
                    src = jdir / "input.wav"
                    await asyncio.to_thread(src.write_bytes, seed_wav.read_bytes())
                t_dl = time.monotonic() - t0

                # Stage 2 & 3: Media probe + PyAV Opus extraction/normalization
                job.transition_to("extracting")
                t0 = time.monotonic()
                async with media_sem:
                    info = await media.probe_media(src, settings)
                    extracted = await media.extract_audio_track(
                        src, jdir, settings, total_duration=info.duration or 3.0
                    )
                t_ext = time.monotonic() - t0

                await db.reserve(job.user_id, job.submission_id, 60)

                # Stage 4: Speechmatics primary -> non-blocking Deepgram overflow
                job.transition_to("transcribing")
                t0 = time.monotonic()
                raw = await stt.transcribe(extracted, settings, session=cluster)  # type: ignore[arg-type]
                doc = build_document(raw)
                t_stt = time.monotonic() - t0

                # Stage 5: Primary LLM -> non-blocking secondary LLM overflow + DOCX
                job.transition_to("generating_notes")
                t0 = time.monotonic()
                notes = await structure_transcript(
                    doc.llm_document(), settings, cluster  # type: ignore[arg-type]
                )
                meta = DocumentMeta(
                    reference=f"GMS-{job.submission_id}",
                    source_name=job.filename,
                    created_at=datetime.now(),
                )
                docx_bytes = await asyncio.to_thread(
                    build_notes_docx,
                    notes,
                    fonts=resolve_fonts(),
                    design=resolve_design(),
                    meta=meta,
                )
                out_docx = jdir / "booklet.docx"
                await asyncio.to_thread(out_docx.write_bytes, docx_bytes)
                t_llm = time.monotonic() - t0

                # Stage 6: Upload + WAL SQLite settlement
                job.transition_to("uploading")
                t0 = time.monotonic()
                async with ul_sem:
                    await asyncio.sleep(0.08)
                await db.save_transcription(
                    job.submission_id, raw.engine, doc.plain(), notes.to_json()
                )
                await db.settle_hold(job.submission_id, 3)
                await db.set_submission_status(job.submission_id, "done", duration_seconds=3.0)
                t_ul = time.monotonic() - t0

                job.transition_to("completed")
                per_job_records.append(
                    {
                        "downloading": t_dl,
                        "extracting": t_ext,
                        "transcribing": t_stt,
                        "generating_notes": t_llm,
                        "uploading": t_ul,
                        "e2e": time.monotonic() - t_job,
                    }
                )
            finally:
                import shutil

                await asyncio.to_thread(shutil.rmtree, jdir, True)
                if len(per_job_records) >= num_jobs:
                    done_event.set()

        orig_sm = stt.PROVIDERS["speechmatics"]
        orig_dg = stt.PROVIDERS["deepgram"]
        stt.PROVIDERS["speechmatics"] = replace(orig_sm, run=cluster.speechmatics_run)
        stt.PROVIDERS["deepgram"] = replace(orig_dg, run=cluster.deepgram_run)
        try:
            queue = JobQueue(
                max_queued=max(8, num_jobs),
                max_active=max_active_jobs,
                worker=worker,
            )
            queue.ensure_workers()
            for idx in range(num_jobs):
                uid = 10_000 + idx
                await db.ensure_user(uid)
                await db.add_credit(uid, 3600, note="bench")
                sid = await db.create_submission(uid, "audio", f"lecture-{idx}.wav")
                queue.submit(
                    Job(
                        submission_id=sid,
                        user_id=uid,
                        kind="audio",
                        filename=f"lecture-{idx}.wav",
                        duration_hint=3.0,
                    )
                )

            t_wall = time.monotonic()
            await asyncio.wait_for(done_event.wait(), timeout=60.0)
            wall_seconds = time.monotonic() - t_wall
            await queue.shutdown()
        finally:
            stt.PROVIDERS["speechmatics"] = orig_sm
            stt.PROVIDERS["deepgram"] = orig_dg
            await db.close()

    _cur_heap, peak_heap = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    keys = ("downloading", "extracting", "transcribing", "generating_notes", "uploading", "e2e")
    mean_stage = {
        k: round(sum(r[k] for r in per_job_records) / len(per_job_records), 4)
        for k in keys
    }
    max_stage = {k: round(max(r[k] for r in per_job_records), 4) for k in keys}
    return {
        "num_jobs": num_jobs,
        "wall_seconds": round(wall_seconds, 4),
        "throughput_jobs_per_sec": round(num_jobs / wall_seconds, 3),
        "peak_python_heap_mb": round(peak_heap / (1024 * 1024), 2),
        "peak_rss_mb": round(peak_rss_bytes() / (1024 * 1024), 2),
        "mean_stage_seconds": mean_stage,
        "max_stage_seconds": max_stage,
        "provider_counts": cluster.counts,
        "metrics": METRICS.snapshot()["providers"],
    }


async def run_burst_provider_benchmark(num_jobs: int = 8) -> dict[str, object]:
    """Direct 8-job concurrent STT + LLM burst matching the pre-change baseline."""
    METRICS.reset()
    stt.reset_cooldowns()
    llm.reset_cooldowns()
    tracemalloc.start()

    cluster = _ClusterSimulator(
        sm_latency=0.32,
        sm_limit=2,
        dg_latency=0.18,
        llm1_latency=0.30,
        llm1_limit=2,
        llm2_latency=0.18,
    )
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "burst.wav"
        _write_synthetic_wav(audio, seconds=1.0)
        settings = make_settings(
            stt_providers=("speechmatics", "deepgram"),
            speechmatics_api_key="bench-sm",
            deepgram_api_key="bench-dg",
            speechmatics_max_in_flight=2,
            deepgram_max_in_flight=6,
            note_providers=("nara", "openrouter"),
            nara_api_key="bench-nara",
            nara_base_url="https://router.bynara.id/v1",
            nara_model="openai/gpt-4o-mini",
            openrouter_api_key="bench-or",
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_model="google/gemini-2.5-flash-lite",
            note_hedge_after=0.0,
            note_compile=False,
            llm_primary_max_in_flight=2,
            llm_fallback_max_in_flight=6,
        )
        orig_sm = stt.PROVIDERS["speechmatics"]
        orig_dg = stt.PROVIDERS["deepgram"]
        stt.PROVIDERS["speechmatics"] = replace(orig_sm, run=cluster.speechmatics_run)
        stt.PROVIDERS["deepgram"] = replace(orig_dg, run=cluster.deepgram_run)
        stt_latencies: list[float] = []
        llm_latencies: list[float] = []

        async def one_job() -> None:
            t0 = time.monotonic()
            raw = await stt.transcribe(audio, settings, session=cluster)  # type: ignore[arg-type]
            stt_latencies.append(time.monotonic() - t0)
            doc = build_document(raw)
            t1 = time.monotonic()
            await structure_transcript(doc.llm_document(), settings, cluster)  # type: ignore[arg-type]
            llm_latencies.append(time.monotonic() - t1)

        try:
            t_start = time.monotonic()
            await asyncio.gather(*(one_job() for _ in range(num_jobs)))
            wall = time.monotonic() - t_start
        finally:
            stt.PROVIDERS["speechmatics"] = orig_sm
            stt.PROVIDERS["deepgram"] = orig_dg

    _cur_heap, peak_heap = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "num_jobs": num_jobs,
        "wall_seconds": round(wall, 4),
        "throughput_jobs_per_sec": round(num_jobs / wall, 3),
        "mean_stt": round(sum(stt_latencies) / len(stt_latencies), 4),
        "max_stt": round(max(stt_latencies), 4),
        "mean_llm": round(sum(llm_latencies) / len(llm_latencies), 4),
        "max_llm": round(max(llm_latencies), 4),
        "peak_python_heap_mb": round(peak_heap / (1024 * 1024), 2),
        "peak_rss_mb": round(peak_rss_bytes() / (1024 * 1024), 2),
        "provider_counts": cluster.counts,
    }


async def _amain() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile",
        nargs="?",
        default="all",
        choices=("all", "cpanel", "high_concurrency", "burst"),
    )
    args = parser.parse_args()
    results: dict[str, object] = {}
    if args.profile in ("all", "cpanel"):
        results["AFTER_CPANEL_DEFAULT_PROFILE"] = await run_full_pipeline_benchmark(
            num_jobs=6,
            max_active_jobs=3,
            max_downloads=2,
            media_concurrency=1,
            max_uploads=2,
            speechmatics_slots=2,
            deepgram_slots=4,
            llm_primary_slots=4,
            llm_fallback_slots=4,
        )
    if args.profile in ("all", "high_concurrency"):
        results["AFTER_STAGE_COORDINATED_MAX_ACTIVE_6"] = await run_full_pipeline_benchmark(
            num_jobs=6,
            max_active_jobs=6,
            max_downloads=3,
            media_concurrency=2,
            max_uploads=3,
            speechmatics_slots=2,
            deepgram_slots=4,
            llm_primary_slots=2,
            llm_fallback_slots=4,
        )
    if args.profile in ("all", "burst"):
        results["AFTER_BURST_8_JOBS"] = await run_burst_provider_benchmark(num_jobs=8)
    print(json.dumps(results, indent=2, ensure_ascii=False))


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
