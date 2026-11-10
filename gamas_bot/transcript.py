"""The raw text engine — everything that happens to a transcript.

This module owns **the text that is passed to the LLM** (the stated first
priority of the v3 overhaul) and the raw-text file the user receives. Both are
the same artifact: a cleaned, deterministically paragraphed document that a
note model can actually read.

Pipeline
--------
1. *Paragraphing.* When the speech engine supplies word timings, paragraphs are
   rebuilt from the way the lecturer actually spoke: a pause longer than
   :data:`PARAGRAPH_GAP_SECONDS`, a spoken topic transition, or a soft size cap
   ends a paragraph. Without timings, paragraphs are rebuilt from sentence
   punctuation with the same topic markers and cap.
2. *Typographic cleanup.* Persian punctuation spacing (``،`` ``؛`` ``؟`` hug
   the previous word, one space after), collapsed terminator runs, Persian
   letter folding and whitespace collapse (via :mod:`gamas_bot.textnorm`).
   **Typography only, never content**: words, digits, Latin tokens, formulas
   and ZWNJ are untouchable.
3. *The fidelity gate.* Every transform runs inside :func:`guarded`, which
   compares a content fingerprint (digit groups, Latin tokens, ZWNJ, letter
   counts) before and after. A transform that would lose or invent content is
   rejected and the pre-transform text is used. The raw text can be
   *reformatted*, never *lost*.
4. *Chunking.* :func:`split_transcript` cuts the document into model-sized
   chunks at paragraph and sentence boundaries — never mid-sentence, never
   mid-number — preferring topic boundaries when a chunk is nearly full.

No function here talks to the network.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

from .progress import to_persian_digits
from .textnorm import normalize_digits, normalize_persian, normalize_whitespace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timing and size tuning
# ---------------------------------------------------------------------------

#: An inter-word pause longer than this (seconds) ends a paragraph. Lecturers
#: pause between topics; STT word timings make that structure recoverable.
PARAGRAPH_GAP_SECONDS = 1.6

#: A pause longer than this is treated as a sentence break even without
#: punctuation (some engines drop final punctuation on short segments).
SENTENCE_GAP_SECONDS = 0.7

#: Soft paragraph cap. A paragraph may exceed it until its next sentence
#: boundary — text is never cut mid-sentence to satisfy a size rule.
PARAGRAPH_SOFT_CAP_CHARS = 600

#: Default transcript chunk budget handed to the note model.
TRANSCRIPT_CHUNK_CHARS = 22000

#: Fraction of a chunk that must be full before an early topic-boundary cut.
TOPIC_BREAK_MIN_FILL = 0.6


# ---------------------------------------------------------------------------
# Raw provider output
# ---------------------------------------------------------------------------


#: A diarization label as the provider reported it (``S1``, ``0``, ``UU`` …).
#: ``""`` means "this engine does not diarize"; ``UU``/``unknown`` mean the
#: engine diarizes but could not attribute this token.
SPEAKER_UNKNOWN = "UU"
_UNKNOWN_SPEAKER_LABELS = frozenset({"", "uu", "unknown", "none", "null", "-"})


@dataclass(frozen=True, slots=True)
class Word:
    """One spoken token with its timing, punctuation already attached.

    ``start``/``end`` are seconds from the beginning of the audio, when the
    engine supplies them. ``speaker`` is the provider's diarization label; it
    is raw on purpose, because a provider may rename speakers between calls.
    ``confidence`` is the provider's word confidence (0..1) when it supplies
    one; ``None`` means the engine does not report it. ``overlaps`` marks a
    token whose time range overlaps another token's (simultaneous speech);
    it is derived from timings by :func:`mark_overlaps`, never invented.
    """

    text: str
    start: float | None = None
    end: float | None = None
    speaker: str = ""
    confidence: float | None = None
    overlaps: bool = False

    @property
    def speaker_id(self) -> str:
        """Stable anonymous id (``speaker_0``) for this token's voice.

        Providers renumber speakers between calls, so this id is only
        meaningful inside one transcript. Unknown attribution stays
        ``speaker_unknown`` — never a fabricated identity.
        """
        label = (self.speaker or "").strip()
        if _speaker_key(label) in _UNKNOWN_SPEAKER_LABELS:
            return "speaker_unknown"
        if label.isdigit():
            return f"speaker_{label}"
        return f"speaker_{_speaker_key(label)}"


@dataclass(slots=True)
class RawTranscript:
    """What a speech engine returns: timed words when available, text always.

    ``speakers`` is the number of voices the engine resolved *after* merging
    its raw labels; ``0`` means the engine does not diarize or could not. The
    count is part of the artifact (the user is told how many people spoke), not
    a statistic computed later from the paragraphs.
    """

    words: list[Word]
    text: str
    confidence: float | None = None
    engine: str = ""
    speakers: int = 0

    @classmethod
    def from_text(cls, text: str, confidence: float | None = None, engine: str = "") -> "RawTranscript":
        return cls(words=[], text=text, confidence=confidence, engine=engine)

    @property
    def is_diarized(self) -> bool:
        return self.speakers > 0


@dataclass(frozen=True, slots=True)
class Paragraph:
    """One paragraph of the delivered transcript.

    ``speaker`` is the stable, user-facing name (``گویندهٔ ۱``) after label
    normalisation; ``None`` when the engine did not diarize.
    """

    text: str
    start: float | None = None
    end: float | None = None
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    """One contiguous run of speech by one anonymous voice.

    ``speaker_id`` is the stable anonymous id (``speaker_0``); ``speaker_label``
    is the resolved label (``0``) or ``None`` for unknown attribution.
    ``display_name`` is the user-facing name (custom name when the user renamed
    the speaker, otherwise the Persian default ``گویندهٔ ۱``); ``None`` means
    explicitly unknown — never a fabricated identity. ``confidence`` is the
    mean word confidence of the turn when the engine reports it. ``overlaps``
    is true when any word in the turn overlaps another voice in time
    (simultaneous speech), so the transcript can flag it instead of silently
    assigning it to one speaker.
    """

    speaker_id: str
    start: float | None
    end: float | None
    text: str
    confidence: float | None = None
    speaker_label: str | None = None
    display_name: str | None = None
    overlaps: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "speaker_id": self.speaker_id,
            "speaker_label": self.speaker_label,
            "display_name": self.display_name,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "confidence": self.confidence,
            "overlaps": self.overlaps,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "SpeakerTurn | None":
        if not isinstance(payload, dict):
            return None
        text = str(payload.get("text") or "").strip()
        if not text:
            return None
        speaker_id = str(payload.get("speaker_id") or "speaker_unknown").strip()
        label = payload.get("speaker_label")
        label_text = str(label).strip() if label is not None else None
        if label_text in (None, "", "None"):
            label_text = None
        display = payload.get("display_name")
        display_text = str(display).strip() if display is not None else None
        if display_text in (None, "", "None"):
            display_text = None

        def _num(value: object) -> float | None:
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                return float(value)
            return None

        return cls(
            speaker_id=speaker_id or "speaker_unknown",
            start=_num(payload.get("start")),
            end=_num(payload.get("end")),
            text=text,
            confidence=_num(payload.get("confidence")),
            speaker_label=label_text,
            display_name=display_text,
            overlaps=bool(payload.get("overlaps", False)),
        )


@dataclass(frozen=True, slots=True)
class TranscriptDocument:
    """The cleaned, paragraphed transcript — the LLM input and the .txt body."""

    paragraphs: tuple[Paragraph, ...]
    confidence: float | None = None
    speakers: int = 0
    #: True when the engine diarized but the labels could not be resolved into
    #: trustworthy turns (too few words, one dominant voice). The transcript is
    #: still delivered; it is simply not split by speaker.
    diarization_attempted: bool = False
    #: User-renamed speaker labels as ``(stable_label, custom_name)`` pairs,
    #: e.g. ``(("0", "استاد"), ("1", "دانشجو"))``. Empty means defaults.
    speaker_names: tuple[tuple[str, str], ...] = ()
    #: Number of distinct time spans where two voices spoke simultaneously.
    #: Reported honestly (never smoothed away) so overlapping speech is visible.
    overlap_count: int = 0

    @property
    def speaker_names_map(self) -> dict[str, str]:
        return dict(self.speaker_names)

    @property
    def text(self) -> str:
        return self.plain()

    def _speaker_heading(self, paragraph: Paragraph, previous: Paragraph | None) -> str:
        """The turn header, only where the speaker changes."""
        name = speaker_name_from_label(
            paragraph.speaker, self.speaker_names_map or None
        )
        if name is None:
            return paragraph.text
        if previous is not None and previous.speaker == paragraph.speaker:
            return paragraph.text
        return f"{name}: {paragraph.text}"

    def speaker_turns(self) -> list[SpeakerTurn]:
        """Chronological speaker turns derived from the paragraphs.

        Each paragraph is already one turn (see
        :func:`paragraphs_from_words_by_speaker`), so this is a faithful
        projection: order, timings and text are preserved, and unknown
        attribution stays explicitly unknown. Word-level confidence is not
        stored in the document, so turn confidence is ``None`` here; use
        :func:`build_speaker_turns` on words when confidences are needed.
        """
        custom = self.speaker_names_map
        turns: list[SpeakerTurn] = []
        for paragraph in self.paragraphs:
            if not paragraph.text:
                continue
            label = paragraph.speaker
            if label is None or label == SPEAKER_UNKNOWN:
                turns.append(
                    SpeakerTurn(
                        speaker_id="speaker_unknown",
                        start=paragraph.start,
                        end=paragraph.end,
                        text=paragraph.text,
                        speaker_label=None,
                        display_name=None,
                    )
                )
            else:
                display = custom.get(label) or speaker_name_from_label(label)
                turns.append(
                    SpeakerTurn(
                        speaker_id=f"speaker_{label}",
                        start=paragraph.start,
                        end=paragraph.end,
                        text=paragraph.text,
                        speaker_label=label,
                        display_name=display,
                    )
                )
        return turns

    def turns_to_json(self) -> str:
        """Serialize the speaker turns for durable storage (``transcriptions``)."""
        import json as _json

        return _json.dumps(
            [turn.to_dict() for turn in self.speaker_turns()],
            ensure_ascii=False,
        )

    @staticmethod
    def turns_from_json(payload: str | None) -> list[SpeakerTurn]:
        """Parse stored turns; corrupt data yields no turns, never an error."""
        if not payload:
            return []
        import json as _json

        try:
            raw = _json.loads(payload)
        except (TypeError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        turns: list[SpeakerTurn] = []
        for item in raw:
            turn = SpeakerTurn.from_dict(item)
            if turn is not None:
                turns.append(turn)
        return turns

    def plain(self) -> str:
        """Paragraphs; a diarized document names the speaker at each new turn.

        The names are inlined rather than set on their own line so the text still
        reads as a transcript when pasted somewhere that collapses blank lines,
        and so the note model sees who said what without extra structure.
        """
        lines: list[str] = []
        previous: Paragraph | None = None
        for paragraph in self.paragraphs:
            if not paragraph.text:
                continue
            lines.append(self._speaker_heading(paragraph, previous))
            previous = paragraph
        return "\n\n".join(lines)

    def timestamped(self) -> str:
        """``[mm:ss]`` (Persian digits) at each paragraph that has a timing."""
        lines: list[str] = []
        previous: Paragraph | None = None
        for paragraph in self.paragraphs:
            if not paragraph.text:
                continue
            body = self._speaker_heading(paragraph, previous)
            if paragraph.start is not None:
                minutes, seconds = divmod(int(paragraph.start), 60)
                stamp = to_persian_digits(f"{minutes:02d}:{seconds:02d}")
                lines.append(f"[{stamp}] {body}")
            else:
                lines.append(body)
            previous = paragraph
        return "\n\n".join(lines)

    def llm_document(self) -> str:
        """The text passed to the note model: clean paragraphs, no timestamps."""
        return self.plain()



# ---------------------------------------------------------------------------
# Speaker turns
# ---------------------------------------------------------------------------


#: Minimum share of *attributed* words one raw label must hold before it is
#: believed. Diarizers sprinkle tiny clusters on laughter, coughs and a chair
#: scraping; treating those as separate speakers inflates the count.
MIN_SPEAKER_SHARE = 0.04
#: Absolute floor of attributed words for a label to count as a speaker.
MIN_SPEAKER_WORDS = 12
#: Upper bound on reported speakers. Beyond this the labels are noise, and a
#: "class with 40 speakers" claim would be worse than no claim at all.
MAX_SPEAKERS = 12
#: Two adjacent paragraphs of the same speaker are merged when the pause
#: between them is shorter than this (seconds); a longer pause is a real break.
SPEAKER_MERGE_GAP_SECONDS = 2.5


def _speaker_key(label: str) -> str:
    """Normalise a provider label for comparison (``S1``/``s1``/``1`` agree)."""
    text = label.strip()
    if text[:1] in {"S", "s"} and text[1:].isdigit():
        text = text[1:]
    return text.lower()


def resolve_speakers(words: list[Word]) -> tuple[list[Word], int]:
    """Fold a provider's raw labels into a stable, believable set of voices.

    Returns the words with the tiny clusters relabelled ``SPEAKER_UNKNOWN`` and
    the number of speakers that survived. A one-voice recording resolves to one
    speaker, which is the honest answer for a solo lecture.

    The thresholds are deliberately conservative: over-reporting speakers is a
    visible lie ("۳ نفر صحبت کردند" for a solo class), while under-reporting
    degrades to the ordinary transcript the user would have received anyway.
    """
    attributed = [word for word in words if _speaker_key(word.speaker) not in _UNKNOWN_SPEAKER_LABELS]
    if not attributed:
        return words, 0

    counts: dict[str, int] = {}
    for word in attributed:
        counts[_speaker_key(word.speaker)] = counts.get(_speaker_key(word.speaker), 0) + 1
    total = len(attributed)
    floor = max(MIN_SPEAKER_WORDS, int(total * MIN_SPEAKER_SHARE))
    kept = {key for key, count in counts.items() if count >= floor}
    if not kept:
        kept = {max(counts, key=lambda key: counts[key])}

    # Rank by how much each voice actually said: the first label the user sees
    # should be the lecturer, not whoever happened to speak first for one word.
    ranked = sorted(kept, key=lambda key: (-counts[key], key))
    ordered = {key: index for index, key in enumerate(ranked[:MAX_SPEAKERS])}

    resolved: list[Word] = []
    for word in words:
        key = _speaker_key(word.speaker)
        if key in ordered:
            resolved.append(replace(word, speaker=str(ordered[key])))
        else:
            resolved.append(replace(word, speaker=SPEAKER_UNKNOWN))
    return resolved, min(len(ordered), MAX_SPEAKERS)


def speaker_name(index: int) -> str:
    """The label shown to the user for a stable speaker id."""
    return f"گویندهٔ {to_persian_digits(index + 1)}"


def speaker_name_from_label(
    label: str | None, custom: dict[str, str] | None = None
) -> str | None:
    """Turn a stable id (``0``, ``1`` …) into its displayed name.

    ``custom`` maps stable labels to user-chosen names (``استاد``,
    ``دانشجو`` …); a custom name always wins over the Persian default.
    Unknown attribution returns ``None`` — explicitly unknown, never a
    fabricated identity.
    """
    if label is None:
        return None
    if custom and label in custom:
        name = str(custom[label]).strip()
        if name:
            return name
    if not label.isdigit():
        return None
    return speaker_name(int(label))


def mark_overlaps(words: list[Word]) -> list[Word]:
    """Flag words whose time range overlaps another word's (simultaneous speech).

    Overlap is derived from timings only: two words overlap when one's
    ``[start, end)`` intersects the other's. Words without timings are never
    flagged. The input order is preserved; the returned list is new.
    """
    timed = sorted(
        (
            (float(word.start), float(word.end), index)
            for index, word in enumerate(words)
            if word.start is not None and word.end is not None and word.end > word.start
        ),
        key=lambda item: (item[0], item[1]),
    )
    if len(timed) < 2:
        return list(words)
    flagged: set[int] = set()
    # Sorted by start, so only the words starting before this one ends can
    # overlap. The inner loop stops at the first non-overlapping start: a full
    # 90-minute lecture stays linear in the number of words, not quadratic.
    for position, (_start, end, index) in enumerate(timed):
        cursor = position + 1
        while cursor < len(timed) and timed[cursor][0] < end:
            other_start, _other_end, other_index = timed[cursor]
            if other_start < end:
                flagged.add(index)
                flagged.add(other_index)
            cursor += 1
    if not flagged:
        return list(words)
    return [
        replace(word, overlaps=True) if index in flagged else word
        for index, word in enumerate(words)
    ]


def overlap_spans(words: list[Word]) -> list[tuple[float, float]]:
    """Time spans where two or more timed words overlap (chronological)."""
    spans: list[tuple[float, float]] = []
    timed = sorted(
        (word.start, word.end)
        for word in words
        if word.start is not None and word.end is not None and word.end > word.start
    )
    for index in range(len(timed)):
        start, end = timed[index]
        for other_start, other_end in timed[index + 1 :]:
            if other_start >= end:
                break
            overlap_start = max(start, other_start)
            overlap_end = min(end, other_end)
            if overlap_end > overlap_start:
                spans.append((overlap_start, overlap_end))
    spans.sort()
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def build_speaker_turns(
    words: list[Word],
    *,
    custom_names: dict[str, str] | None = None,
    merge_gap: float = SPEAKER_MERGE_GAP_SECONDS,
    soft_cap: int = PARAGRAPH_SOFT_CAP_CHARS,
) -> list[SpeakerTurn]:
    """Group resolved words into chronological :class:`SpeakerTurn` objects.

    Mirrors :func:`paragraphs_from_words_by_speaker` (same turn boundaries,
    same unknown-inheritance) but keeps word confidences and overlap flags,
    so evaluation and storage can use the turns directly. ``words`` must
    already be resolved by :func:`resolve_speakers`.
    """
    if not words:
        return []
    marked = mark_overlaps(words)
    carried: list[Word] = []
    pending_unknown: list[Word] = []
    last_known = SPEAKER_UNKNOWN
    for word in marked:
        if word.speaker in _UNKNOWN_SPEAKER_LABELS or word.speaker == SPEAKER_UNKNOWN:
            pending_unknown.append(word)
            continue
        if pending_unknown:
            for held in pending_unknown:
                carried.append(replace(held, speaker=last_known))
            pending_unknown.clear()
        carried.append(word)
        last_known = word.speaker
    carried.extend(pending_unknown)

    turns: list[SpeakerTurn] = []
    current: list[Word] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        text = " ".join(word.text for word in current if word.text).strip()
        if text:
            label = current[0].speaker
            confidences = [
                word.confidence
                for word in current
                if word.confidence is not None
            ]
            mean_confidence = (
                sum(confidences) / len(confidences) if confidences else None
            )
            if label == SPEAKER_UNKNOWN:
                turns.append(
                    SpeakerTurn(
                        speaker_id="speaker_unknown",
                        start=current[0].start,
                        end=current[-1].end,
                        text=text,
                        confidence=mean_confidence,
                        speaker_label=None,
                        display_name=None,
                        overlaps=any(word.overlaps for word in current),
                    )
                )
            else:
                display = (custom_names or {}).get(label) or speaker_name_from_label(
                    label
                )
                turns.append(
                    SpeakerTurn(
                        speaker_id=f"speaker_{label}",
                        start=current[0].start,
                        end=current[-1].end,
                        text=text,
                        confidence=mean_confidence,
                        speaker_label=label,
                        display_name=display,
                        overlaps=any(word.overlaps for word in current),
                    )
                )
        current, current_len = [], 0

    for index, word in enumerate(carried):
        previous = carried[index - 1] if index else None
        same = bool(current) and current[0].speaker == word.speaker
        if same:
            gap = _gap_seconds(previous, word) if previous is not None else None
            if gap is not None and gap > merge_gap:
                flush()
            elif current_len >= soft_cap and _ends_sentence(current[-1].text):
                flush()
        elif current:
            flush()
        current.append(word)
        current_len += len(word.text) + 1
    flush()
    return turns


def paragraphs_from_words_by_speaker(
    words: list[Word],
    *,
    paragraph_gap: float = PARAGRAPH_GAP_SECONDS,
    soft_cap: int = PARAGRAPH_SOFT_CAP_CHARS,
    merge_gap: float = SPEAKER_MERGE_GAP_SECONDS,
) -> list[Paragraph]:
    """Group words into one paragraph per contiguous speaker turn.

    A turn is closed by a change of speaker, by a pause longer than
    ``merge_gap`` inside the same speaker (a question, an aside) or by the soft
    cap. Timings survive, so the ``[mm:ss]`` profile still works.

    Unknown attribution inside a turn inherits the surrounding speaker, because
    a single word a diarizer could not place does not make a new voice; an
    unknown run between two speakers stays unknown.
    """
    if not words:
        return []

    # Carry the previous attribution across short unknown runs.
    carried: list[Word] = []
    pending_unknown: list[Word] = []
    last_known = SPEAKER_UNKNOWN
    for word in words:
        if word.speaker in _UNKNOWN_SPEAKER_LABELS or word.speaker == SPEAKER_UNKNOWN:
            pending_unknown.append(word)
            continue
        if pending_unknown:
            for held in pending_unknown:
                carried.append(replace(held, speaker=last_known))
            pending_unknown.clear()
        carried.append(word)
        last_known = word.speaker
    carried.extend(pending_unknown)  # trailing unknown words stay unknown

    paragraphs: list[Paragraph] = []
    current: list[Word] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        text = " ".join(word.text for word in current if word.text).strip()
        if text:
            label = current[0].speaker
            paragraphs.append(
                Paragraph(
                    text=text,
                    start=current[0].start,
                    end=current[-1].end,
                    speaker=None if label == SPEAKER_UNKNOWN else label,
                )
            )
        current, current_len = [], 0

    for index, word in enumerate(carried):
        previous = carried[index - 1] if index else None
        same = bool(current) and current[0].speaker == word.speaker
        if same:
            gap = _gap_seconds(previous, word) if previous is not None else None
            if gap is not None and gap > merge_gap:
                flush()
            elif current_len >= soft_cap and _ends_sentence(current[-1].text):
                flush()
        elif current:
            flush()
        current.append(word)
        current_len += len(word.text) + 1
    flush()
    return paragraphs


#: Header the transcript carries when the class had more than one voice.
DIARIZATION_HEADER_TEMPLATE = "🎙 تعداد گویندگان شناسایی‌شده: {count}"


# ---------------------------------------------------------------------------
# The fidelity gate
# ---------------------------------------------------------------------------

_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")
_DIGIT_GROUP = re.compile(r"\d+(?:[.,:/]\d+)*")
_DIGIT_RUN = re.compile(r"\d+")
_PERSIAN_RUN = re.compile(r"[\u0600-\u06FF]+")
_ASCII_ALPHA_RUN = re.compile(r"[A-Za-z]+")
_ZWNJ = "\u200c"


def fingerprint(text: str) -> tuple[int, int, int, int, int, int]:
    """A content fingerprint: what must survive every typographic transform.

    Counts *content* units — digit groups, digit characters, Latin tokens,
    ZWNJ carriers, Persian-script and Latin letters — and deliberately excludes
    punctuation and spaces, so spacing fixes pass while any word, number or
    term loss is caught.
    """
    digits_folded = normalize_digits(text)
    return (
        len(_DIGIT_GROUP.findall(digits_folded)),
        sum(map(len, _DIGIT_RUN.findall(digits_folded))),
        len(_ALNUM_RUN.findall(digits_folded)),
        digits_folded.count(_ZWNJ),
        sum(map(len, _PERSIAN_RUN.findall(digits_folded))),
        sum(map(len, _ASCII_ALPHA_RUN.findall(digits_folded))),
    )


def guarded(text: str, transform, *, name: str) -> str:
    """Apply ``transform`` only if it preserves the content fingerprint.

    An exception, an empty result or any content-count change rejects the
    transform: the original text is returned and the rejection is logged. This
    is the hard invariant that makes cleanup safe — the raw text can be
    reformatted, never lost.
    """
    if not text:
        return text
    try:
        result = transform(text)
    except Exception:  # noqa: BLE001 — a typography fix must never crash delivery
        logger.warning("raw-text transform %r raised; keeping original", name, exc_info=True)
        return text
    if not result or not result.strip():
        logger.warning("raw-text transform %r emptied the text; keeping original", name)
        return text
    if result == text:
        return result
    if fingerprint(result) != fingerprint(text):
        logger.warning("raw-text transform %r rejected by the fidelity gate", name)
        return text
    return result


# ---------------------------------------------------------------------------
# Typographic cleanup (Persian)
# ---------------------------------------------------------------------------

#: Terminator run: «؟؟؟» → «؟», «.....» → «...». Content-neutral.
_TERMINATOR_RUN = re.compile(r"([،؛؟!])\1+")
_LONG_DOTS = re.compile(r"\.{4,}")

#: Persian punctuation must hug the previous word…
_SPACE_BEFORE_PERSIAN_PUNCT = re.compile(r"[ \t]+([،؛؟])")
#: …and be followed by exactly one space.
_PERSIAN_PUNCT_NO_SPACE_AFTER = re.compile(r"([،؛؟])(?=[^\s،؛؟])")

#: A space before a sentence dot (Persian habit) — but never inside numbers.
_SPACE_BEFORE_DOT = re.compile(r"[ \t]+\.((?=[^\d])|(?=$))")
#: A dot glued to the next Persian word needs one space after it.
_DOT_GLUED_TO_PERSIAN = re.compile(r"\.(?=[\u0600-\u06FF])")


def clean_typography(text: str) -> str:
    """Persian typographic cleanup. Typography only — never content.

    * Persian letter folding and whitespace collapse (:mod:`gamas_bot.textnorm`).
    * ``،`` ``؛`` ``؟`` attach to the previous word, one space after.
    * A stray space before a sentence dot is removed; a dot glued to the next
      Persian word gains a space. Decimal/thousand separators (``7.2``,
      ``1.000``) are never touched.
    * Runs of identical terminators collapse to one.

    Always run inside :func:`guarded`; never call directly on user content.
    """
    text = normalize_whitespace(normalize_persian(text))
    text = _TERMINATOR_RUN.sub(r"\1", text)
    text = _LONG_DOTS.sub("...", text)
    text = _SPACE_BEFORE_PERSIAN_PUNCT.sub(r"\1", text)
    text = _PERSIAN_PUNCT_NO_SPACE_AFTER.sub(r"\1 ", text)
    text = _SPACE_BEFORE_DOT.sub(r".", text)
    text = _DOT_GLUED_TO_PERSIAN.sub(". ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Sentence and topic detection
# ---------------------------------------------------------------------------

#: Paragraph openings that reliably mark a new topic in lecture speech.
_TOPIC_START = re.compile(
    r"^\s*(?:#{1,6}\s+\S"                                   # `### اسلاید ۳ — …`
    r"|(?:اسلاید|slide)\s*[\d۰-۹]"                          # «اسلاید ۱۲»
    r"|(?:بخش|فصل|موضوع|مبحث|بحث|قسمت)\s*(?:بعد|جدید|دوم|سوم|چهارم|پنجم)"
    r"|(?:حالا|اکنون|خب|بسیار خوب)?\s*(?:می‌رسیم|میرسیم|می‌رویم|میرویم|برویم)\s+"
    r"(?:سراغ|به)\b"
    r"|(?:نکته|موضوع)\s*(?:بعدی|آخر|پایانی)"
    r")",
    re.IGNORECASE,
)


def starts_a_topic(paragraph: str) -> bool:
    """Does this paragraph open a new topic (a safe early cut point)?"""
    return bool(_TOPIC_START.match(paragraph))


#: Sentence terminators. A dot between digits is a decimal separator, never a
#: boundary — splitting there would corrupt numeric values.
_SENTENCE_TERMINATOR = re.compile(r"(?:[!?؟…]+|؛|(?<!\d)\.(?!\d))[»)\]”\"']*")
#: Closing punctuation that can trail a terminator without ending a sentence
#: is handled by the character class above; a word "ends a sentence" when its
#: tail matches the terminator pattern.
_WORD_ENDS_SENTENCE = re.compile(r"(?:[!?؟…؛]|(?<!\d)\.(?!\d))[»)\]”\"']*$")


def _ends_sentence(token: str) -> bool:
    return bool(_WORD_ENDS_SENTENCE.search(token))


def sentence_spans(paragraph: str) -> list[tuple[int, int]]:
    """Spans of consecutive sentences; terminators stay inside their sentence."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_TERMINATOR.finditer(paragraph):
        end = match.end()
        if end <= start:
            continue  # a zero-length match can never terminate a sentence
        spans.append((start, end))
        start = end
    if start < len(paragraph):
        spans.append((start, len(paragraph)))
    return spans


