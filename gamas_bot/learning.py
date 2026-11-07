"""Learning-loop content: prompts, strict JSON shapes and validation.

The quiz and AI-flashcard features both turn an archived booklet text into
strictly validated Persian JSON through :mod:`gamas_bot.llm`. This module keeps
every prompt, schema and clamp in one place so the bot layer only orchestrates
billing and screens, and the rules can be unit-tested without Telethon.

Nothing here performs I/O: functions are pure validators/parsers, which keeps
the billing-then-generation discipline in ``bot.py`` easy to follow.
"""

from __future__ import annotations

import json
import logging
import random
import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Any

from .progress import to_persian_digits

logger = logging.getLogger("gamas_bot.learning")

#: Generated booklet mirrors retain a bounded preview in the legacy library.
#: Uploaded documents retain their full extracted text and chunk cache instead.
MAX_CONTENT_CHARS = 60_000
#: Each learning chunk leaves room for the prompt envelope inside the model's
#: context budget; the generator can process multiple such chunks concurrently.
MAX_MODEL_INPUT_CHARS = 56_000

#: Validation ceilings for model output, mirrored in the prompt wording.
#: CR-5: scenario-based Apply/Analyze/Evaluate stems need a little more room
#: than a bare recall question, so the question ceiling is 400 chars.
MAX_QUESTION_CHARS = 400
MAX_OPTION_CHARS = 120
MAX_EXPLAIN_CHARS = 400
MAX_CARD_FRONT_CHARS = 160
MAX_CARD_BACK_CHARS = 400
#: Manual cards are user-typed in chat, so they get the slightly larger chat
#: budgets from the feature spec rather than the model-output ceilings.
MAX_MANUAL_FRONT_CHARS = 200
MAX_MANUAL_BACK_CHARS = 500

#: A generation that yields fewer usable units is a failure the caller refunds.
MIN_VALID_QUESTIONS = 3
MIN_VALID_CARDS = 5

#: Per-chunk model calls run concurrently through ``llm.complete_json`` (which
#: owns the provider failover). Four is the sweet spot measured against the
#: provider rate limits already configured for note generation: fewer makes a
#: long document slow, more starts tripping per-minute quotas.
CHUNK_CONCURRENCY = 4

#: Longest a generated item may cite: a page/slide locator plus a heading.
MAX_SOURCE_CHARS = 120

#: Size presets the user picks before generation: (code, label, cards, questions).
#: The label is what the button shows; the numbers are the requested counts.
SIZE_PRESETS: tuple[tuple[str, str, int, int], ...] = (
    ("quick", "⚡ سریع", 10, 5),
    ("standard", "⭐ استاندارد", 20, 10),
    ("full", "🎯 کامل", 40, 20),
)
SIZE_PRESET_CODES = tuple(preset[0] for preset in SIZE_PRESETS)


def preset_cards(code: str) -> int | None:
    for preset_code, _label, cards, _questions in SIZE_PRESETS:
        if preset_code == code:
            return cards
    return None


