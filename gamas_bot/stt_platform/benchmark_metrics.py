"""Deterministic benchmark and quality metrics for Gamas STT (spec §55-§58).

Pure functions only: no I/O, no network, no API keys — CI can exercise every
metric offline and benchmarks stay reproducible. Text passed to these helpers
stays in the caller's process; nothing here logs transcript content (spec §54).

The composite score follows the reviewed weighting (spec §58):

* 40% Persian transcription accuracy  (1 - word error rate)
* 20% medical/technical terminology preservation
* 10% numeric preservation (numbers, dates, units, drug names)
* 10% punctuation/readability
* 10% reliability
* 5%  latency
* 5%  quota efficiency

Accuracy dominates: a provider cannot win on speed alone, and "free" never
inflates the quality score (spec §59) — cost is tracked separately.
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass

#: Arabic/Persian code-point forms folded to canonical Persian letters before
#: word comparison (the benchmark WER must not count orthography drift as an
#: ASR error).
PERSIAN_NORMALIZATION = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه"})

_DIACRITICS_RE = re.compile(r"[\u064b-\u065f\u0670\u0640]")
_PUNCT_STRIP = str.maketrans("", "", string.punctuation + "،؛؟٪٬٫«»…“”‘’")

#: Numbers in Latin or Persian digits, optionally with separators/decimals and
#: a following unit (mg, ml, mmHg, ...). Used for numeric-preservation checks.
_NUMBER_RE = re.compile(
    r"(?<![\w])(\d[\d,\.\/]*\s?(?:%|mg|ml|mcg|µg|g|kg|mmHg|cm|mm|IU|u/l|°[cf])?)",
    re.IGNORECASE,
)
_PERSIAN_DIGIT_RE = re.compile(r"[۰-۹٠-٩]+")
_PERSIAN_LETTER_RE = re.compile(r"[\u0600-\u06ff]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

#: Score weights (spec §58). Sum to 1.0.
SCORE_WEIGHTS = {
    "persian_accuracy": 0.40,
    "terminology": 0.20,
    "numeric": 0.10,
    "punctuation": 0.10,
    "reliability": 0.10,
    "latency": 0.05,
    "quota_efficiency": 0.05,
}

_PUNCTUATION_CHARS = frozenset(".,;:!?،؛؟٫٬.…!?")


def normalize_words(text: str) -> list[str]:
    """Comparable word tokens: NFKC, Persian letter folding, no diacritics/punct."""
    text = unicodedata.normalize("NFKC", text or "").translate(PERSIAN_NORMALIZATION)
    text = _DIACRITICS_RE.sub("", text)
    text = text.translate(_PUNCT_STRIP)
    return [token for token in text.split() if token]


def _edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, ref_item in enumerate(reference, start=1):
        current = [row]
        for column, hyp_item in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[column - 1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (ref_item != hyp_item),
                )
            )
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """WER over normalized words. ``0.0`` for empty reference and hypothesis."""
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else float("inf")
    return _edit_distance(ref, hyp) / len(ref)


def character_error_rate(reference: str, hypothesis: str) -> float:
    """CER over normalized text (no spaces/punct), for orthography-sensitive review."""
    ref = "".join(normalize_words(reference))
    hyp = "".join(normalize_words(hypothesis))
    if not ref:
        return 0.0 if not hyp else float("inf")
    return _edit_distance(list(ref), list(hyp)) / len(ref)


def _normalize_for_search(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(PERSIAN_NORMALIZATION)
    return _DIACRITICS_RE.sub("", text).casefold()


def terminology_preservation(hypothesis: str, terms) -> float | None:
    """Fraction of ``terms`` present in the transcript (None when no terms given).

    Used for drug names, acronyms and English technical vocabulary in Persian
    lectures; matching is normalized and case-insensitive.
    """
    wanted = [str(term).strip() for term in (terms or ()) if str(term).strip()]
    if not wanted:
        return None
    haystack = _normalize_for_search(hypothesis)
    found = sum(1 for term in wanted if _normalize_for_search(term) in haystack)
    return found / len(wanted)


def extract_numbers(text: str) -> list[str]:
    """Numbers/units in the text, Latin and Persian digits, normalized to Latin."""
    text = unicodedata.normalize("NFKC", text or "").translate(PERSIAN_NORMALIZATION)
    text = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    found = [match.group(1).strip().casefold() for match in _NUMBER_RE.finditer(text)]
    found += [match.group(0).strip() for match in _PERSIAN_DIGIT_RE.finditer(text)]
    return found


def numeric_preservation(reference: str, hypothesis: str) -> float | None:
    """Fraction of reference numbers still present in the hypothesis.

    ``None`` when the reference contains no numbers — absence is reported as
    unknown, never as a perfect score.
    """
    wanted = extract_numbers(reference)
    if not wanted:
        return None
    haystack = _normalize_for_search(
        (hypothesis or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    )
    found = sum(1 for number in wanted if number in haystack)
    return found / len(wanted)


def punctuation_density(text: str) -> float:
    """Sentence punctuation marks per 100 words (readability proxy)."""
    words = normalize_words(text)
    if not words:
        return 0.0
    marks = sum(1 for char in (text or "") if char in _PUNCTUATION_CHARS)
    return round(100.0 * marks / len(words), 3)


def script_ratios(text: str) -> tuple[float, float]:
    """(Persian-letter share, Latin-letter share) of all letters in the text."""
    letters = _LETTER_RE.findall(text or "")
    if not letters:
        return 0.0, 0.0
    persian = sum(1 for letter in letters if _PERSIAN_LETTER_RE.fullmatch(letter))
    latin = sum(1 for letter in letters if _LATIN_LETTER_RE.fullmatch(letter))
    return persian / len(letters), latin / len(letters)


def repetition_ratio(text: str) -> float:
    """Share of tokens that are immediate repeats of the previous token."""
    words = normalize_words(text)
    if len(words) < 2:
        return 0.0
    repeats = sum(1 for first, second in zip(words, words[1:], strict=False) if first == second)
    return round(repeats / (len(words) - 1), 4)


def non_whitespace_ratio(text: str) -> float:
    text = text or ""
    if not text:
        return 0.0
    return round(1.0 - len(_WS_RE.findall(text)) / max(len(text), 1), 4)


@dataclass(frozen=True, slots=True)
class QualitySignals:
    """Deterministic quality signals for one transcript (spec §55).

    Abnormal output is *flagged* — a single abnormal signal never rejects a
    transcript on its own (spec §55/§30).
    """

    characters_per_audio_second: float | None
    words_per_audio_second: float | None
    non_whitespace_ratio: float
    repetition_ratio: float
    unicode_ratio: float
    persian_character_ratio: float
    latin_character_ratio: float
    number_density: float
    flags: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "characters_per_audio_second": self.characters_per_audio_second,
            "words_per_audio_second": self.words_per_audio_second,
            "non_whitespace_ratio": self.non_whitespace_ratio,
            "repetition_ratio": self.repetition_ratio,
            "unicode_ratio": self.unicode_ratio,
            "persian_character_ratio": self.persian_character_ratio,
            "latin_character_ratio": self.latin_character_ratio,
            "number_density": self.number_density,
            "flags": list(self.flags),
        }


def quality_signals(text: str, duration_seconds: float | None) -> QualitySignals:
    """Compute the spec §55 signal set for one transcript."""
    text = text or ""
    words = normalize_words(text)
    duration = float(duration_seconds) if duration_seconds and duration_seconds > 0 else None
    persian, latin = script_ratios(text)
    letters = _LETTER_RE.findall(text)
    unicode_ratio = round(len(letters) / max(len(text), 1), 4)
    numbers = extract_numbers(text)
    number_density = round(len(numbers) / max(len(words), 1), 4)
    flags: list[str] = []
    chars_per_second = (
        round(len(text) / duration, 4) if duration else None
    )
    words_per_second = round(len(words) / duration, 4) if duration else None
    if words_per_second is not None and words_per_second > 5.0:
        flags.append("high_words_per_second")
    if words_per_second is not None and words_per_second < 0.3 and len(words) > 20:
        flags.append("too_little_text_for_duration")
    rep = repetition_ratio(text)
    if rep > 0.25:
        flags.append("excessive_repetition")
    if non_whitespace_ratio(text) < 0.5:
        flags.append("low_non_whitespace_ratio")
    return QualitySignals(
        characters_per_audio_second=chars_per_second,
        words_per_audio_second=words_per_second,
        non_whitespace_ratio=non_whitespace_ratio(text),
        repetition_ratio=rep,
        unicode_ratio=unicode_ratio,
        persian_character_ratio=round(persian, 4),
        latin_character_ratio=round(latin, 4),
        number_density=number_density,
        flags=tuple(flags),
    )


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Weighted provider score for one benchmark run (spec §58)."""

    persian_accuracy: float | None
    terminology: float | None
    numeric: float | None
    punctuation: float | None
    reliability: float | None
    latency: float | None
    quota_efficiency: float | None
    total: float | None

    def as_dict(self) -> dict:
        return {
            "persian_accuracy": self.persian_accuracy,
            "terminology": self.terminology,
            "numeric": self.numeric,
            "punctuation": self.punctuation,
            "reliability": self.reliability,
            "latency": self.latency,
            "quota_efficiency": self.quota_efficiency,
            "total": self.total,
        }


