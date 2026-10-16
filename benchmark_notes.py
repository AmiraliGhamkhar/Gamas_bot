#!/usr/bin/env python3
"""Benchmark note generation performance."""

import asyncio
import json
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from tests.support import make_settings
from tests.test_notes import FakeSession, big_notes_json
from gamas_bot.notes import structure_transcript
from gamas_bot.transcript import split_transcript


def create_sample_notes_json(title: str) -> str:
    return json.dumps({
        "title": title,
        "summary": "خلاصه‌ای از متن ارائه شده",
        "sections": [
            {
                "heading": "مقدمه",
                "paragraphs": ["متن پاراگراف اول.", "متن پاراگراف دوم."],
                "callouts": [],
            },
            {
                "heading": "مبحث اول",
                "paragraphs": ["توضیحات مبحث اول."],
                "callouts": [{"type": "note", "text": "نکته مهم"}],
            },
        ],
    }, ensure_ascii=False)


async def benchmark_single_chunk():
    """Benchmark single chunk (fastest path)."""
    settings = make_settings(note_chunk_chars=100000)
    document = "یک متن نسبتاً کوتاه برای تست عملکرد جزوه‌نویسی."

    times = []
    for i in range(5):
        session = FakeSession([create_sample_notes_json("تست")])
        start = time.perf_counter()
        await structure_transcript(document, settings, session)
        elapsed = time.perf_counter() - start
        times.append(elapsed)

    avg = sum(times) / len(times)
    print(f"Single chunk: avg={avg*1000:.1f}ms over {len(times)} runs")
    return avg


async def benchmark_multi_chunk():
    """Benchmark multi-chunk map-reduce (no compile)."""
    settings = make_settings(note_chunk_chars=400, note_compile=False)
    document = "\n\n".join(
        f"موضوع {index}: " + "جمله‌های بسیار زیاد برای پر کردن حجم. " * 20
        for index in range(6)
    )
    chunks = split_transcript(document, 400)

    times = []
    for i in range(5):
        # note_compile=False skips compile, so only chunks calls needed
        session = FakeSession(
            [big_notes_json(f"جزوهٔ {j}") for j in range(len(chunks))]
        )
        start = time.perf_counter()
        await structure_transcript(document, settings, session)
        elapsed = time.perf_counter() - start
        times.append(elapsed)

    avg = sum(times) / len(times)
    print(f"Multi-chunk ({len(chunks)} chunks, no compile): avg={avg*1000:.1f}ms over {len(times)} runs")
    return avg


async def benchmark_large_document():
    """Benchmark large document with compile phase."""
    settings = make_settings(note_chunk_chars=300, note_compile=True)
    document = "\n\n".join(
        f"موضوع {index}: " + "این یک متن طولانی برای تست عملکرد است. " * 30
        for index in range(12)
    )
    chunks = split_transcript(document, 300)

    compile_answer = {
        "title": "جزوهٔ نهایی",
        "summary": "خلاصه",
        "sections": [
            {"heading": f"بخش {i}", "paragraphs": ["متن."]}
            for i in range(len(chunks))
        ],
    }

    # Check if compile will run based on merged_chars
    from gamas_bot.notes import compact_notes_payload, COMPILE_MIN_NOTES_CHARS, COMPILE_MAX_CHARS
    merged_notes = {"title": "test", "summary": "test", "sections": [{"heading": f"S{i}", "paragraphs": ["p"]} for i in range(len(chunks))]}
    merged_chars = len(json.dumps(merged_notes, ensure_ascii=False))
    will_compile = settings.note_compile and COMPILE_MIN_NOTES_CHARS <= merged_chars <= COMPILE_MAX_CHARS

    times = []
    responses_needed = len(chunks) + (1 if will_compile else 0)

    for i in range(3):
        responses = [big_notes_json(f"جزوهٔ {j}") for j in range(len(chunks))]
        if will_compile:
            responses.append(json.dumps(compile_answer, ensure_ascii=False))
        session = FakeSession(responses)
        start = time.perf_counter()
        await structure_transcript(document, settings, session)
        elapsed = time.perf_counter() - start
        times.append(elapsed)

    avg = sum(times) / len(times)
    mode = "with compile" if will_compile else "no compile"
    print(f"Large ({len(chunks)} chunks, {mode}): avg={avg*1000:.1f}ms over {len(times)} runs")
    return avg


async def main():
    print("Note Generation Benchmark")
    print("=" * 40)

    await benchmark_single_chunk()
    await benchmark_multi_chunk()
    await benchmark_large_document()

    print("=" * 40)
    print("Note: These are mock timings (no actual LLM calls)")
    print("Real-world times depend on provider latency")


if __name__ == "__main__":
    asyncio.run(main())