def distribute_counts(total: int, weights: list[int]) -> list[int]:
    """Split ``total`` items across chunks proportionally to their ``weights``.

    The result always sums to ``total``. Small presets are concentrated on
    the longest chunks, bounding the number of calls for large documents.
    """
    total = max(0, int(total))
    if not weights:
        return []
    if total == 0:
        return [0] * len(weights)
    if total <= len(weights):
        # Concentrate small presets on the longest chunks to bound LLM calls.
        selected_count = min(len(weights), total, max(2, total // 5))
        selected = sorted(range(len(weights)), key=lambda i: (-weights[i], i))[:selected_count]
        if selected_count == total:
            allocated = [1] * selected_count
        else:
            allocated = distribute_counts(total, [weights[i] for i in selected])
        counts = [0] * len(weights)
        for index, count in zip(selected, allocated):
            counts[index] = count
        return counts
    clean = [max(1, int(weight)) for weight in weights]
    pool = sum(clean)
    exact = [total * weight / pool for weight in clean]
    counts = [max(1, int(value)) for value in exact]
    # Hand out the remainder to the largest fractional parts first so the total
    # is exact instead of off by one.
    while sum(counts) < total:
        order = sorted(
            range(len(counts)), key=lambda index: exact[index] - counts[index], reverse=True
        )
        for index in order:
            if sum(counts) >= total:
                break
            counts[index] += 1
    while sum(counts) > total:
        # Only over-allocated when minimums were forced; trim the biggest chunk.
        index = max(range(len(counts)), key=lambda candidate: counts[candidate])
        if counts[index] <= 1:
            break
        counts[index] -= 1
    return counts


def normalize_front(text: str) -> str:
    """Normalize Persian/Arabic letter variants, digits, punctuation and case."""
    cleaned = unicodedata.normalize("NFKC", str(text or "")).casefold()
    cleaned = cleaned.translate(str.maketrans("يكى٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "یکی01234567890123456789"))
    return "".join(char for char in cleaned if char.isalnum())


def _dedupe_items(items: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    """Drop exact and near-identical stems while preserving meaningful numbers."""
    seen: list[str] = []
    unique: list[dict[str, Any]] = []
    for item in items:
        key = normalize_front(str(item.get(field, "")))
        if not key:
            continue
        numbers = re.findall(r"\d+", key)
        duplicate = any(
            key == previous or (
                len(key) >= 12 and len(previous) >= 12
                and numbers == re.findall(r"\d+", previous)
                and SequenceMatcher(None, key, previous, autojunk=False).ratio() >= 0.92
            )
            for previous in seen
        )
        if not duplicate:
            seen.append(key)
            unique.append(item)
    return unique


def dedupe_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _dedupe_items(cards, "front")


def dedupe_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _dedupe_items(questions, "question")

#: Persian option labels, indexed exactly like ``correct_index``.
OPTION_LABELS = ("الف", "ب", "ج", "د")


def bound_content_text(text: str | None, *, limit: int = MAX_CONTENT_CHARS) -> str:
    """Trim archived text to ``limit`` chars on a paragraph boundary.

    Cutting at the last blank line (falling back to the last whitespace, then a
    hard slice) never leaves a half-written paragraph as the quiz source.
    """
    if not text:
        return ""
    normalized = str(text).strip()
    if len(normalized) <= limit:
        return normalized
    window = normalized[:limit]
    paragraph_cut = window.rfind("\n\n")
    if paragraph_cut >= limit // 2:
        return window[:paragraph_cut].rstrip()
    line_cut = window.rfind("\n")
    if line_cut >= limit // 2:
        return window[:line_cut].rstrip()
    word_cut = window.rfind(" ")
    if word_cut >= limit // 2:
        return window[:word_cut].rstrip()
    return window.rstrip()


# ---------------------------------------------------------------------------
# Quiz
# ---------------------------------------------------------------------------

QUIZ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "correct": {"type": "integer"},
                    "explain": {"type": "string"},
                    # Generated questions require both tags. The parser can
                    # still read legacy rows when strict flags are disabled.
                    "difficulty": {"type": "string", "enum": ["medium", "hard"]},
                    # The generator verifies the locator belongs to the chunk;
                    # it never invents or back-fills an unverified source.
                    "source": {"type": "string"},
                },
                "required": ["question", "options", "correct", "explain", "difficulty", "source"],
            },
        }
    },
    "required": ["questions"],
}


def _source_rule(source: str | None, sources: tuple[str, ...] | None) -> str:
    choices = tuple(dict.fromkeys(
        " ".join(str(value).split())[:MAX_SOURCE_CHARS]
        for value in (sources or ((source,) if source else ())) if value
    ))
    if not choices:
        return ""
    allowed = "، ".join(f"«{locator}»" for locator in choices)
    return (
        "منبع‌دهی: هر مورد باید فقط از متن همان بخش ساخته شود. "
        "مقدار source را دقیقاً از فهرست نشانگرهای زیر انتخاب کن؛ "
        "اگر نمی‌توانی آن را به یکی از این بخش‌ها نسبت دهی، آن مورد را نساز:\n"
        f"{allowed}\n\n"
    )