# ---------------------------------------------------------------------------
# Paragraphing
# ---------------------------------------------------------------------------


def _gap_seconds(previous: Word, current: Word) -> float | None:
    if previous.end is None or current.start is None:
        return None
    return current.start - previous.end


def paragraphs_from_words(
    words: list[Word],
    *,
    paragraph_gap: float = PARAGRAPH_GAP_SECONDS,
    sentence_gap: float = SENTENCE_GAP_SECONDS,
    soft_cap: int = PARAGRAPH_SOFT_CAP_CHARS,
) -> list[Paragraph]:
    """Rebuild paragraph structure from the lecturer's own pacing.

    A paragraph ends at: a pause longer than ``paragraph_gap``; a spoken topic
    transition at a sentence start; or the soft cap, applied only at the next
    sentence boundary so no sentence is ever cut in half.
    """
    paragraphs: list[Paragraph] = []
    current: list[Word] = []
    current_len = 0
    sentence_terminated = True

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        text = " ".join(word.text for word in current if word.text).strip()
        if text:
            paragraphs.append(
                Paragraph(
                    text=text,
                    start=current[0].start if current[0].start is not None else None,
                    end=current[-1].end if current[-1].end is not None else None,
                )
            )
        current, current_len = [], 0

    for index, word in enumerate(words):
        previous = words[index - 1] if index else None
        gap = _gap_seconds(previous, word) if previous else None
        starts_sentence = (
            sentence_terminated or (gap is not None and gap > sentence_gap)
        )
        # A long pause separates thought blocks, mid-sentence or not.
        if current and gap is not None and gap > paragraph_gap:
            flush()
        elif current and starts_sentence:
            if current_len >= soft_cap:
                flush()
            else:
                # A spoken topic transition spans several words («حالا می‌رسیم
                # سراغ …»), so the marker is tested against a lookahead window.
                window = " ".join(
                    upcoming.text for upcoming in words[index : index + 8]
                )
                if starts_a_topic(window):
                    flush()
        current.append(word)
        current_len += len(word.text) + 1
        sentence_terminated = _ends_sentence(word.text)
    flush()
    return paragraphs


