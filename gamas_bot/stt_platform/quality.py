"""Deterministic transcript validation and the STT quality gate.

Two separate concerns (spec §30/§32/§55):

* :func:`validate_transcript` — hard, deterministic checks. A transcript that
  fails validation is malformed output (empty text, embedded provider error,
  leaked request IDs, massive repetition) and is rejected.
* :func:`compute_quality_signals` — soft, statistical signals (characters per
  audio second, repetition ratio, Persian/Latin ratio, number density,
  confidence, detected language). A single abnormal metric never rejects a
  transcript; the gate combines signals conservatively.

Nothing here logs or stores transcript *content*: only counts and ratios.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

from .models import Transcript

#: Provider error markers that must never reach a Gamas transcript (spec §74).
_ERROR_MARKERS = (
    "request_id",
    "request id",
    "api key",
    "apikey",
    "authorization",
    "bearer ",
    "unauthorized",
    "quota exceeded",
    "rate limit exceeded",
    "rate_limit_exceeded",
    "insufficient_quota",
    "billing_required",
    "error_code",
    "traceback",
    "exception:",
)

#: A JSON error payload leaked into the transcript text.
_JSON_ERROR_PATTERN = re.compile(
    r"\{\s*\"(error|detail|message|code)\"\s*:\s*(\"|\{)", re.IGNORECASE
)

_PERSIAN_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
_LATIN_RANGE = re.compile(r"[A-Za-z]")
_DIGIT_RANGE = re.compile(r"[0-9\u06F0-\u06F9]")

#: A token repeated this many times in a row is a provider loop (spec §55).
_MAX_REPEATED_TOKEN_RUN = 12

#: Plausible speech density bounds (characters per audio second, non-whitespace).
_MIN_CHARS_PER_SECOND = 1.0
_MAX_CHARS_PER_SECOND = 60.0
#: Plausible words per audio second for natural speech.
_MIN_WORDS_PER_SECOND = 0.2
_MAX_WORDS_PER_SECOND = 12.0


@dataclass(frozen=True, slots=True)
class QualitySignals:
    """Deterministic, content-free quality metrics for one transcript."""

    characters: int = 0
    words: int = 0
    characters_per_audio_second: float | None = None
    words_per_audio_second: float | None = None
    non_whitespace_ratio: float = 0.0
    repetition_ratio: float = 0.0
    max_repeated_run: int = 0
    unicode_ratio: float = 0.0
    persian_character_ratio: float = 0.0
    latin_character_ratio: float = 0.0
    number_density: float = 0.0
    confidence: float | None = None
    detected_language: str | None = None


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    """The gate's decision for one transcript."""

    accepted: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    signals: QualitySignals | None = None

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def compute_quality_signals(
    transcript: Transcript, *, audio_duration_seconds: float | None = None
) -> QualitySignals:
    """Compute content-free quality metrics (never returns the text itself)."""
    text = transcript.text or ""
    characters = len(text)
    words_list = text.split()
    words = len(words_list)
    non_whitespace = sum(1 for char in text if not char.isspace())
    non_whitespace_ratio = (non_whitespace / characters) if characters else 0.0

    # Repetition: longest run of the same whitespace-delimited token plus the
    # share of tokens that belong to any run longer than the threshold.
    max_run = 0
    run = 0
    previous = None
    run_tokens = 0
    for token in words_list:
        if token == previous:
            run += 1
        else:
            run = 1
            previous = token
        if run > max_run:
            max_run = run
        if run >= _MAX_REPEATED_TOKEN_RUN:
            run_tokens += 1
    repetition_ratio = (run_tokens / words) if words else 0.0

    letters = sum(1 for char in text if char.isalpha())
    persian = len(_PERSIAN_RANGE.findall(text))
    latin = len(_LATIN_RANGE.findall(text))
    digits = len(_DIGIT_RANGE.findall(text))
    unicode_ratio = (letters / characters) if characters else 0.0
    persian_ratio = (persian / letters) if letters else 0.0
    latin_ratio = (latin / letters) if letters else 0.0
    number_density = (digits / characters) if characters else 0.0

    chars_per_second = None
    words_per_second = None
    if audio_duration_seconds and audio_duration_seconds > 0:
        chars_per_second = characters / audio_duration_seconds
        words_per_second = words / audio_duration_seconds

    return QualitySignals(
        characters=characters,
        words=words,
        characters_per_audio_second=chars_per_second,
        words_per_audio_second=words_per_second,
        non_whitespace_ratio=non_whitespace_ratio,
        repetition_ratio=repetition_ratio,
        max_repeated_run=max_run,
        unicode_ratio=unicode_ratio,
        persian_character_ratio=persian_ratio,
        latin_character_ratio=latin_ratio,
        number_density=number_density,
        confidence=transcript.confidence,
        detected_language=transcript.language,
    )