def quiz_system_prompt(
    count: int, *, source: str | None = None, sources: tuple[str, ...] | None = None
) -> str:
    """Persian four-option, source-anchored MCQs across cognitive levels."""
    total = int(count)
    medium = max(1, round(total * 0.4))
    hard = max(0, total - medium)
    example_source = json.dumps((sources or (source or "صفحهٔ ۳",))[0], ensure_ascii=False)
    return (
        "تو طراح سؤال‌های چهارگزینه‌ای فارسی در سطح متوسط تا سخت هستی. فقط بر پایهٔ متن داده‌شده "
        "سؤال بساز؛ اطلاعات بیرونی یا ادعاهای بدون پشتوانه اضافه نکن.\n"
        f"دقیقاً {to_persian_digits(total)} سؤال بنویس.\n\n"
        "ترکیب آموزشی:\n"
        "- ترکیبی متعادل از یادآوری، درک مفهوم و به‌کاربستن بساز. "
        "برای به‌کاربستن، تحلیل و ارزیابی از سناریو کوتاه و «کدام گزینه» استفاده کن.\n"
        f"- حدود ۴۰٪ سؤال‌ها متوسط و ۶۰٪ سخت باشند؛ تقریباً {to_persian_digits(medium)} متوسط "
        f"و {to_persian_digits(hard)} سخت.\n"
        "- دشواری هر سؤال را در difficulty با medium یا hard مشخص کن.\n"
        "- هر سؤال دقیقاً ۴ گزینه دارد و فقط یک گزینه کاملاً درست است.\n"
        "- سه گزینهٔ نادرست باید باورپذیر، هم‌موضوع و بر پایهٔ بدفهمی‌های رایج باشند.\n"
        "- گزینه‌ها هم‌سبک و تقریباً هم‌طول باشند و پاسخ درست با نشانهٔ ظاهری لو نرود.\n"
        "- از «همهٔ موارد فوق»، مطلق‌نماها و گزینه‌های گمراه‌کنندهٔ بی‌ارتباط پرهیز کن.\n"
        "- برای هر پاسخ یک توضیح کوتاه و مستند به متن بنویس.\n\n"
        + _source_rule(source, sources)
        + "خروجی فقط JSON معتبر با این ساختار باشد و هیچ متن اضافه‌ای ننویس:\n"
        '{"questions":[{"question":"متن سؤال", "options":["الف", "ب", "ج", "د"], '
        '"correct":0, "explain":"توضیح کوتاه", "difficulty":"hard", '
        '"source":' + example_source + '}]}'
    )


def _clean_str(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()


def _clean_source(value: Any) -> str:
    """Read a locator the model may have emitted as a string *or* a number.

    Providers that ignore the JSON schema sometimes answer ``"source": 3`` for
    «صفحهٔ ۳». Treating that as "no source" silently discarded a perfectly
    anchored item and could empty a whole generation.
    """
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return _clean_str(str(value), MAX_SOURCE_CHARS)
    return _clean_str(value, MAX_SOURCE_CHARS)


#: Persian/English spellings of the two allowed difficulty tags. The schema asks
#: for ``medium``/``hard``, but a route without structured-output enforcement
#: answers in the language of the prompt; an unrecognized label used to reject
#: the whole question (S5: the tag is metadata and never causes rejection).
_DIFFICULTY_ALIASES: dict[str, str] = {
    "medium": "medium",
    "med": "medium",
    "moderate": "medium",
    "normal": "medium",
    "intermediate": "medium",
    "متوسط": "medium",
    "میانه": "medium",
    "معمولی": "medium",
    "متوسطروبهسخت": "medium",
    "hard": "hard",
    "harder": "hard",
    "difficult": "hard",
    "advanced": "hard",
    "challenging": "hard",
    "سخت": "hard",
    "دشوار": "hard",
    "سختتر": "hard",
    "خیلیسخت": "hard",
    "بسیارسخت": "hard",
    "پیشرفته": "hard",
}


def parse_difficulty(value: Any) -> str | None:
    """Map a model's difficulty label to ``medium``/``hard`` (or ``None``).

    ``normalize_front`` drops ZWNJ and punctuation and case-folds, so «سخت‌تر»,
    «HARD.» and «hard» all reach the same alias entry.
    """
    return _DIFFICULTY_ALIASES.get(normalize_front(value)) or None


#: Locator punctuation is cosmetic: «[بخش ۱]»، «بخش (۱/۳)» and «بخش 1 3» name
#: the same place. Comparison folds it, rendering never does.
_SOURCE_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)
_DIGIT_FOLDING = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"
)


