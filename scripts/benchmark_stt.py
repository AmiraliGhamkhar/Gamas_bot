from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re
import string
import time
import unicodedata
from dataclasses import replace
from pathlib import Path

from gamas_bot.config import Settings
from gamas_bot.stt import STT_PROVIDERS, transcribe

PERSIAN_NORMALIZATION = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه"})


def normalize_words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).translate(PERSIAN_NORMALIZATION)
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = text.translate(str.maketrans("", "", string.punctuation + "،؛؟٪٬٫«»…“”‘’"))
    return text.split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else float("inf")
    previous = list(range(len(hyp) + 1))
    for row, ref_word in enumerate(ref, start=1):
        current = [row]
        for column, hyp_word in enumerate(hyp, start=1):
            current.append(
                min(
                    current[column - 1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (ref_word != hyp_word),
                )
            )
        previous = current
    return previous[-1] / len(ref)


async def run(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    # Compare every STT engine that is actually configured (Speechmatics,
    # Deepgram, and any OpenAI-compatible endpoint), in registry order.
    engines = [
        name for name, provider in STT_PROVIDERS.items() if provider.availability(settings)
    ]
    if not engines:
        raise SystemExit("هیچ سرویس STT پیکربندی نشده است؛ مقایسه‌ای برای انجام نیست.")
    rows: list[dict[str, str | float]] = []
    logging.basicConfig(level=logging.WARNING)
    for audio in args.audio:
        if not audio.is_file():
            logging.error("فایل پیدا نشد: %s", audio)
            continue
        reference_path = audio.with_suffix(audio.suffix + ".txt")
        reference = reference_path.read_text(encoding="utf-8") if reference_path.exists() else None
        for engine in engines:
            run_settings = replace(settings, stt_primary=engine, stt_fallback_enabled=False)
            started = time.perf_counter()
            try:
                if audio.stat().st_size >= STT_PROVIDERS[engine].max_upload(run_settings):
                    raise ValueError(
                        f"{engine} direct-upload limit exceeded; sample was not sent"
                    )
                result = await transcribe(audio, run_settings)
                if result.engine != engine:
                    raise ValueError("Requested benchmark engine was not used")
                elapsed = round(time.perf_counter() - started, 3)
                rows.append({
                    "sample": audio.name,
                    "engine": engine,
                    "status": "ok",
                    "latency_seconds": elapsed,
                    "file_size_mb": round(audio.stat().st_size / (1024 * 1024), 3),
                    "confidence": result.confidence if result.confidence is not None else "",
                    "wer": round(word_error_rate(reference, result.text), 4) if reference is not None else "",
                    "reference_file": reference_path.name if reference is not None else "",
                    "error": "",
                })
            except Exception as exc:
                elapsed = round(time.perf_counter() - started, 3)
                logging.exception("%s failed for %s", engine, audio.name)
                rows.append({
                    "sample": audio.name,
                    "engine": engine,
                    "status": "failed",
                    "latency_seconds": elapsed,
                    "file_size_mb": round(audio.stat().st_size / (1024 * 1024), 3),
                    "confidence": "",
                    "wer": "",
                    "reference_file": reference_path.name if reference is not None else "",
                    "error": str(exc)[:400],
                })
    fields = [
        "sample", "engine", "status", "latency_seconds", "file_size_mb",
        "confidence", "wer", "reference_file", "error",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"نتایج {len(rows)} ارزیابی در {args.output} ذخیره شد؛ متن فایل‌ها ذخیره نشده است.")


def main() -> None:
    parser = argparse.ArgumentParser(description="ارزیابی محلی و هم‌شرایط موتورهای STT فارسی")
    parser.add_argument("audio", nargs="+", type=Path, help="مسیر یک یا چند فایل صوتی فارسی")
    parser.add_argument("--output", type=Path, default=Path("stt-benchmark.csv"))
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
