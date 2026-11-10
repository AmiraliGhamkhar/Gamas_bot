"""Offline evaluation for Gamas 2.0 transcription and diarization.

Four standard, deterministic metrics, all computed locally from JSON files —
no provider call, no model, no network:

* :func:`word_error_rate` (WER) — word-level edit distance / reference words.
* :func:`character_error_rate` (CER) — character-level edit distance / chars.
* :func:`diarization_error_rate` (DER) — (missed + false alarm + speaker
  confusion) / total reference speech time, after an optimal *greedy* label
  mapping. Anonymous labels are arbitrary (``speaker_0`` in the hypothesis may
  be ``استاد`` in the reference), so they must be aligned before comparing.
* :func:`jaccard_error_rate` (JER) — ``1 - mean(intersection / union)`` over
  reference speakers, using the same label mapping.

The timeline is quantized to 100 ms frames. That is coarse enough for a
two-hour lecture to stay bounded (72 000 frames) and fine enough that a
one-word attribution error is visible. Overlapping speech is supported: each
frame carries a *set* of active speakers.

No identity is inferred. A hypothesis label with no reference counterpart is a
false alarm, and an unmatched reference label is missed speech — the evaluator
never guesses that two labels are "probably the same person".
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

FRAME_SECONDS = 0.1
MAX_FRAMES = 100_000
MAX_TOKENS = 40_000

_PUNCTUATION = re.compile(r"[^\w\s\u200c]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Normalize Persian/Arabic variants and drop punctuation for WER/CER."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("\u064a", "\u06cc").replace("\u0643", "\u06a9")
    value = _PUNCTUATION.sub(" ", value)
    return _WHITESPACE.sub(" ", value).strip()


def _levenshtein(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Edit distance with one rolling row (bounded memory, O(n*m) time)."""
    if len(reference) > MAX_TOKENS or len(hypothesis) > MAX_TOKENS:
        raise ValueError("transcript is too long to evaluate")
    if not reference:
        return len(hypothesis)
    if not hypothesis:
        return len(reference)
    previous = list(range(len(hypothesis) + 1))
    for index, ref_token in enumerate(reference, start=1):
        current = [index]
        for column, hyp_token in enumerate(hypothesis, start=1):
            cost = 0 if ref_token == hyp_token else 1
            current.append(
                min(
                    current[column - 1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + cost,
                )
            )
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """WER in ``[0, 1]`` (can exceed 1 when insertions dominate)."""
    ref = normalize_text(reference).split()
    hyp = normalize_text(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else float(len(hyp))
    return _levenshtein(ref, hyp) / len(ref)


def character_error_rate(reference: str, hypothesis: str) -> float:
    """CER in ``[0, 1]`` over normalized characters (spaces removed)."""
    ref = normalize_text(reference).replace(" ", "")
    hyp = normalize_text(hypothesis).replace(" ", "")
    if not ref:
        return 0.0 if not hyp else float(len(hyp))
    return _levenshtein(list(ref), list(hyp)) / len(ref)


def _turns(raw: Iterable[Mapping[str, Any]]) -> list[tuple[float, float, str]]:
    parsed: list[tuple[float, float, str]] = []
    for item in raw:
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        label = str(item.get("speaker_label") or item.get("speaker_id") or "").strip()
        if not label or end <= start or start < 0:
            continue
        parsed.append((start, end, label))
    return parsed


def _frames(
    turns: Iterable[tuple[float, float, str]]
) -> tuple[list[frozenset[str]], int]:
    """Quantize turns into ``[frozenset(active labels)]`` frames."""
    materialized = list(turns)
    if not materialized:
        return [], 0
    duration = max(end for _start, end, _label in materialized)
    count = min(MAX_FRAMES, int(duration / FRAME_SECONDS) + 1)
    buckets: list[set[str]] = [set() for _ in range(count)]
    for start, end, label in materialized:
        first = int(start / FRAME_SECONDS)
        last = min(count, int(end / FRAME_SECONDS) + 1)
        for index in range(max(0, first), last):
            buckets[index].add(label)
    return [frozenset(bucket) for bucket in buckets], count


def optimal_label_map(
    reference: Sequence[Mapping[str, Any]],
    hypothesis: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Greedy one-to-one mapping from hypothesis labels to reference labels.

    For each hypothesis label, the reference label sharing the most speech time
    wins; a label can only be used once. Greedy is deterministic and O(n²) in
    the number of *labels* (at most ``MAX_SPEAKERS``), which is what a classroom
    needs — a Hungarian solver would add a dependency for no measurable gain.
    """
    ref_turns = _turns(reference)
    hyp_turns = _turns(hypothesis)
    overlap: dict[tuple[str, str], float] = {}
    for h_start, h_end, h_label in hyp_turns:
        for r_start, r_end, r_label in ref_turns:
            shared = min(h_end, r_end) - max(h_start, r_start)
            if shared > 0:
                key = (h_label, r_label)
                overlap[key] = overlap.get(key, 0.0) + shared
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for (h_label, r_label), _shared in sorted(
        overlap.items(), key=lambda pair: (-pair[1], pair[0])
    ):
        if h_label in mapping or r_label in used:
            continue
        mapping[h_label] = r_label
        used.add(r_label)
    return mapping


@dataclass(frozen=True, slots=True)
class DiarizationScores:
    """The four time-based counts behind DER, plus JER."""

    der: float
    jer: float
    missed_frames: int
    false_alarm_frames: int
    confusion_frames: int
    reference_frames: int
    label_map: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "der": round(self.der, 6),
            "jer": round(self.jer, 6),
            "missed_frames": self.missed_frames,
            "false_alarm_frames": self.false_alarm_frames,
            "confusion_frames": self.confusion_frames,
            "reference_frames": self.reference_frames,
            "label_map": dict(self.label_map),
        }


def score_diarization(
    reference: Sequence[Mapping[str, Any]],
    hypothesis: Sequence[Mapping[str, Any]],
) -> DiarizationScores:
    """Compute DER and JER for one recording, with overlap-aware frames."""
    mapping = optimal_label_map(reference, hypothesis)
    ref_frames, ref_count = _frames(_turns(reference))
    hyp_turns = _turns(hypothesis)
    mapped = [
        (start, end, mapping.get(label, f"__unmapped__{label}"))
        for start, end, label in hyp_turns
    ]
    hyp_frames, hyp_count = _frames(mapped)
    count = max(ref_count, hyp_count, 1)

    def at(frames: list[frozenset[str]], size: int, index: int) -> frozenset[str]:
        return frames[index] if index < size else frozenset()

    missed = false_alarm = confusion = total_ref = 0
    hyp_labels: set[str] = set()
    per_speaker: dict[str, list[int]] = {}
    for index in range(count):
        ref_set = at(ref_frames, ref_count, index)
        hyp_set = at(hyp_frames, hyp_count, index)
        hyp_labels.update(hyp_set)
        total_ref += len(ref_set)
        # Standard DER decomposition on the (mapped) frame sets:
        #   missed       = reference speech with no matching hypothesis label
        #   false alarm  = hypothesis speech with no matching reference label
        #   confusion    = reference speech attributed to a *different* label
        # A frame with both a missing and an extra label contributes one of
        # each; counting the missing part again as confusion would double it,
        # so confusion is only counted when the sets overlap on no other label.
        missing_here = len(ref_set - hyp_set)
        extra_here = len(hyp_set - ref_set)
        missed += missing_here
        false_alarm += extra_here
        if ref_set and hyp_set and ref_set != hyp_set:
            confusion += min(missing_here, extra_here)
        for label in ref_set | hyp_set:
            bucket = per_speaker.setdefault(label, [0, 0])
            bucket[1] += 1
            if label in ref_set and label in hyp_set:
                bucket[0] += 1
    mistakes = missed + false_alarm + confusion
    der = mistakes / total_ref if total_ref else (1.0 if hyp_labels else 0.0)
    if per_speaker:
        jer = 1.0 - sum(
            inter / union if union else 0.0
            for inter, union in per_speaker.values()
        ) / len(per_speaker)
    else:
        jer = 0.0
    return DiarizationScores(
        der=der,
        jer=jer,
        missed_frames=missed,
        false_alarm_frames=false_alarm,
        confusion_frames=confusion,
        reference_frames=total_ref,
        label_map=mapping,
    )


def evaluate(
    reference: Mapping[str, Any], hypothesis: Mapping[str, Any]
) -> dict[str, Any]:
    """Score one recording: ``{"turns": [...], "text": "..."}`` in, all four out.

    ``text`` may be omitted; then WER/CER are ``None`` rather than a fabricated
    zero. The same rule applies to missing turns: DER/JER are ``None``.
    """
    ref_turns = list(reference.get("turns") or [])
    hyp_turns = list(hypothesis.get("turns") or [])
    result: dict[str, Any] = {}
    if ref_turns and hyp_turns:
        result["diarization"] = score_diarization(ref_turns, hyp_turns).to_dict()
    else:
        result["diarization"] = None
    ref_text = str(reference.get("text") or "")
    hyp_text = str(hypothesis.get("text") or "")
    if ref_text.strip():
        result["wer"] = round(word_error_rate(ref_text, hyp_text), 6)
        result["cer"] = round(character_error_rate(ref_text, hyp_text), 6)
    else:
        result["wer"] = None
        result["cer"] = None
    return result


__all__ = [
    "DiarizationScores",
    "character_error_rate",
    "evaluate",
    "normalize_text",
    "optimal_label_map",
    "score_diarization",
    "word_error_rate",
]