def source_key(value: Any) -> str:
    """A comparison key for a page/slide/section locator.

    Digit shape, Persian/Arabic letter variants, brackets, quotes, slashes and
    spacing are all folded away, because a model re-types the locator it read
    («بخش ۱ (۱/۳)» can come back as «بخش 1 (1/3)» or «[بخش ۱]»). This is the
    comparison-only normalization P1 allows; the rendered locator keeps its
    original spelling.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_DIGIT_FOLDING)
    text = text.replace("ي", "ی").replace("ك", "ک").replace("ى", "ی").replace("ة", "ه")
    text = text.replace("\u200c", " ")
    text = _SOURCE_PUNCTUATION.sub(" ", text)
    return " ".join(text.split()).casefold()


def source_matches(source: Any, allowed: Iterable[str]) -> bool:
    """True when ``source`` names one of the chunk's own locators.

    A locator is never invented or repaired here: an item that names a section
    the model did not read stays invalid (the caller retries, then refunds).
    What *is* accepted is the same locator written differently, and a more
    specific variant of an allowed one («بخش ۱» vs «بخش ۱ (۲/۳)»), because a
    model that adds the part number of a split section is still citing that
    section. Prefix matching is token-aligned, so «بخش ۱» can never swallow
    «بخش ۱۰».
    """
    key = source_key(source)
    if not key:
        return False
    keys = {source_key(item) for item in allowed}
    keys.discard("")
    if not keys:
        # A chunk with no locator at all (legacy content) has nothing to verify
        # against; requiring a match here rejected *every* item and failed the
        # whole generation.
        return True
    for candidate in keys:
        if candidate == key:
            return True
        shorter, longer = (
            (candidate, key) if len(candidate) <= len(key) else (key, candidate)
        )
        if longer.startswith(shorter + " ") and len(shorter.split()) >= 2:
            return True
    return False


def source_grounded_in(source: Any, text: str) -> bool:
    """True when the cited locator literally occurs in the chunk that was read.

    A booklet keeps its own page/slide markers inside the archived text
    («[صفحهٔ ۲]»), so a model may cite that inner marker instead of the chunk
    label it was offered. Accepting it is not an invention: the string can be
    verified inside the very text the item was generated from. Matching is
    token-aligned and needs at least two tokens, so a bare «۱» in prose can
    never anchor an item.
    """
    key = source_key(source)
    if not key or len(key.split()) < 2:
        return False
    return f" {key} " in f" {source_key(text)} "


def verified_source(
    source: Any, allowed: Iterable[str], text: str = ""
) -> str | None:
    """The locator to store for one item, or ``None`` when it is unverifiable.

    Preference order, so nothing invented is ever stored:

    1. the chunk's own spelling of a locator the model actually named — a
       re-typed «بخش 1 (1/3)» is stored as the chunk label it means;
    2. a marker the model quoted that literally occurs in the text it read
       (a booklet's inner «[صفحهٔ ۲]»);
    3. ``None`` — the caller then anchors the item to the chunk it was
       generated from (see :func:`anchor_items`).

    This is the single place where a model-supplied locator becomes a stored
    one, so the generator and the parser can never disagree about what counts
    as verified.
    """
    if not source_key(source):
        return None
    for candidate in allowed:
        if candidate and source_matches(source, (candidate,)):
            return " ".join(str(candidate).split())[:MAX_SOURCE_CHARS]
    if text and source_grounded_in(source, text):
        return _clean_source(source)
    return None


def anchor_items(
    items: Iterable[dict[str, Any]],
    allowed: Iterable[str],
    fallback: str,
    text: str = "",
) -> list[dict[str, Any]]:
    """Attach a *verified* source to each generated item.

    An item was generated from exactly one chunk, so that chunk is itself a
    verifiable source: when the model's own locator cannot be checked, the
    item is anchored to the chunk label instead of being discarded. Losing a
    whole paid generation over a cosmetic locator disagreement was the failure
    mode this replaces, and it is still never fabrication — the stored locator
    is either verified against the chunk (1 and 2 above) or is the chunk's own
    label, never an unchecked model string.

    A chunk that carries no locator at all (legacy content) keeps the model's
    cleaned locator when it supplied one, because there is nothing to verify it
    against.
    """
    choices = tuple(allowed)
    fallback = " ".join(str(fallback or "").split())[:MAX_SOURCE_CHARS]
    anchored: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        locator = verified_source(item.get("source"), choices, text) or fallback
        if not locator:
            locator = _clean_source(item.get("source"))
        record = dict(item)
        if locator:
            record["source"] = locator
        anchored.append(record)
    return anchored


class GenerationShortfallError(RuntimeError):
    """A model answered, but with too few valid items to save (B4 minimums).

    Carries the counts so the caller can tell the user *why* the generation was
    not saved («۳ مورد از ۱۰») instead of a generic "unsuccessful", while the
    billing layer still refunds exactly once.
    """

    def __init__(
        self,
        *,
        kind: str,
        produced: int,
        required: int,
        requested: int,
        failures: int = 0,
        chunks: int = 0,
    ) -> None:
        super().__init__(
            f"{kind} produced {produced} valid item(s) of {requested} "
            f"({required} required, {failures}/{chunks} chunk(s) failed)"
        )
        self.kind = kind
        self.produced = int(produced)
        self.required = int(required)
        self.requested = int(requested)
        self.failures = int(failures)
        self.chunks = int(chunks)


#: Persian/latin option letters a model may use as the answer key. Only the
#: Persian set the product renders (OPTION_LABELS) and the latin equivalents.
_OPTION_LETTERS: dict[str, int] = {
    "a": 0, "b": 1, "c": 2, "d": 3,
    "الف": 0, "ب": 1, "ج": 2, "د": 3,
    "گزینهالف": 0, "گزینهب": 1, "گزینهج": 2, "گزینهد": 3,
    "optiona": 0, "optionb": 1, "optionc": 2, "optiond": 3,
}

_QUIZ_LIST_KEYS: tuple[str, ...] = (
    "questions", "quiz", "items", "mcqs", "سوالات", "سؤالات", "پرسش‌ها", "پرسشها",
)
_CARD_LIST_KEYS: tuple[str, ...] = (
    "cards", "flashcards", "items", "فلش‌کارت‌ها", "فلش کارت ها", "کارت‌ها", "کارتها",
)
_CORRECT_KEYS: tuple[str, ...] = (
    "correct", "correct_index", "answer", "correct_option", "پاسخ", "گزینه_صحیح",
)
_SEQUENTIAL_OPTION_PREFIXES: tuple[tuple[str, str, str, str], ...] = (
    ("الف", "ب", "ج", "د"),
    ("a", "b", "c", "d"),
    ("1", "2", "3", "4"),
)
_OPTION_LABEL_PREFIX_RE = re.compile(
    r"^\s*[\(\[]?\s*(الف|ب|ج|د|[a-dA-D]|[1-4۱-۴١-٤])\s*[\)\].:،\-–—]+\s*(.+)$"
)


def _correct_field(entry: dict[str, Any]) -> Any:
    for key in _CORRECT_KEYS:
        if key in entry and entry[key] is not None and entry[key] != "":
            return entry[key]
    return None


def _one_based_correct(raw_questions: list[Any]) -> bool:
    """True when a payload counts answer indexes from ``1`` instead of ``0``.

    A ``4`` cannot be a 0-based index, and a payload that also contains a ``0``
    is 0-based. Requiring both signals keeps a legitimate 0-based quiz whose
    key never points at the first option («correct: 2» ten times) from being
    shifted by one — a silently wrong answer key is worse than a dropped item.
    """
    values: list[int] = []
    for entry in raw_questions:
        if not isinstance(entry, dict):
            continue
        value = _correct_field(entry)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            values.append(value)
        elif isinstance(value, str):
            folded = value.strip().translate(_DIGIT_FOLDING)
            if folded.isdigit():
                values.append(int(folded))
    return bool(values) and 4 in values and 0 not in values


def _parse_correct(
    value: Any,
    *,
    one_based: bool,
    options: list[str] | None = None,
) -> int | None:
    """Read an answer key as a 0-based option index, or ``None``.

    Providers that ignore the JSON schema answer with the *letter* («ب», "B"),
    a 1-based position, or the exact option text itself; each is unambiguous
    once the payload's own style is known, and silently mis-grading is not an
    option.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        token = normalize_front(value)
        if token in _OPTION_LETTERS:
            return _OPTION_LETTERS[token]
        folded = value.strip().translate(_DIGIT_FOLDING)
        if folded.isdigit():
            index = int(folded)
            if one_based:
                index -= 1
            return index if 0 <= index <= 3 else None
        if options and len(options) == 4 and token:
            matches = [i for i, opt in enumerate(options) if normalize_front(opt) == token]
            if len(matches) == 1:
                return matches[0]
        return None
    if isinstance(value, int):
        index = value
        if one_based:
            index -= 1
        return index if 0 <= index <= 3 else None
    return None


def _strip_sequential_option_prefixes(items: list[str]) -> list[str]:
    matched = [_OPTION_LABEL_PREFIX_RE.match(opt) for opt in items]
    if all(m is not None for m in matched):
        labels = tuple(
            m.group(1).translate(_DIGIT_FOLDING).casefold()
            for m in matched if m is not None
        )
        if labels in _SEQUENTIAL_OPTION_PREFIXES:
            return [_clean_str(m.group(2), MAX_OPTION_CHARS) for m in matched if m is not None]
    return items


def _extract_options(entry: dict[str, Any]) -> list[str] | None:
    """Normalize 4 options from a list of strings/dicts or a 4-key dict."""
    raw: Any = None
    for key in ("options", "choices", "گزینه‌ها", "گزینه ها"):
        if key in entry and entry[key] is not None:
            raw = entry[key]
            break
    if isinstance(raw, dict):
        ordered: list[str] = []
        for group in (("الف", "ب", "ج", "د"), ("A", "B", "C", "D"), ("a", "b", "c", "d"), ("1", "2", "3", "4")):
            if all(k in raw for k in group):
                ordered = [_clean_str(raw[k], MAX_OPTION_CHARS) for k in group]
                break
        if not ordered and len(raw) == 4:
            ordered = [_clean_str(val, MAX_OPTION_CHARS) for val in raw.values()]
        return _strip_sequential_option_prefixes(ordered) if len(ordered) == 4 else None
    if not isinstance(raw, list):
        return None
    items: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            text_val = _entry_text(item, "text", "option", "label", "value", "متن")
            items.append(_clean_str(text_val, MAX_OPTION_CHARS))
        else:
            items.append(_clean_str(item, MAX_OPTION_CHARS))
    if len(items) != 4:
        return None
    return _strip_sequential_option_prefixes(items)


def _entry_text(entry: dict[str, Any], *keys: str) -> str:
    """The first non-empty string among ``keys`` (models rename fields)."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _source_field(entry: dict[str, Any]) -> Any:
    """The item's locator field, whatever the model called it."""
    for key in ("source", "منبع", "locator", "page", "section"):
        value = entry.get(key)
        if value is not None and value != "":
            return value
    return None


def _list_from_payload(payload: dict[str, Any], keys: tuple[str, ...]) -> list[Any] | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return None


def parse_quiz_payload(
    payload: dict[str, Any],
    count: int,
    *,
    rng: random.Random | None = None,
    require_source: bool = False,
    require_difficulty: bool = False,
) -> list[dict[str, Any]]:
    """Validate/clamp a model answer and return at most ``count`` questions.

    Malformed questions are dropped rather than failing the whole generation.
    Surviving options are shuffled and ``correct`` is remapped, so a model that
    favours option 0 cannot create a predictable answer key.

    Generation from either documents or booklets requires source and difficulty;
    the optional flags let legacy callers parse pre-existing untagged content.
    """
    raw_questions = _list_from_payload(payload, _QUIZ_LIST_KEYS)
    if not isinstance(raw_questions, list):
        return []
    one_based = _one_based_correct(raw_questions)
    validated: list[dict[str, Any]] = []
    for entry in raw_questions:
        if not isinstance(entry, dict):
            continue
        question = _clean_str(
            _entry_text(entry, "question", "q", "stem", "prompt", "سوال", "سؤال", "پرسش"),
            MAX_QUESTION_CHARS,
        )
        # «explanation» is what a model writes when it paraphrases the example
        # instead of copying it; the field name is cosmetic, the text is not.
        explain = _clean_str(
            _entry_text(entry, "explain", "explanation", "rationale", "reason", "توضیح", "پاسخ_تشریحی"),
            MAX_EXPLAIN_CHARS,
        )
        options = _extract_options(entry)
        if options is None:
            # The quiz schema requires four options. In particular, a three-option
            # numeric key is ambiguous and must never be silently mis-graded.
            logger.debug("dropped quiz item with invalid option shape")
            continue
        if not question or not explain:
            continue
        if len(options) != 4 or any(not option for option in options):
            continue
        option_keys = [normalize_front(option) for option in options]
        if any(not key for key in option_keys) or len(set(option_keys)) != 4:
            # Duplicate options make "single correct answer" ambiguous.
            continue
        correct = _parse_correct(
            _correct_field(entry), one_based=one_based, options=options
        )
        if not isinstance(correct, int) or isinstance(correct, bool) or not 0 <= correct <= 3:
            continue
        order = list(range(4))
        if rng is None:
            random.shuffle(order)
        else:
            rng.shuffle(order)
        shuffled = [options[index] for index in order]
        # Legacy callers may read older items without difficulty, whereas the
        # generation boundary explicitly requires a valid tag and source.
        record: dict[str, Any] = {
            "question": question,
            "options": shuffled,
            "correct_index": order.index(correct),
            "explanation": explain,
        }
        difficulty = parse_difficulty(
            entry.get("difficulty") if "difficulty" in entry else entry.get("سطح")
        )
        if difficulty is not None:
            record["difficulty"] = difficulty
        elif require_difficulty:
            # Legacy strictness only: generation records the tag when present
            # and never rejects a question over it (S5).
            continue
        source = _clean_source(_source_field(entry))
        if source:
            record["source"] = source
        elif require_source:
            # Unanchored item: nothing in the document says where it came from.
            continue
        validated.append(record)
        if len(validated) >= int(count):
            break
    return validated


# ---------------------------------------------------------------------------
# Flashcards
# ---------------------------------------------------------------------------

FLASHCARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "front": {"type": "string"},
                    "back": {"type": "string"},
                    # Source anchoring (page/slide/section locator).
                    "source": {"type": "string"},
                },
                "required": ["front", "back", "source"],
            },
        }
    },
    "required": ["cards"],
}