def _bounded(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def punctuation_score(reference: str, hypothesis: str) -> float | None:
    """Readability score in 0..1: hypothesis punctuation density close to reference."""
    ref_density = punctuation_density(reference)
    hyp_density = punctuation_density(hypothesis)
    if ref_density <= 0 and hyp_density <= 0:
        return None
    denominator = max(ref_density, hyp_density, 1e-9)
    return max(0.0, 1.0 - abs(ref_density - hyp_density) / denominator)


def composite_score(
    *,
    reference: str | None,
    hypothesis: str,
    terms=(),
    reliability: float | None = None,
    latency_score: float | None = None,
    quota_efficiency: float | None = None,
) -> ScoreBreakdown:
    """Weighted score; measured components that are unavailable stay ``None``.

    The total is computed over the weights that are actually present and
    rescaled, so an unmeasured component never silently counts as zero — and
    never as a perfect score either.
    """
    if reference is None:
        accuracy = None
        terms_score = terminology_preservation(hypothesis, terms) if terms else None
        numbers_score = None
        punct_score = None
    else:
        wer = word_error_rate(reference, hypothesis)
        accuracy = None if wer == float("inf") else _bounded(1.0 - wer)
        terms_score = terminology_preservation(hypothesis, terms)
        numbers_score = numeric_preservation(reference, hypothesis)
        punct_score = punctuation_score(reference, hypothesis)
    components = {
        "persian_accuracy": _bounded(accuracy),
        "terminology": _bounded(terms_score),
        "numeric": _bounded(numbers_score),
        "punctuation": _bounded(punct_score),
        "reliability": _bounded(reliability),
        "latency": _bounded(latency_score),
        "quota_efficiency": _bounded(quota_efficiency),
    }
    present = {name: value for name, value in components.items() if value is not None}
    total: float | None = None
    if present:
        weight_sum = sum(SCORE_WEIGHTS[name] for name in present)
        total = round(
            sum(SCORE_WEIGHTS[name] * value for name, value in present.items()) / weight_sum,
            4,
        )
    return ScoreBreakdown(
        persian_accuracy=components["persian_accuracy"],
        terminology=components["terminology"],
        numeric=components["numeric"],
        punctuation=components["punctuation"],
        reliability=components["reliability"],
        latency=components["latency"],
        quota_efficiency=components["quota_efficiency"],
        total=total,
    )


#: The ten reviewed Gamas benchmark categories (spec §57). Each fixture
#: directory under ``tests/fixtures/stt/`` uses one of these slugs as its name
#: so results always identify their profile.
BENCHMARK_PROFILES: tuple[str, ...] = (
    "clean_persian_lecture",
    "noisy_persian_lecture",
    "persian_english_code_switching",
    "medical_lecture",
    "technical_lecture",
    "multiple_speakers",
    "classroom_noise",
    "numbers_and_units",
    "names_drugs_acronyms",
    "long_lecture",
)


def profile_from_path(path) -> str:
    """Benchmark profile of a fixture: its category directory name, or ``unclassified``."""
    parts = [part.name for part in getattr(path, "parents", ())]
    for part in parts:
        if part in BENCHMARK_PROFILES:
            return part
    return "unclassified"


def result_row(
    *,
    provider: str,
    model: str,
    profile: str,
    language: str,
    audio_duration: float | None,
    reference: str | None,
    hypothesis: str,
    terms=(),
    latency_seconds: float | None = None,
    reliability: float | None = None,
    quota_efficiency: float | None = None,
) -> dict:
    """One benchmark CSV row with the measured metrics and composite score."""
    wer = word_error_rate(reference, hypothesis) if reference is not None else None
    cer = character_error_rate(reference, hypothesis) if reference is not None else None
    terms_score = terminology_preservation(hypothesis, terms)
    numbers_score = numeric_preservation(reference, hypothesis) if reference is not None else None
    signals = quality_signals(hypothesis, audio_duration)
    latency_score = None
    if latency_seconds is not None and audio_duration:
        # 1.0 when the provider returns faster than real time; never negative.
        latency_score = _bounded(2.0 - max(latency_seconds / max(audio_duration, 1e-9), 0.0))
    score = composite_score(
        reference=reference,
        hypothesis=hypothesis,
        terms=terms,
        reliability=reliability,
        latency_score=latency_score,
        quota_efficiency=quota_efficiency,
    )
    return {
        "provider": provider,
        "model": model,
        "profile": profile,
        "language": language,
        "audio_duration_seconds": audio_duration if audio_duration is not None else "",
        "wer": "" if wer is None or wer == float("inf") else round(wer, 4),
        "cer": "" if cer is None or cer == float("inf") else round(cer, 4),
        "terminology_preservation": "" if terms_score is None else round(terms_score, 4),
        "numeric_preservation": "" if numbers_score is None else round(numbers_score, 4),
        "punctuation_per_100_words": punctuation_density(hypothesis),
        "persian_character_ratio": signals.persian_character_ratio,
        "latin_character_ratio": signals.latin_character_ratio,
        "repetition_ratio": signals.repetition_ratio,
        "quality_flags": ",".join(signals.flags),
        "latency_seconds": latency_seconds if latency_seconds is not None else "",
        "score": score.total if score.total is not None else "",
    }
