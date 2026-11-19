"""Offline booklet comparison with reproducible source, replies and wire faults.

Run the same absolute script path with PYTHONPATH pointing at each checkout.
This measures app/transport overhead and retention under a scripted provider,
not a model's writing quality, production latency or provider invoices.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import logging
import statistics
import tempfile
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from gamas_bot import llm, notes
from gamas_bot.artifact_validation import validate_docx_artifact, validate_pdf_artifact
from gamas_bot.docx_export import DocumentMeta, build_notes_docx
from gamas_bot.metrics import peak_rss_bytes
from gamas_bot.notes_pdf import build_notes_pdf
from tests.support import make_settings

TOPICS = (
    ("چرخهٔ آب", "تبخیر و میعان", "تبخیر: تبدیل آب مایع به بخار است.", (
        "در ابتدای آزمایش دمای آب برابر 25 °C ثبت شد.",
        "گرمای محیط حرکت مولکول‌ها را افزایش می‌دهد و تبخیر آسان‌تر می‌شود.",
        "ترتیب مشاهده: ابتدا ظرف را گرم کنید، سپس تشکیل بخار را ثبت کنید.",
    )),
    ("اندازه‌گیری خاک", "جدول آزمایش", "رطوبت خاک: مقدار آب موجود در خاک است.", (
        "در میانهٔ آزمایش جرم خاک برابر 125 g ثبت شد.",
        "برای مقایسهٔ دو ظرف، جرم هر ظرف را پیش از افزودن خاک ثبت کنید.",
        "یادداشت غیرآموزشی: این خط عمداً بخشی از متن منبع است و باید کامل باقی بماند.",
    )),
    ("جریان رودخانه", "سرعت جریان", "رسوب‌گذاری: انباشت ذرات حمل‌شده به وسیلهٔ جریان است.", (
        "با کاهش سرعت آب، جریان ذرات درشت را در بستر باقی می‌گذارد.",
        "برای مرور، رابطهٔ کاهش سرعت و رسوب‌گذاری را توضیح دهید.",
        "در انتهای آزمایش طول مسیر جریان برابر 32 cm گزارش شد.",
    )),
)
HEADERS = ["نمونه", "جرم"]
ROWS = [["خاک خشک", "125 g"], ["خاک مرطوب", "180 g"]]
TABLE = "| نمونه | جرم |\n| --- | --- |\n| خاک خشک | 125 g |\n| خاک مرطوب | 180 g |"
REPEAT = "اندازه‌گیری در شرایط یکسان امکان مقایسهٔ مشاهده‌ها را فراهم می‌کند."
MISSING = TOPICS[0][-1][0]
STAGE: contextvars.ContextVar[str] = contextvars.ContextVar("bench_stage", default="unspecified")


def lesson(repeats: int) -> str:
    padding = "\n".join([REPEAT] * repeats)
    a, b, c = TOPICS
    return (
        f"# {a[0]}\n\n{a[3][0]}\n\n## {a[1]}\n\n{a[2]}\n{a[3][1]}\n{a[3][2]}\n\n{padding}\n\n"
        f"# {b[0]}\n\n{padding}\n\n## {b[1]}\n\n{b[2]}\n" + "\n".join(b[3]) + f"\n\n{TABLE}\n\n"
        f"# {c[0]}\n\n{padding}\n\n## {c[1]}\n\n{c[2]}\n" + "\n".join(c[3])
    )


def sections_for(source: str, *, omit: bool = False) -> list[dict]:
    sections = []
    for heading, _, definition, facts in TOPICS:
        term, explanation = definition.split(": ", 1)
        paragraphs = [line for line in facts if line in source and (not omit or line != MISSING)]
        entry: dict = {"heading": heading, "paragraphs": paragraphs}
        if definition in source:
            entry["definitions"] = [{"term": term, "definition": explanation}]
        if heading == TOPICS[1][0] and all(" | ".join(row) in source for row in ROWS):
            entry["table"] = {"headers": HEADERS, "rows": ROWS}
        if paragraphs or "definitions" in entry or "table" in entry:
            sections.append(entry)
    if not sections:
        sections.append({"heading": "اصول اندازه‌گیری", "paragraphs": [REPEAT]})
    return sections


def quote_set(booklet) -> set[str]:
    return {paragraph for section in booklet.sections for paragraph in section.paragraphs}


def quality(booklet) -> dict:
    """Objective source-key checks, not scores of learner outcomes or prose style."""
    quotes = quote_set(booklet)
    expected = [fact for topic in TOPICS for fact in topic[3]]
    definitions = {(item.term, item.definition) for section in booklet.sections for item in section.definitions}
    rows = {tuple(row) for section in booklet.sections for table in section.all_tables for row in table.rows}
    headings = [section.heading for section in booklet.sections]
    plain = [paragraph for section in booklet.sections for paragraph in section.paragraphs]
    return {
        "topics": sum(heading in headings for heading, *_ in TOPICS),
        "facts": sum(fact in quotes for fact in expected),
        "definitions": sum(tuple(item[2].split(": ", 1)) in definitions for item in TOPICS),
        "table_rows": sum(tuple(row) in rows for row in ROWS),
        "measurement_sentences": sum(fact in quotes for fact in (TOPICS[0][3][0], TOPICS[1][3][0], TOPICS[2][3][-1])),
        "procedure_and_explanations": sum(fact in quotes for fact in (TOPICS[0][3][1], TOPICS[0][3][2], TOPICS[2][3][0])),
        "duplicate_paragraphs": len(plain) - len(quotes),
        "review_prompt": TOPICS[2][3][1] in quotes,
        "renderable": notes.structurally_valid_booklet(booklet),
        "verification_passed": getattr(getattr(booklet, "verification_report", None), "passed", None),
    }


class WireLoad:
    def __init__(self) -> None:
        self.active: Counter[str] = Counter()
        self.peak: Counter[str] = Counter()
        self.total_peak = 0
        self.requests: Counter[str] = Counter()

    def enter(self, provider: str) -> None:
        self.active[provider] += 1
        self.requests[provider] += 1
        self.peak[provider] = max(self.peak[provider], self.active[provider])
        self.total_peak = max(self.total_peak, sum(self.active.values()))

    def leave(self, provider: str) -> None:
        self.active[provider] -= 1


class ScriptedSession:
    def __init__(self, source: str, load: WireLoad, *, fault: bool, delay: float = 0.004) -> None:
        self.source = source
        self.fault = fault
        self.fault_target: str | None = None
        self.load = load
        self.delay = delay
        self.usage: Counter[tuple[str, str]] = Counter()
        self.errors: Counter[str] = Counter()
        self.active = self.peak = 0
        self.tries: Counter[tuple[str, str, str]] = Counter()

    def post(self, url, *, json: dict, **_kwargs):
        session = self
        user = json["messages"][-1]["content"]
        stage = STAGE.get()
        provider = "avalai" if "avalai" in url else "openrouter"
        key = (stage, provider, user)

        class Response:
            headers: dict[str, str] = {}
            status = 200

            async def __aenter__(self):
                session.active += 1
                session.load.enter(provider)
                session.peak = max(session.peak, session.active)
                session.tries[key] += 1
                session.usage[stage, "requests"] += 1
                session.usage[stage, "retries"] += int(session.tries[key] > 1)
                # Common estimate across revisions, not actual hosted tokenizer usage.
                session.usage[stage, "input_est"] += llm.estimate_tokens(json_module(json), chars_per_token=1.5)
                try:
                    await asyncio.sleep(session.delay)
                except BaseException:
                    session.active -= 1
                    session.load.leave(provider)
                    raise
                if session.fault and stage == "BOOKLET_GENERATION" and provider == "avalai":
                    if session.fault_target is None:
                        session.fault_target = user
                    if user == session.fault_target and session.tries[key] <= 2:
                        self.status = 503
                        session.errors[provider] += 1
                        self.payload = {"error": {"message": "scripted 503"}}
                        return self
                if stage == "BOOKLET_COMPILE":
                    if user.startswith('{"level":'):
                        reply = {"order": [row["id"] for row in json_load(user)["sections"]]}
                    else:
                        # Legacy editor returned a whole booklet rather than an ID manifest.
                        reply = {"title": "مرور علمی چرخهٔ آب", "sections": sections_for(session.source, omit=session.fault)}
                elif stage == "BOOKLET_VERIFY":
                    reply = {"verdicts": [{"id": row["id"], "verdict": "unclear"} for row in json_load(user)["claims"]]}
                else:
                    reply = {"title": "مرور علمی چرخهٔ آب", "sections": sections_for(user, omit=session.fault)}
                text = json_module(reply)
                session.usage[stage, "output_est"] += llm.estimate_tokens(text, chars_per_token=1.5)
                self.payload = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
                return self

            async def __aexit__(self, *_args):
                session.active -= 1
                session.load.leave(provider)

            async def read(self):
                return json_module(self.payload).encode()

            async def json(self, **_kwargs):
                return self.payload

        return Response()


def json_module(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


json_load = json.loads


async def run(*, repeats: int, jobs: int, compile_enabled: bool, fault: bool) -> dict:
    source = lesson(repeats)
    original_dispatch = llm._dispatch_across_routes

    async def record_stage(*args, **kwargs):
        scope = STAGE.set(kwargs.get("task", "unspecified"))
        try:
            return await original_dispatch(*args, **kwargs)
        finally:
            STAGE.reset(scope)

    with tempfile.TemporaryDirectory() as directory:
        settings = make_settings(
            db_path=Path(directory) / "bench.sqlite", note_compile=compile_enabled, note_chunk_chars=10_000,
            avalai_api_key="synthetic-primary", openrouter_api_key="synthetic-fallback",
            note_providers=("avalai", "openrouter"), note_strict_grounding=False, note_completeness_fill=fault,
            note_structured_output=True, note_ttfb_timeout=0, note_concurrency=3,
            llm_primary_max_in_flight=3, llm_fallback_max_in_flight=1,
            avalai_max_in_flight=3, openrouter_max_in_flight=1,
            openrouter_rpm_limit=0, openrouter_rpd_limit=0,
        )
        load = WireLoad()
        sessions = [ScriptedSession(source, load, fault=fault) for _ in range(jobs)]

        async def job(session):
            t = time.perf_counter()
            booklet = await notes.structure_transcript(source, settings, session)
            generated = time.perf_counter()
            meta = DocumentMeta(reference="PHASE8")
            pdf, docx = await asyncio.gather(asyncio.to_thread(build_notes_pdf, booklet, meta=meta),
                                            asyncio.to_thread(build_notes_docx, booklet, meta=meta))
            validate_pdf_artifact(pdf)
            validate_docx_artifact(docx)
            return (time.perf_counter() - t, generated - t, quality(booklet), booklet.quality_state)

        start = time.perf_counter()
        with patch.object(llm, "_dispatch_across_routes", record_stage), patch.object(llm, "_retry_delay_seconds", return_value=0):
            results = await asyncio.gather(*(job(session) for session in sessions))
        elapsed = time.perf_counter() - start
    usage = sum((session.usage for session in sessions), Counter())
    stages = {stage: {metric: usage[stage, metric] for metric in ("requests", "retries", "input_est", "output_est")}
              for stage in sorted({key[0] for key in usage})}
    return {
        "jobs": jobs, "avg_source_chars": len(source), "compile": compile_enabled, "scripted_fault": fault,
        "stages": stages, "total_requests": sum(row["requests"] for row in stages.values()),
        "http_failures": dict(sum((session.errors for session in sessions), Counter())),
        "repair_jobs": sum(session.usage["BOOKLET_REPAIR", "requests"] > 0 for session in sessions),
        "wall_seconds": round(elapsed, 4), "median_job_seconds": round(statistics.median(row[0] for row in results), 4),
        "median_generation_seconds": round(statistics.median(row[1] for row in results), 4),
        "peak_rss_mib": round(peak_rss_bytes() / (1024**2), 1),
        "max_inflight_in_single_job": max(session.peak for session in sessions),
        "max_inflight_total": load.total_peak, "max_inflight_per_route": dict(load.peak),
        "requests_per_route": dict(load.requests),
        "quality": [row[2] for row in results], "states": [row[3] for row in results],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=250)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--fault", action="store_true")
    args = parser.parse_args()
    if args.jobs < 1 or args.repeats < 1:
        parser.error("--jobs and --repeats must be positive")
    logging.basicConfig(level=logging.ERROR)
    print(json_module(asyncio.run(run(repeats=args.repeats, jobs=args.jobs, compile_enabled=args.compile, fault=args.fault))))