def flashcard_system_prompt(
    count: int, *, source: str | None = None, sources: tuple[str, ...] | None = None
) -> str:
    """Persian minimum-information, cloze-first and source-anchored cards."""
    total = int(count)
    example_source = json.dumps((sources or (source or "صفحهٔ ۳",))[0], ensure_ascii=False)
    return (
        "تو طراح فلش‌کارت آموزشی فارسی هستی. فقط از متن داده‌شده کارت بساز؛ "
        "اطلاعات بیرونی یا ادعاهای بدون پشتوانه اضافه نکن.\n"
        f"دقیقاً {to_persian_digits(total)} کارت بساز.\n\n"
        "قانون کمینه‌سازی اطلاعات:\n"
        "- هر کارت فقط یک واقعیت را بپرسد؛ پاسخ چندنکته‌ای را به چند کارت جدا بشکن.\n"
        "- فهرست‌های بیش از ۵ مورد را به کارت‌های شمارشی جداگانه تقسیم کن.\n"
        "- پرسش بله/خیر ممنوع است؛ هیچ کارت دو مفهوم بی‌ربط را نپرسد.\n"
        "- هر روی کارت حداکثر ۱۶۰ نویسه باشد و پاسخ بلند را خلاصه کن.\n"
        "- برای اصطلاح، تعریف و فرمول ابتدا از قالب جای‌خالی استفاده کن؛ "
        "برای مفهوم و رابطهٔ علت و معلول از پرسش‌وپاسخ کوتاه.\n\n"
        + _source_rule(source, sources)
        + "خروجی فقط JSON معتبر با این ساختار باشد و هیچ متن اضافه‌ای ننویس:\n"
        '{"cards":[{"front":"پرسش کوتاه", "back":"پاسخ یک‌خطی", "source":' + example_source + '}]}'
    )


