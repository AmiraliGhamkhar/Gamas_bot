from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

from gamas_bot.config import Settings
from gamas_bot.stt import STT_PROVIDERS, _language_error, transcribe
from gamas_bot.stt_platform.benchmark_metrics import (
    BENCHMARK_PROFILES,
    normalize_words,
    profile_from_path,
    result_row,
    word_error_rate,
)
from gamas_bot.stt_platform.adapters import get_audio_duration_seconds
from gamas_bot.stt_platform.policy import SttPolicy
from gamas_bot.stt_platform.registry import STT_PROVIDER_REGISTRY
from gamas_bot.stt_platform.router import CandidateFacts, SttRequirements, plan_route

__all__ = ["word_error_rate", "normalize_words", "main"]


def plan_report(settings: Settings, audio: Path) -> list[dict[str, str]]:
    """Route decisions per engine for one sample. Sends no audio and no key.

    Use it before a paid or trial benchmark: it shows which engines the free-only
    policy would refuse, and why, so a skipped engine is never mistaken for a
    result.
    """
    size = audio.stat().st_size
    duration = get_audio_duration_seconds(audio)
    req = SttRequirements.for_job(
        language=settings.stt_language,
        file_bytes=size,
        duration_seconds=duration,
    )
    facts = {
        name: CandidateFacts(
            has_credential=bool(STT_PROVIDERS[name].availability(settings)),
            max_upload=STT_PROVIDERS[name].max_upload(settings),
            language_error=_language_error(name, settings),
        )
        for name in STT_PROVIDERS
    }
    policy = SttPolicy.from_settings(settings)
    plan = plan_route(settings, req, facts, policy=policy)
    rows = []
    for decision in plan.decisions:
        rows.append({
            "sample": audio.name,
            "engine": decision.provider,
            "eligible": str(decision.eligible).lower(),
            "tier": decision.tier,
            "reasons": ",".join(decision.reasons) or "-",
            "warnings": ",".join(decision.warnings) or "-",
            "evidence": STT_PROVIDER_REGISTRY[decision.provider].evidence,
        })
    return rows


def load_terms(path: Path | None) -> tuple[str, ...]:
    """One terminology entry per line (drug names, acronyms, technical terms)."""
    if path is None or not path.is_file():
        return ()
    return tuple(
        line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


async def run(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    if getattr(args, "plan", False):
        rows = [row for audio in args.audio if audio.is_file() for row in plan_report(settings, audio)]
        writer = csv.DictWriter(
            sys.stdout,
            fieldnames=["sample", "engine", "eligible", "tier", "reasons", "warnings", "evidence"],
        )
        writer.writeheader()
        writer.writerows(rows)
        return
    # Compare every STT engine that is actually configured (Speechmatics,
    # Deepgram, and any OpenAI-compatible endpoint), in registry order.
    engines = [
        name for name, provider in STT_PROVIDERS.items() if provider.availability(settings)
    ]
    if not engines:
        raise SystemExit("هیچ سرویس STT پیکربندی نشده است؛ مقایسه‌ای برای انجام نیست.")
    terms = load_terms(getattr(args, "terms", None))
    rows: list[dict[str, str | float]] = []
    logging.basicConfig(level=logging.WARNING)
    for audio in args.audio:
        if not audio.is_file():
            logging.error("فایل پیدا نشد: %s", audio)
            continue
        reference_path = audio.with_suffix(audio.suffix + ".txt")
        reference = reference_path.read_text(encoding="utf-8") if reference_path.exists() else None
        profile = getattr(args, "profile", None) or profile_from_path(audio)
        duration = get_audio_duration_seconds(audio)
        for engine in engines:
            # Pin the route to this engine: the free-only policy still applies,
            # so a refused engine shows up as a failed row with its reason.
            run_settings = replace(
                settings,
                stt_primary=engine,
                stt_default_route=engine,
                stt_fallback_enabled=False,
            )
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
                row = result_row(
                    provider=engine,
                    model=str(STT_PROVIDER_REGISTRY[engine].default_model or ""),
                    profile=profile,
                    language=settings.stt_language,
                    audio_duration=duration,
                    reference=reference,
                    hypothesis=result.text,
                    terms=terms,
                    latency_seconds=elapsed,
                    reliability=1.0,
                    quota_efficiency=None,
                )
                row.pop("provider", None)
                row.update({
                    "sample": audio.name,
                    "engine": engine,
                    "status": "ok",
                    "file_size_mb": round(audio.stat().st_size / (1024 * 1024), 3),
                    "confidence": result.confidence if result.confidence is not None else "",
                    "reference_file": reference_path.name if reference is not None else "",
                    "error": "",
                    "evidence": STT_PROVIDER_REGISTRY[engine].evidence,
                })
                rows.append(row)
            except Exception as exc:
                elapsed = round(time.perf_counter() - started, 3)
                logging.exception("%s failed for %s", engine, audio.name)
                rows.append({
                    "sample": audio.name,
                    "engine": engine,
                    "model": str(STT_PROVIDER_REGISTRY[engine].default_model or ""),
                    "profile": profile,
                    "language": settings.stt_language,
                    "audio_duration_seconds": duration if duration is not None else "",
                    "status": "failed",
                    "latency_seconds": elapsed,
                    "file_size_mb": round(audio.stat().st_size / (1024 * 1024), 3),
                    "confidence": "",
                    "wer": "",
                    "cer": "",
                    "terminology_preservation": "",
                    "numeric_preservation": "",
                    "punctuation_per_100_words": "",
                    "persian_character_ratio": "",
                    "latin_character_ratio": "",
                    "repetition_ratio": "",
                    "quality_flags": "",
                    "score": "",
                    "reference_file": reference_path.name if reference is not None else "",
                    "error": str(exc)[:400],
                    "evidence": STT_PROVIDER_REGISTRY[engine].evidence,
                })
    fields = [
        "sample", "engine", "model", "profile", "language", "audio_duration_seconds",
        "status", "latency_seconds", "file_size_mb", "confidence",
        "wer", "cer", "terminology_preservation", "numeric_preservation",
        "punctuation_per_100_words", "persian_character_ratio", "latin_character_ratio",
        "repetition_ratio", "quality_flags", "score",
        "reference_file", "error", "evidence",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"نتایج {len(rows)} ارزیابی در {args.output} ذخیره شد؛ متن فایل‌ها ذخیره نشده است.")


def main() -> None:
    parser = argparse.ArgumentParser(description="ارزیابی محلی و هم‌شرایط موتورهای STT فارسی")
    parser.add_argument("audio", nargs="+", type=Path, help="مسیر یک یا چند فایل صوتی فارسی")
    parser.add_argument("--output", type=Path, default=Path("stt-benchmark.csv"))
    parser.add_argument(
        "--plan",
        action="store_true",
        help="print the route decision per engine to stdout; sends no audio",
    )
    parser.add_argument(
        "--terms",
        type=Path,
        default=None,
        help="file with one medical/technical term per line for terminology preservation",
    )
    parser.add_argument(
        "--profile",
        choices=BENCHMARK_PROFILES,
        default=None,
        help="benchmark category override (default: taken from the fixture directory)",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
