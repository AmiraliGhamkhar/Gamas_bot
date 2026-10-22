"""Local, offline benchmarks for the three slow layers of the pipeline.

The numbers in ``docs/PERFORMANCE_PLAN.md`` come from this script. It never
touches the network, never reads a user file and never imports a provider key:
it synthesises its own audio and text so a result can be reproduced on any
machine (a cPanel host included) before and after a change.

    python -m scripts.perf_bench audio --minutes 10
    python -m scripts.perf_bench cpu --chars 108000
    python -m scripts.perf_bench prompts --chars 108000
    python -m scripts.perf_bench all

What each group measures:

``audio``
    Decode + normalise cost and output size for every format the media worker
    can produce (16 kHz mono WAV, 48 kHz Opus at several bitrates/compression
    levels, AAC in MP4, FLAC). This is the trade-off behind
    ``choose_merge_format``: the encoder that is twice as fast may cost twice
    the upload, and which one wins depends on the server's egress.
``cpu``
    The note layer's deterministic work (source indexing, the evidence gate,
    transcript splitting and the Word export) on a realistic Persian lecture.
    These are the parts that run on the event loop, so their cost matters even
    though it is small.
``prompts``
    Prompt sizes and chunk counts for a given transcript length: the system
    prompt is re-sent with every map request, so its byte size decides whether
    a provider's prefix cache can help at all.

Every number is wall-clock on *this* machine; treat them as ratios, not as
absolute provider latencies.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import tempfile
import time
import wave
from collections.abc import Callable, Iterable
from pathlib import Path

#: Persian lecture vocabulary for the synthetic transcript (no user data).
WORDS = (
    "سلول غشا انتقال فعال پروتئین انرژی پتانسیل یون سدیم پتاسیم غلظت گرادیان پمپ کانال "
    "دیفیوژن اسمز محلول بیمار درمان دوز سطح خون کبد کلیه مطالعه بالینی"
).split()


def _timed(label: str, action: Callable[[], object], *, note: str = "") -> float:
    started = time.perf_counter()
    action()
    elapsed = time.perf_counter() - started
    suffix = f"  {note}" if note else ""
    print(f"{label:46s} {elapsed:7.3f}s{suffix}")
    return elapsed


def _synthetic_transcript(chars: int) -> str:
    """Build roughly *chars* characters of paragraph-structured Persian text."""
    random.seed(7)
    paragraphs: list[str] = []
    total = 0
    while total < chars:
        sentences = []
        for _ in range(random.randint(3, 6)):
            sentences.append(" ".join(random.choice(WORDS) for _ in range(random.randint(8, 20))))
        paragraph = ". ".join(sentences) + "."
        paragraphs.append(paragraph)
        total += len(paragraph) + 2
    return "\n\n".join(paragraphs)[:chars]


def _synthetic_source(path: Path, minutes: float, rate: int = 48000) -> None:
    """Write a mono 16-bit WAV of white noise (speech-like energy, no numpy)."""
    random.seed(11)
    frames = int(minutes * 60 * rate)
    block = bytearray(random.getrandbits(8) for _ in range(4096))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        written = 0
        while written < frames:
            chunk = min(4096, frames - written)
            handle.writeframes(bytes(block[: chunk * 2]))
            written += chunk


def _encode_aac(source: Path, target: Path) -> None:
    """Re-encode the synthetic WAV as 128 kbit/s AAC in MP4 (a realistic upload)."""
    import av

    with av.open(str(source)) as media, av.open(str(target), "w") as out:
        stream = out.add_stream("aac", rate=48000)
        stream.layout = "mono"
        stream.bit_rate = 128_000
        for frame in media.decode(audio=0):
            frame.pts = None
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)


def bench_audio(minutes: float, workdir: Path) -> None:
    from gamas_bot.media_worker import _transcode

    wav = workdir / "source.wav"
    _timed("build synthetic source", lambda: _synthetic_source(wav, minutes), note=f"{minutes:g} min")
    source = workdir / "source.m4a"
    _timed("encode synthetic m4a (128k)", lambda: _encode_aac(wav, source))
    wav.unlink(missing_ok=True)
    inputs = [source]
    print(f"\nsource: {source.stat().st_size / 1e6:.2f} MB, {minutes:g} min\n")

    cases: list[tuple[str, str, dict]] = [
        ("wav 16k mono pcm_s16le", "out.wav", {"rate": 16000, "codec": "pcm_s16le"}),
        (
            "opus 32k voip cl=5 (shipped)",
            "out.ogg",
            {
                "rate": 48000, "codec": "libopus", "container_format": "ogg",
                "bit_rate": 32000, "codec_options": {"application": "voip", "compression_level": "5"},
            },
        ),
        (
            "opus 32k voip cl=0",
            "out0.ogg",
            {
                "rate": 48000, "codec": "libopus", "container_format": "ogg",
                "bit_rate": 32000, "codec_options": {"application": "voip", "compression_level": "0"},
            },
        ),
        (
            "opus 24k voip cl=5",
            "out24.ogg",
            {
                "rate": 48000, "codec": "libopus", "container_format": "ogg",
                "bit_rate": 24000, "codec_options": {"application": "voip", "compression_level": "5"},
            },
        ),
        (
            "aac 32k m4a 16k rate",
            "out32.m4a",
            {"rate": 16000, "codec": "aac", "container_format": "mp4", "bit_rate": 32000},
        ),
        (
            "aac 48k m4a 16k rate",
            "out48.m4a",
            {"rate": 16000, "codec": "aac", "container_format": "mp4", "bit_rate": 48000},
        ),
        ("flac 16k", "out.flac", {"rate": 16000, "codec": "flac"}),
    ]
    source_seconds = minutes * 60
    print(f"{'format':46s} {'time':>9s} {'size MB':>9s} {'speed':>8s}")
    for label, name, kwargs in cases:
        target = workdir / name
        started = time.perf_counter()
        try:
            _transcode(inputs, target, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a missing encoder is a result too
            print(f"{label:46s}   failed: {type(exc).__name__}: {exc}")
            continue
        elapsed = time.perf_counter() - started
        size = target.stat().st_size / 1e6
        print(f"{label:46s} {elapsed:8.2f}s {size:8.2f}  {source_seconds / elapsed:7.0f}x")
        target.unlink(missing_ok=True)


def bench_cpu(chars: int) -> None:
    from datetime import datetime

    from gamas_bot.docx_export import DocumentMeta, build_notes_docx, resolve_design, resolve_fonts
    from gamas_bot.notes import NoteDefinition, NoteSection, StructuredNotes, gate_notes
    from gamas_bot.notes_evidence import audit_notes, build_source_index, sentence_coverage
    from gamas_bot.transcript import split_transcript

    text = _synthetic_transcript(chars)
    print(f"synthetic transcript: {len(text)} chars (~{len(text) / 900:.0f} min of speech)\n")
    _timed("build_source_index", lambda: build_source_index(text))
    _timed("build_source_index (cached)", lambda: build_source_index(text))
    _timed("split_transcript(64k)", lambda: split_transcript(text, 64000))

    notes = StructuredNotes(
        title="جزوهٔ آزمایشی",
        summary=" ".join(random.choice(WORDS) for _ in range(40)) + ".",
        sections=tuple(
            NoteSection(
                heading=f"بخش {index} — غشا و انتقال",
                paragraphs=(
                    " ".join(random.choice(WORDS) for _ in range(60)) + ".",
                    " ".join(random.choice(WORDS) for _ in range(60)) + ".",
                ),
                bullets=(" ".join(random.choice(WORDS) for _ in range(10)) + ".",),
                definitions=(
                    NoteDefinition(term="پمپ سدیم", definition="انتقال فعال یون", term_en="Na/K pump"),
                ),
            )
            for index in range(30)
        ),
    )
    _timed("gate_notes (validate + audit + coverage)", lambda: gate_notes(notes, text, strict=True, mode="full"))
    _timed("audit_notes", lambda: audit_notes(text, notes))
    _timed("sentence_coverage", lambda: sentence_coverage(text, notes))
    meta = DocumentMeta(reference="GMS-0", source_name="bench.m4a", created_at=datetime.now())
    started = time.perf_counter()
    payload = build_notes_docx(notes, fonts=resolve_fonts(), design=resolve_design(), meta=meta)
    print(f"{'build_notes_docx (30 sections)':46s} {time.perf_counter() - started:7.3f}s"
          f"  {len(payload) // 1024} KB")
    _timed("json.dumps(compact payload)", lambda: json.dumps(str(notes), ensure_ascii=False))


def bench_prompts(chars: int) -> None:
    from gamas_bot.notes import (
        CHUNK_CONTEXT_RESERVE_CHARS,
        build_compile_system_prompt,
        build_system_prompt,
        effective_chunk_chars,
        effective_compile_max_chars,
    )
    from gamas_bot.transcript import split_transcript

    text = _synthetic_transcript(chars)
    try:
        from tests.support import make_settings
    except ImportError:  # pragma: no cover - tests are not shipped with a wheel
        print("tests.support is unavailable; run from a source checkout.")
        return
    settings = make_settings()
    system = build_system_prompt("full")
    contextual = build_system_prompt("full", context_block="x" * CHUNK_CONTEXT_RESERVE_CHARS)
    print(f"transcript: {len(text)} chars (~{len(text) / 900:.0f} min of speech)")
    print(f"NOTE_CHUNK_CHARS={settings.note_chunk_chars}  NOTE_CONCURRENCY={settings.note_concurrency}")
    print(f"NOTE_CONTEXT_TOKENS={settings.note_context_tokens}  NOTE_MAX_OUTPUT_TOKENS={settings.note_max_output_tokens}")
    print(f"\n{'system prompt (stable rules)':46s} {len(system):6d} chars  ~{len(system) / 2:5.0f} tokens")
    print(f"{'system prompt + chunk context':46s} {len(contextual):6d} chars  ~{len(contextual) / 2:5.0f} tokens")
    print(f"{'compile system prompt':46s} {len(build_compile_system_prompt('full')):6d} chars")
    chunk_chars = effective_chunk_chars(settings, system_prompt=system)
    with_context = effective_chunk_chars(settings, system_prompt=system, extra_reserve_chars=CHUNK_CONTEXT_RESERVE_CHARS)
    chunks = split_transcript(text, with_context)
    print(f"\neffective_chunk_chars={chunk_chars}  with context={with_context}")
    print(f"chunks={len(chunks)}  sizes={[len(chunk) for chunk in chunks]}")
    rounds = math.ceil(len(chunks) / max(1, settings.note_concurrency))
    print(f"map rounds at NOTE_CONCURRENCY={settings.note_concurrency}: {rounds}")
    print(f"compile budget (chars): {effective_compile_max_chars(settings)}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="perf_bench", description=__doc__.splitlines()[0])
    parser.add_argument("group", choices=("audio", "cpu", "prompts", "all"))
    parser.add_argument("--minutes", type=float, default=10.0, help="audio length to synthesise")
    parser.add_argument("--chars", type=int, default=108000, help="transcript length (~2h of Persian)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary media files")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.group in ("cpu", "prompts", "all"):
        print("=== notes layer (deterministic CPU) ===")
        bench_cpu(args.chars)
        print()
    if args.group in ("prompts", "all"):
        print("=== prompt sizing ===")
        bench_prompts(args.chars)
        print()
    if args.group in ("audio", "all"):
        print("=== media layer (decode + normalise) ===")
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            bench_audio(args.minutes, workdir)
            if args.keep:
                print(f"kept files in {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