def parse_flashcard_payload(
    payload: dict[str, Any], count: int, *, require_source: bool = False
) -> list[dict[str, str]]:
    """Validate/clamp a model answer into at most ``count`` Q/A cards.

    ``source`` is required for newly generated document and booklet cards;
    legacy callers may still parse older untagged items. A yes/no front is dropped.
    """
    raw_cards = _list_from_payload(payload, _CARD_LIST_KEYS)
    if not isinstance(raw_cards, list):
        return []
    validated: list[dict[str, str]] = []
    for entry in raw_cards:
        if not isinstance(entry, dict):
            continue
        # «question»/«answer» is a common paraphrase of the example's
        # «front»/«back»; a field name is cosmetic, losing a valid card is not.
        front = _clean_str(
            _entry_text(
                entry,
                "front", "question", "q", "prompt", "term",
                "روی_کارت", "پرسش", "سوال", "سؤال",
            ),
            MAX_CARD_FRONT_CHARS,
        )
        back = _clean_str(
            _entry_text(
                entry,
                "back", "answer", "a", "definition",
                "پشت_کارت", "پاسخ", "جواب",
            ),
            MAX_CARD_BACK_CHARS,
        )
        if not front or not back:
            continue
        if _is_yes_no(front, back):
            continue
        card = {"front": front, "back": back}
        source = _clean_source(_source_field(entry))
        if source:
            card["source"] = source
        elif require_source:
            continue
        validated.append(card)
        if len(validated) >= int(count):
            break
    return validated


_ALTERNATIVE_CHOICE_RE = re.compile(r"\sیا\s")
_YES_NO_ANSWERS: frozenset[str] = frozenset(
    {"بله", "خیر", "نه", "آری", "بلی", "درست", "نادرست", "صحیح", "غلط", "yes", "no", "true", "false"}
)


def _is_yes_no(front: str, back: str = "") -> bool:
    """True for a card whose whole answer is «بله»/«خیر» (banned by R5.1).

    Comparative «آیا A است یا B؟» cards are kept unless the answer itself is
    literally a yes/no word.
    """
    if back:
        norm_back = normalize_front(back)
        if norm_back in _YES_NO_ANSWERS:
            return True
    stripped = front.strip()
    starters = ("آیا ", "آیا‌", "ایا ")
    if not stripped.startswith(starters):
        return False
    if _ALTERNATIVE_CHOICE_RE.search(stripped):
        return False
    return True
