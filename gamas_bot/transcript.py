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


@dataclass(frozen=True, slots=True)
class Word:
    """One spoken token with its timing, punctuation already attached.

    ``start``/``end`` are seconds from the beginning of the audio, when the
    engine supplies them.
    """

    text: str
    start: float | None = None
    end: float | None = None


@dataclass(slots=True)
class RawTranscript:
    """What a speech engine returns: timed words when available, text always."""

    words: list[Word]
    text: str
    confidence: float | None = None
    engine: str = ""

    @classmethod
    def from_text(cls, text: str, confidence: float | None = None, engine: str = "") -> "RawTranscript":
        return cls(words=[], text=text, confidence=confidence, engine=engine)


@dataclass(frozen=True, slots=True)
class Paragraph:
    text: str
    start: float | None = None
    end: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptDocument:
    """The cleaned, paragraphed transcript — the LLM input and the .txt body."""

    paragraphs: tuple[Paragraph, ...]
    confidence: float | None = None

    @property
    def text(self) -> str:
        return self.plain()

    def plain(self) -> str:
        return "\n\n".join(paragraph.text for paragraph in self.paragraphs if paragraph.text)

    def timestamped(self) -> str:
        """``[mm:ss]`` (Persian digits) at each paragraph that has a timing."""
        lines: list[str] = []
        for paragraph in self.paragraphs:
            if not paragraph.text:
                continue
            if paragraph.start is not None:
                minutes, seconds = divmod(int(paragraph.start), 60)
                stamp = to_persian_digits(f"{minutes:02d}:{seconds:02d}")
                lines.append(f"[{stamp}] {paragraph.text}")
            else:
                lines.append(paragraph.text)
        return "\n\n".join(lines)

    def llm_document(self) -> str:
        """The text passed to the note model: clean paragraphs, no timestamps."""
        return self.plain()


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
    """
    if raw.words:
        paragraphs = paragraphs_from_words(raw.words)
        cleaned: list[Paragraph] = []
        for paragraph in paragraphs:
            text = guarded(paragraph.text, clean_typography, name="typography")
            if text:
                cleaned.append(replace(paragraph, text=text))
        if cleaned:
            return TranscriptDocument(tuple(cleaned), raw.confidence)
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