def validate_transcript(transcript: Transcript) -> tuple[bool, tuple[str, ...]]:
    """Hard deterministic validation (spec §32).

    Checks content *shape* only: non-empty, valid Unicode (always true for
    ``str``, kept for the contract), no embedded provider error text, no leaked
    request IDs / API metadata, no massive repeated sequence.
    """
    reasons: list[str] = []
    text = transcript.text or ""
    if not text.strip():
        reasons.append("empty_transcript")
        return False, tuple(reasons)
    lowered = text.lower()
    for marker in _ERROR_MARKERS:
        if marker in lowered:
            reasons.append(f"embedded_provider_error:{marker.strip()}")
    if _JSON_ERROR_PATTERN.search(text):
        reasons.append("embedded_json_error_payload")
    # Massive repeated sequence: one token repeated beyond the run threshold.
    words = text.split()
    if words:
        run = 1
        previous = words[0]
        max_run = 1
        for token in words[1:]:
            run = run + 1 if token == previous else 1
            previous = token
            if run > max_run:
                max_run = run
        if max_run > _MAX_REPEATED_TOKEN_RUN * 4:
            reasons.append("massive_repeated_sequence")
    # Normalize away: a transcript that is only punctuation/whitespace.
    if not any(char.isalpha() or char.isdigit() for char in text):
        reasons.append("no_alphanumeric_content")
    return (not reasons), tuple(reasons)


def _language_plausible(transcript: Transcript, expected_language: str | None) -> str | None:
    """A Persian job must produce Persian-dominant text (spec §10/§30)."""
    if not expected_language:
        return None
    expected = expected_language.strip().lower()
    if expected in {"auto", "multi"}:
        return None
    base = expected.split("-")[0].split("_")[0]
    if base != "fa":
        return None
    text = transcript.text or ""
    letters = sum(1 for char in text if char.isalpha())
    if not letters:
        return "no_letters_for_language_check"
    persian = len(_PERSIAN_RANGE.findall(text))
    # Code-switched lectures keep a substantial Persian share; a transcript
    # that is overwhelmingly Latin for a fa job is a language mismatch.
    if persian / letters < 0.30:
        return "language_mismatch"
    return None


class TranscriptQualityGate:
    """Conservative quality gate (spec §30).

    Rejects only when signals *clearly* justify it: hard validation failure,
    provider-reported confidence far below the configured threshold, an
    empty transcript, or a duration/text ratio anomaly. A single soft metric
    only produces a warning — Gamas never re-transcribes on one signal alone.
    """

    def __init__(self, settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "stt_quality_gate_enabled", True))

    @property
    def min_confidence(self) -> float:
        return float(getattr(self.settings, "stt_min_confidence", 0.65))

    def evaluate(
        self,
        transcript: Transcript,
        *,
        audio_duration_seconds: float | None = None,
        expected_language: str | None = None,
    ) -> QualityVerdict:
        if not self.enabled:
            signals = compute_quality_signals(
                transcript, audio_duration_seconds=audio_duration_seconds
            )
            return QualityVerdict(True, (), (), signals)
        signals = compute_quality_signals(
            transcript, audio_duration_seconds=audio_duration_seconds
        )
        if transcript.language and not signals.detected_language:
            signals = QualitySignals(
                **{**signals.__dict__, "detected_language": transcript.language}
            )
        reasons: list[str] = []
        warnings: list[str] = []

        valid, validation_reasons = validate_transcript(transcript)
        if not valid:
            reasons.extend(validation_reasons)

        confidence = transcript.confidence
        if confidence is not None and math.isfinite(confidence):
            if confidence < self.min_confidence:
                reasons.append(
                    f"low_confidence:{confidence:.3f}<{self.min_confidence:.2f}"
                )
        elif confidence is not None and not math.isfinite(confidence):
            warnings.append("non_finite_confidence")

        mismatch = _language_plausible(transcript, expected_language)
        if mismatch:
            reasons.append(mismatch)

        # Duration/text ratio anomaly: speech produces a bounded character rate.
        if signals.characters_per_audio_second is not None:
            rate = signals.characters_per_audio_second
            if rate < _MIN_CHARS_PER_SECOND:
                reasons.append(f"too_little_text_for_duration:{rate:.2f}cps")
            elif rate > _MAX_CHARS_PER_SECOND:
                reasons.append(f"too_much_text_for_duration:{rate:.2f}cps")
        if signals.words_per_audio_second is not None:
            wps = signals.words_per_audio_second
            if wps > _MAX_WORDS_PER_SECOND:
                warnings.append(f"high_words_per_second:{wps:.1f}")

        if signals.repetition_ratio > 0.10 or signals.max_repeated_run > _MAX_REPEATED_TOKEN_RUN:
            reasons.append(
                f"excessive_repetition:run={signals.max_repeated_run}"
            )
        if signals.characters and signals.non_whitespace_ratio < 0.5:
            warnings.append("low_non_whitespace_ratio")
        if signals.characters and signals.unicode_ratio < 0.3:
            warnings.append("low_unicode_ratio")

        return QualityVerdict(not reasons, tuple(reasons), tuple(warnings), signals)
