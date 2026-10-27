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
import random
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from .progress import to_persian_digits

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


def preset_questions(code: str) -> int | None:
    for preset_code, _label, _cards, questions in SIZE_PRESETS:
        if preset_code == code:
            return questions
    return None


def distribute_counts(total: int, weights: list[int]) -> list[int]:
    """Split ``total`` items across chunks proportionally to their ``weights``.

    Every chunk gets at least one item while the budget allows, and the result
    always sums to ``total`` — so a 40-card deck is 40 cards no matter how the
    document was chunked. When there are more chunks than items, the extra
    chunks get zero and the caller simply skips them.
    """
    total = max(0, int(total))
    if not weights:
        return []
    if total <= len(weights):
        # Spread a small preset across the whole document rather than silently
        # taking only its first pages. Select weighted midpoint quantiles.
        clean = [max(1, int(weight)) for weight in weights]
        pool = sum(clean)
        counts = [0] * len(clean)
        cumulative = 0
        index = 0
        for slot in range(total):
            target = (slot + 0.5) * pool / total
            while index < len(clean) - 1 and cumulative + clean[index] < target:
                cumulative += clean[index]
                index += 1
            # One item per selected chunk when there are more chunks than
            # items; choose the nearest remaining position for heavy pages.
            candidate = index
            if counts[candidate]:
                candidate = min(
                    (i for i, value in enumerate(counts) if not value),
                    key=lambda i: (abs(i - index), i),
                )
            counts[candidate] = 1
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
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        return []
    validated: list[dict[str, Any]] = []
    for entry in raw_questions:
        if not isinstance(entry, dict):
            continue
        question = _clean_str(entry.get("question"), MAX_QUESTION_CHARS)
        explain = _clean_str(entry.get("explain"), MAX_EXPLAIN_CHARS)
        raw_options = entry.get("options")
        correct = entry.get("correct")
        if not question or not explain or not isinstance(raw_options, list):
            continue
        options = [
            _clean_str(option, MAX_OPTION_CHARS) for option in raw_options
        ]
        if len(options) != 4 or any(not option for option in options):
            continue
        if len(set(options)) != 4:
            # Duplicate options make "single correct answer" ambiguous.
            continue
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
        difficulty = entry.get("difficulty")
        if difficulty in ("medium", "hard"):
            record["difficulty"] = difficulty
        elif require_difficulty:
            continue
        source = _clean_str(entry.get("source"), MAX_SOURCE_CHARS)
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
    raw_cards = payload.get("cards")
    if not isinstance(raw_cards, list):
        return []
    validated: list[dict[str, str]] = []
    for entry in raw_cards:
        if not isinstance(entry, dict):
            continue
        front = _clean_str(entry.get("front"), MAX_CARD_FRONT_CHARS)
        back = _clean_str(entry.get("back"), MAX_CARD_BACK_CHARS)
        if not front or not back:
            continue
        if _is_yes_no(front):
            continue
        card = {"front": front, "back": back}
        source = _clean_str(entry.get("source"), MAX_SOURCE_CHARS)
        if source:
            card["source"] = source
        elif require_source:
            continue
        validated.append(card)
        if len(validated) >= int(count):
            break
    return validated


def _is_yes_no(front: str) -> bool:
    """True for a card whose whole answer is «بله»/«خیر» (banned by R5.1)."""
    stripped = front.strip()
    starters = ("آیا ", "آیا‌", "ایا ")
    return stripped.startswith(starters)
