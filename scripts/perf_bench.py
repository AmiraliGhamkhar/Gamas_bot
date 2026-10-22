"""Local, offline benchmarks for the three slow layers of the pipeline.

Every number in ``docs/PERFORMANCE_PLAN.md`` comes from this script. It never
touches the network, never reads a user file and never needs a provider key: it
synthesises its own audio and Persian text, so a result can be reproduced on any
machine (a cPanel host included) before and after a change.

    python -m scripts.perf_bench audio  --minutes 10     # format/encoder matrix
    python -m scripts.perf_bench frames --minutes 10     # where transcode time goes
    python -m scripts.perf_bench cpu    --chars 108000   # note layer deterministic work
    python -m scripts.perf_bench json                    # STT response parsing
    python -m scripts.perf_bench prompts --chars 108000  # prompt sizes and chunk counts
    python -m scripts.perf_bench all

What each group answers:

``audio``
    Cost and output size for every format the media worker can produce
    (16 kHz mono WAV, AAC in MP4 with both coders, Opus at several
    bitrates/compression levels/applications, FLAC). This is the trade-off
    behind ``media.choose_merge_format``: the fastest encoder may cost twice the
    upload, and which one wins depends on the server's egress.
``frames``
    The same pipeline broken into its stages (decode -> resample -> encode) with
    the muxer replaced by a null sink, so it is clear whether a slow transcode is
    the decoder, the sample-rate converter or the encoder.
``cpu``
    The note layer's deterministic work (source indexing, the evidence gate,
    transcript splitting, Word export) on a realistic Persian lecture, at two
    lecture lengths so scaling is visible. These run on the event loop, so their
    cost matters even though it is small.
``json``
    Reading and parsing a provider transcript response of realistic size: the
    bounded reader's chunk accumulation, ``orjson`` vs the standard library, and
    building the ``Word`` list the paragraphing engine consumes.
``prompts``
    Prompt sizes and chunk counts for a given transcript length. The system
    prompt is re-sent with every map request, so its byte size decides whether a
    provider's prefix cache can help at all.

Every number is wall-clock on *this* machine; treat them as ratios, not as
absolute provider latencies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import tempfile
import time
import wave
from collections.abc import Iterable
from pathlib import Path
from typing import Callable

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
        sentences = [
            " ".join(random.choice(WORDS) for _ in range(random.randint(8, 20)))
            for _ in range(random.randint(3, 6))
        ]
        paragraph = ". ".join(sentences) + "."
        paragraphs.append(paragraph)
        total += len(paragraph) + 2
    return "\n\n".join(paragraphs)[:chars]


def _synthetic_source(path: Path, minutes: float, rate: int = 48000) -> None:
    """Write a mono 16-bit WAV of white noise (speech-like energy, no numpy)."""
    random.seed(11)
    frames = int(minutes * 60 * rate)
    block = bytearray(random.getrandbits(8) for _ in range(8192))
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


#: Format matrix: (label, filename, transcode kwargs). Sizes are what the STT
#: provider receives, so they are quoted per audio hour in the plan.
AUDIO_CASES: tuple[tuple[str, str, dict], ...] = (
    ("wav 16k mono pcm_s16le", "out.wav", {"rate": 16000, "codec": "pcm_s16le"}),
    (
        "aac 48k mp4 (coder=twoloop, default)",
        "out.m4a",
        {"rate": 16000, "codec": "aac", "container_format": "mp4", "bit_rate": 48000},
    ),
    (
        "aac 48k mp4 (coder=fast)",
        "fast48.m4a",
        {
            "rate": 16000, "codec": "aac", "container_format": "mp4", "bit_rate": 48000,
            "codec_options": {"aac_coder": "fast"},
        },
    ),
    (
        "aac 32k mp4 (coder=fast)",
        "fast32.m4a",
        {
            "rate": 16000, "codec": "aac", "container_format": "mp4", "bit_rate": 32000,
            "codec_options": {"aac_coder": "fast"},
        },
    ),
    (
        "opus 32k voip cl=5 (shipped)",
        "out.ogg",
        {
            "rate": 48000, "codec": "libopus", "container_format": "ogg",
            "bit_rate": 32000,
            "codec_options": {"application": "voip", "compression_level": "5"},
        },
    ),
    (
        "opus 32k voip cl=0",
        "v0.ogg",
        {
            "rate": 48000, "codec": "libopus", "container_format": "ogg",
            "bit_rate": 32000,
            "codec_options": {"application": "voip", "compression_level": "0"},
        },
    ),
    (
        "opus 32k audio cl=0",
        "a0.ogg",
        {
            "rate": 48000, "codec": "libopus", "container_format": "ogg",
            "bit_rate": 32000,
            "codec_options": {"application": "audio", "compression_level": "0"},
        },
    ),
    (
        "opus 24k voip cl=0",
        "v24.ogg",
        {
            "rate": 48000, "codec": "libopus", "container_format": "ogg",
            "bit_rate": 24000,
            "codec_options": {"application": "voip", "compression_level": "0"},
        },
    ),
    (
        "opus 16k voip cl=0",
        "v16.ogg",
        {
            "rate": 48000, "codec": "libopus", "container_format": "ogg",
            "bit_rate": 16000,
            "codec_options": {"application": "voip", "compression_level": "0"},
        },
    ),
    ("flac 16k", "out.flac", {"rate": 16000, "codec": "flac"}),
)


def bench_audio(minutes: float, workdir: Path) -> None:
    from gamas_bot.media_worker import _transcode

    wav = workdir / "source.wav"
    _timed("build synthetic source", lambda: _synthetic_source(wav, minutes), note=f"{minutes:g} min")
    source = workdir / "source.m4a"
    _timed("encode synthetic m4a (128k)", lambda: _encode_aac(wav, source))
    wav.unlink(missing_ok=True)
    source_seconds = minutes * 60
    print(f"\nsource: {source.stat().st_size / 1e6:.2f} MB, {minutes:g} min")
    print(f"\n{'format':40s} {'time':>9s} {'size MB':>9s} {'speed':>8s} {'per hour':>10s}")
    for label, name, kwargs in AUDIO_CASES:
        target = workdir / name
        started = time.perf_counter()
        try:
            _transcode([source], target, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a missing encoder is a result too
            print(f"{label:40s}   failed: {type(exc).__name__}: {exc}")
            continue
        elapsed = time.perf_counter() - started
        size = target.stat().st_size / 1e6
        per_hour = size * (3600 / source_seconds)
        print(
            f"{label:40s} {elapsed:8.2f}s {size:8.2f}  {source_seconds / elapsed:7.0f}x"
            f" {per_hour:8.1f}MB"
        )
        target.unlink(missing_ok=True)


def bench_frames(minutes: float, workdir: Path) -> None:
    """Decode -> resample -> encode, with a null muxer to isolate each stage."""
    import av
    from av.audio.resampler import AudioResampler

    from gamas_bot.media_worker import INPUT_PROTOCOL_OPTIONS

    source = workdir / "frames.m4a"
    _synthetic_source(workdir / "tmp.wav", minutes)
    _encode_aac(workdir / "tmp.wav", source)
    (workdir / "tmp.wav").unlink(missing_ok=True)
    seconds = minutes * 60
    print(f"source: {source.stat().st_size / 1e6:.2f} MB, {minutes:g} min\n")

    def decode_only() -> None:
        with av.open(str(source), options=dict(INPUT_PROTOCOL_OPTIONS)) as container:
            for _frame in container.decode(container.streams.audio[0]):
                pass

    def decode_and_resample() -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=16000)
        with av.open(str(source), options=dict(INPUT_PROTOCOL_OPTIONS)) as container:
            for frame in container.decode(container.streams.audio[0]):
                for _converted in resampler.resample(frame):
                    pass
        for _converted in resampler.resample(None):
            pass

    def encode(codec: str, rate: int, *, bit_rate: int | None = None, options: dict | None = None) -> None:
        from fractions import Fraction

        container = av.open(str(workdir / "null.out"), "w", format="null")
        try:
            stream = container.add_stream(codec, rate=rate)
            stream.layout = "mono"
            if bit_rate is not None:
                stream.bit_rate = bit_rate
            if options:
                stream.codec_context.options = dict(options)
            resampler = AudioResampler(format="s16", layout="mono", rate=rate)
            time_base = Fraction(1, rate)
            next_pts = 0
            with av.open(str(source), options=dict(INPUT_PROTOCOL_OPTIONS)) as media:
                for frame in media.decode(media.streams.audio[0]):
                    for converted in resampler.resample(frame):
                        converted.time_base = time_base
                        converted.pts = next_pts
                        next_pts += converted.samples
                        for packet in stream.encode(converted):
                            container.mux(packet)
            for converted in resampler.resample(None):
                converted.time_base = time_base
                converted.pts = next_pts
                next_pts += converted.samples
                for packet in stream.encode(converted):
                    container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        finally:
            container.close()

    cases: tuple[tuple[str, Callable[[], None]], ...] = (
        ("decode only (aac -> frames)", decode_only),
        ("decode + resample to 16k mono", decode_and_resample),
        ("decode + resample + pcm_s16le", lambda: encode("pcm_s16le", 16000)),
        ("decode + resample + aac 48k (twoloop)", lambda: encode("aac", 16000, bit_rate=48000)),
        (
            "decode + resample + aac 48k (fast)",
            lambda: encode("aac", 16000, bit_rate=48000, options={"aac_coder": "fast"}),
        ),
        (
            "decode + resample + opus 32k cl=5",
            lambda: encode(
                "libopus", 48000, bit_rate=32000,
                options={"application": "voip", "compression_level": "5"},
            ),
        ),
        (
            "decode + resample + opus 32k cl=0 audio",
            lambda: encode(
                "libopus", 48000, bit_rate=32000,
                options={"application": "audio", "compression_level": "0"},
            ),
        ),
    )
    print(f"{'stage':46s} {'time':>9s} {'speed':>8s}")
    for label, action in cases:
        started = time.perf_counter()
        action()
        elapsed = time.perf_counter() - started
        print(f"{label:46s} {elapsed:8.2f}s {seconds / elapsed:7.0f}x")


def bench_cpu(chars: int) -> None:
    from datetime import datetime

    from gamas_bot.docx_export import DocumentMeta, build_notes_docx, resolve_design, resolve_fonts
    from gamas_bot.notes import NoteDefinition, NoteSection, StructuredNotes, gate_notes
    from gamas_bot.notes_evidence import audit_notes, build_source_index, sentence_coverage
    from gamas_bot.transcript import RawTranscript, Word, build_document, split_transcript

    for size in (chars, chars * 3):
        text = _synthetic_transcript(size)
        print(f"\n--- {len(text)} chars (~{len(text) / 900:.0f} min of speech) ---")
        _timed("build_source_index", lambda text=text: build_source_index(text))
        _timed("build_source_index (cached, lru)", lambda text=text: build_source_index(text))
        _timed("split_transcript(64k)", lambda text=text: split_transcript(text, 64000))
        # Timed words: what a provider with word timings actually returns.
        count = int(len(text) / 6)
        words: list[Word] = []
        clock = 0.0
        for _ in range(count):
            duration = random.uniform(0.16, 0.55)
            words.append(Word(random.choice(WORDS), round(clock, 2), round(clock + duration, 2)))
            clock += duration + (random.uniform(0.05, 0.4) if random.random() < 0.15 else 0.0)
        raw = RawTranscript(words=words, text=text, confidence=None, engine="bench")
        _timed(f"build_document ({len(words)} timed words)", lambda raw=raw: build_document(raw))
        notes = StructuredNotes(
            title="جزوهٔ آزمایشی",
            summary=" ".join(random.choice(WORDS) for _ in range(40)) + ".",
            sections=tuple(
                NoteSection(
                    heading=f"بخش {index} — غشا و انتقال",
                    paragraphs=(" ".join(random.choice(WORDS) for _ in range(60)) + ".",),
                    bullets=(" ".join(random.choice(WORDS) for _ in range(10)) + ".",),
                    definitions=(
                        NoteDefinition(
                            term="پمپ سدیم", definition="انتقال فعال یون", term_en="Na/K pump"
                        ),
                    ),
                )
                for index in range(30)
            ),
        )
        _timed(
            "gate_notes (validate + audit + coverage)",
            lambda notes=notes, text=text: gate_notes(notes, text, strict=True, mode="full"),
        )
        _timed("audit_notes", lambda notes=notes, text=text: audit_notes(text, notes))
        _timed("sentence_coverage", lambda notes=notes, text=text: sentence_coverage(text, notes))
        meta = DocumentMeta(reference="GMS-0", source_name="bench.m4a", created_at=datetime.now())
        started = time.perf_counter()
        payload = build_notes_docx(notes, fonts=resolve_fonts(), design=resolve_design(), meta=meta)
        print(
            f"{'build_notes_docx (30 sections)':46s} {time.perf_counter() - started:7.3f}s"
            f"  {len(payload) // 1024} KB"
        )


def bench_json(minutes: float = 120) -> None:
    """Provider-response reading/parsing cost at a realistic transcript size."""
    from gamas_bot.stt import _deepgram_transcript, _read_bounded

    words = int(minutes * 60 * 2.5)
    payload = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": " ".join(random.choice(WORDS) for _ in range(words)),
                            "confidence": 0.94,
                            "words": [
                                {
                                    "word": random.choice(WORDS),
                                    "punctuated_word": random.choice(WORDS),
                                    "start": index * 0.4,
                                    "end": index * 0.4 + 0.3,
                                    "confidence": 0.9123,
                                }
                                for index in range(words)
                            ],
                        }
                    ]
                }
            ]
        },
        "metadata": {"duration": minutes * 60},
    }
    blob = json.dumps(payload).encode()
    print(f"response: {len(blob) / 1e6:.1f} MB, {words} words (~{minutes:g} min)\n")
    _timed(f"json.loads ({len(blob) / 1e6:.1f} MB)", lambda: json.loads(blob))
    try:
        import orjson
    except ImportError:  # pragma: no cover - optional dependency
        print("orjson is not installed; the shipped fast path is unavailable")
    else:
        _timed(f"orjson.loads ({len(blob) / 1e6:.1f} MB)", lambda: orjson.loads(blob))
    _timed("_deepgram_transcript (build Word list)", lambda: _deepgram_transcript(payload))

    class _Content:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._position = 0

        async def read(self, size: int) -> bytes:
            chunk = self._data[self._position : self._position + size]
            self._position += len(chunk)
            return chunk

    class _Response:
        def __init__(self, data: bytes) -> None:
            self.content = _Content(data)

    async def reader_bench() -> None:
        started = time.perf_counter()
        body = await _read_bounded("bench", _Response(blob), len(blob))
        print(
            f"{'_read_bounded 256 KiB chunks':46s} {time.perf_counter() - started:7.3f}s"
            f"  {len(body) / 1e6:.1f} MB"
        )

    asyncio.run(reader_bench())


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
    compile_prompt = build_compile_system_prompt("full")
    print(f"transcript: {len(text)} chars (~{len(text) / 900:.0f} min of speech)")
    print(f"NOTE_CHUNK_CHARS={settings.note_chunk_chars}  NOTE_CONCURRENCY={settings.note_concurrency}")
    print(
        f"NOTE_CONTEXT_TOKENS={settings.note_context_tokens}"
        f"  NOTE_MAX_OUTPUT_TOKENS={settings.note_max_output_tokens}"
    )
    print(f"\n{'system prompt (stable rules)':46s} {len(system):6d} chars  ~{len(system) / 2:5.0f} tokens")
    print(f"{'system prompt + chunk context':46s} {len(contextual):6d} chars  ~{len(contextual) / 2:5.0f} tokens")
    print(f"{'compile system prompt':46s} {len(compile_prompt):6d} chars  ~{len(compile_prompt) / 2:5.0f} tokens")
    chunk_chars = effective_chunk_chars(settings, system_prompt=system)
    with_context = effective_chunk_chars(
        settings, system_prompt=system, extra_reserve_chars=CHUNK_CONTEXT_RESERVE_CHARS
    )
    print(f"\neffective_chunk_chars={chunk_chars}  with context={with_context}")
    for size in (chars, chars * 3):
        body = text if size <= chars else _synthetic_transcript(size)
        chunks = split_transcript(body, with_context)
        # Map rounds for a few concurrency settings: the number that matters is
        # ceil(chunks / concurrency), and it is what NOTE_CONCURRENCY buys.
        rounds = ", ".join(
            f"c={level}->{math.ceil(len(chunks) / level)} round(s)" for level in (2, 3, 6)
        )
        print(
            f"{len(body):7d} chars -> chunks={len(chunks):3d}  {rounds}"
            f"  sizes={[len(chunk) for chunk in chunks]}"
        )
    print(f"compile budget (chars): {effective_compile_max_chars(settings)}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="perf_bench", description=__doc__.splitlines()[0])
    parser.add_argument("group", choices=("audio", "frames", "cpu", "json", "prompts", "all"))
    parser.add_argument("--minutes", type=float, default=10.0, help="audio length to synthesise")
    parser.add_argument("--chars", type=int, default=108000, help="transcript length (~2h of Persian)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.group in ("cpu", "json", "prompts", "all"):
        print("=== notes layer (deterministic CPU) ===")
        bench_cpu(args.chars)
        print()
    if args.group in ("json", "all"):
        print("=== STT response reading/parsing ===")
        bench_json()
        print()
    if args.group in ("prompts", "all"):
        print("=== prompt sizing ===")
        bench_prompts(args.chars)
        print()
    if args.group in ("frames", "all"):
        print("=== transcode stages (null muxer) ===")
        with tempfile.TemporaryDirectory() as tmp:
            bench_frames(args.minutes, Path(tmp))
        print()
    if args.group in ("audio", "all"):
        print("=== media layer: format matrix ===")
        with tempfile.TemporaryDirectory() as tmp:
            bench_audio(args.minutes, Path(tmp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