def paragraphs_from_text(
    text: str,
    *,
    soft_cap: int = PARAGRAPH_SOFT_CAP_CHARS,
) -> list[Paragraph]:
    """Paragraphing without timings: sentence spans + topic markers + cap."""
    paragraphs: list[Paragraph] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            joined = " ".join(current).strip()
            if joined:
                paragraphs.append(Paragraph(text=joined))
            current, current_len = [], 0

    for start, end in sentence_spans(text):
        sentence = text[start:end].strip()
        if not sentence:
            continue
        if current and (starts_a_topic(sentence) or current_len >= soft_cap):
            flush()
        current.append(sentence)
        current_len += len(sentence) + 1
    flush()
    return paragraphs


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------


def build_document(raw: RawTranscript) -> TranscriptDocument:
    """Turn engine output into the cleaned, paragraphed document.

    The timed-words path is preferred; the plain-text path is the fallback for
    engines that return no timings. Every paragraph text passes through the
    fidelity-gated typographic cleanup.

    When the engine diarized, paragraphs follow speaker turns instead of pauses,
    and the resolved speaker count travels with the document. A diarized run
    that resolves to a single voice is *not* labelled: headings on every
    paragraph of a solo lecture are noise, so the document keeps pause-based
    paragraphs and simply records that only one speaker was found.
    """
    if raw.words:
        words = raw.words
        speakers = 0
        if any(word.speaker for word in words):
            words, speakers = resolve_speakers(words)
        if speakers > 1:
            paragraphs = paragraphs_from_words_by_speaker(words)
        else:
            paragraphs = paragraphs_from_words(words)
        cleaned: list[Paragraph] = []
        for paragraph in paragraphs:
            text = guarded(paragraph.text, clean_typography, name="typography")
            if text:
                cleaned.append(replace(paragraph, text=text))
        if cleaned:
            return TranscriptDocument(
                tuple(cleaned),
                raw.confidence,
                speakers=max(speakers, raw.speakers, 0),
                diarization_attempted=bool(raw.speakers),
                overlap_count=len(overlap_spans(words)),
            )
        logger.warning("timed-words paragraphing produced nothing; falling back to text")

    text = guarded(raw.text, clean_typography, name="typography")
    paragraphs = paragraphs_from_text(text)
    if not paragraphs and text.strip():
        # The last possible fallback: one paragraph, unsplit but cleaned.
        paragraphs = [Paragraph(text=text.strip())]
    return TranscriptDocument(tuple(paragraphs), raw.confidence)


def render(document: TranscriptDocument, profile: str) -> str:
    """Render the document in a delivery profile (``plain``/``timestamped``)."""
    if profile == "timestamped":
        return document.timestamped()
    return document.plain()


# ---------------------------------------------------------------------------
# Chunking — the text handed to the note model, in model-sized pieces
# ---------------------------------------------------------------------------


def _split_single_line_paragraph(paragraph: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    for sentence_start, sentence_end in sentence_spans(paragraph):
        sentence = paragraph[sentence_start:sentence_end].strip()
        while len(sentence) > max_chars:
            window = sentence[:max_chars]
            cut = window.rfind(" ")
            if cut < max_chars // 2:
                cut = max_chars  # a single monster word: hard cut, no loss
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    packed: list[str] = []
    for piece in pieces:
        if packed and len(packed[-1]) + len(piece) + 1 <= max_chars:
            packed[-1] = packed[-1] + " " + piece
        else:
            packed.append(piece)
    return packed or [paragraph[:max_chars]]


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """Cut an oversized paragraph at line, sentence, then word boundaries."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if "\n" not in paragraph:
        return _split_single_line_paragraph(paragraph, max_chars)
    lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
    if len(lines) <= 1:
        return _split_single_line_paragraph(paragraph.strip(), max_chars)
    packed: list[str] = []
    for line in lines:
        line_pieces = (
            [line]
            if len(line) <= max_chars
            else _split_single_line_paragraph(line, max_chars)
        )
        for index, piece in enumerate(line_pieces):
            separator = "\n" if index == 0 else " "
            if packed and len(packed[-1]) + len(piece) + len(separator) <= max_chars:
                packed[-1] = packed[-1] + separator + piece
            else:
                packed.append(piece)
    return packed or [paragraph[:max_chars]]


def split_transcript(text: str, max_chars: int = TRANSCRIPT_CHUNK_CHARS) -> list[str]:
    """Split the LLM document into coherent, model-sized chunks.

    The document is divided at paragraph breaks so a definition, procedure or
    worked example is never cut mid-unit. Oversized paragraphs are then split
    at sentence boundaries, and only as a last resort at a word boundary.

    When a chunk is already mostly full and the next paragraph opens a new
    topic, the chunk ends there — the cut moves to the nearest meaningful
    boundary. No text is ever dropped or reordered: the concatenation of the
    returned chunks equals the input (token-for-token).
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    text = text.strip()
    if not text:
        return []

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n\n".join(current))
            current, current_len = [], 0

    for paragraph in paragraphs:
        pieces = [paragraph]
        if (
            current
            and all(item.lstrip().startswith("#") for item in current)
            and current_len + 2 + len(paragraph) > max_chars
        ):
            first_budget = max_chars - current_len - 2
            if first_budget >= max(20, max_chars // 5):
                initial = _split_long_paragraph(paragraph, first_budget)
                if len(initial) > 1:
                    sep = "\n" if "\n" in paragraph else " "
                    rest = sep.join(initial[1:])
                    pieces = [initial[0], *_split_long_paragraph(rest, max_chars)]
                else:
                    pieces = initial
            elif len(paragraph) > max_chars:
                pieces = _split_long_paragraph(paragraph, max_chars)
        elif len(paragraph) > max_chars:
            pieces = _split_long_paragraph(paragraph, max_chars)
        for piece_index, piece in enumerate(pieces):
            if (
                piece_index == 0
                and current
                and current_len >= max_chars * TOPIC_BREAK_MIN_FILL
                and starts_a_topic(piece)
            ):
                flush()
            extra = len(piece) + (2 if current else 0)
            if current and current_len + extra > max_chars:
                flush()
                extra = len(piece)
            current.append(piece)
            current_len += extra
    flush()
    return chunks
