"""Learning-loop content: prompts, strict JSON shapes and validation.

The quiz and AI-flashcard features both turn an archived booklet text into
strictly validated Persian JSON through :mod:`gamas_bot.llm`. This module keeps
every prompt, schema and clamp in one place so the bot layer only orchestrates
billing and screens, and the rules can be unit-tested without Telethon.

Nothing here performs I/O: functions are pure validators/parsers, which keeps
the billing-then-generation discipline in ``bot.py`` easy to follow.
"""

from __future__ import annotations

import random
from typing import Any

from .progress import to_persian_digits

#: Archived learning text is capped before it is stored and again before it is
#: sent to a model, so a multi-hour lecture can never bloat the library row or
#: overflow a provider context (one LLM call, no chunking, by design).
MAX_CONTENT_CHARS = 60_000
#: Input sent to the learning generators is slightly tighter than the stored
#: cap to leave room for the prompt envelope inside NOTE_CHUNK_CHARS budgets.
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
        return [1 if index < total else 0 for index in range(len(weights))]
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
    """A comparison key for deduplicating card fronts (case/space/punct blind)."""
    cleaned = " ".join(str(text or "").split()).casefold()
    return "".join(char for char in cleaned if char.isalnum())


def dedupe_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop cards whose normalized front already appeared (order preserved)."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for card in cards:
        key = normalize_front(str(card.get("front", "")))
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(card)
    return unique


def dedupe_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop questions whose normalized stem already appeared."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for question in questions:
        key = normalize_front(str(question.get("question", "")))
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(question)
    return unique

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
                    # CR-5: optional difficulty tag. ``required`` is left
                    # unchanged so a model that omits it is never rejected —
                    # that protects the MIN_VALID_QUESTIONS refund path.
                    "difficulty": {"type": "string", "enum": ["medium", "hard"]},
                    # Source anchoring: the page/slide/section locator the
                    # question was built from. Optional in the schema for the
                    # same reason as ``difficulty`` (never reject a whole item);
                    # the caller back-fills the chunk locator when it is absent.
                    "source": {"type": "string"},
                },
                "required": ["question", "options", "correct", "explain"],
            },
        }
    },
    "required": ["questions"],
}


def quiz_system_prompt(count: int, *, source: str | None = None) -> str:
    """The Persian system prompt demanding ``count`` grounded, harder MCQs.

    CR-5: questions move from pure recall to mid-to-hard Apply/Analyze/Evaluate
    items built around a short scenario, with homogeneous, misconception-based
    distractors and no giveaway cues.

    ``source`` names the page/slide/section the model is reading in this call
    (chunk-parallel generation feeds one chunk at a time). When it is given, the
    prompt asks the model to copy that locator into ``source`` on every item; the
    caller still back-fills it, so a forgetful model never breaks anchoring.
    """
    total = int(count)
    medium = max(1, round(total * 0.4))
    hard = max(0, total - medium)
    locator = " ".join(str(source or "").split())[:MAX_SOURCE_CHARS]
    source_rule = (
        "منبع‌دهی: هر سؤال فقط از متن همین بخش ساخته شود و مقدار \"source\" دقیقاً "
        f"همان نشانگر «{locator}» باشد.\n\n"
        if locator
        else ""
    )
    return (
        "تو طراح سؤال‌های چهارگزینه‌ای سطح «متوسط تا سخت» برای یک جزوهٔ فارسی هستی. "
        "فقط بر پایهٔ متن جزوه‌ای که در پیام کاربر می‌آید سؤال بساز؛ هیچ اطلاعات بیرونی وارد نکن.\n"
        f"دقیقاً {to_persian_digits(total)} سؤال بنویس.\n\n"
        "سطح دشواری:\n"
        f"- حدود ۴۰٪ سؤال‌ها «متوسط» و ۶۰٪ «سخت» باشند (برای {to_persian_digits(total)} سؤال، "
        f"تقریباً {to_persian_digits(medium)} متوسط و {to_persian_digits(hard)} سخت).\n"
        "- سؤال‌ها باید در سطح «به‌کاربستن»، «تحلیل» یا «ارزیابی» باشند، نه یادآوری صرف.\n"
        "- سؤال حافظه‌ای ممنوع است: مستقیم نپرس «X چیست؟»، «X را تعریف کن»، «X چند است؟»، "
        "«چه کسی/چه سالی …؟» یا «کدام گزینه نام/عدد/تاریخ درست را می‌گوید؟».\n"
        "- ساختار هر سؤال: یک سناریوی کوتاه (مثلاً «اگر …»، «بیماری …»، «دانشجویی …») ← "
        "پرسش ← پایان با «کدام گزینه …؟».\n\n"
        "قوانین گزینه‌ها:\n"
        "- هر سؤال دقیقاً ۴ گزینه دارد و فقط یک گزینه کاملاً درست است.\n"
        "- گزینه‌های نادرست باید برآمده از خطای رایج یا بدفهمی دانشجو، باورپذیر و نزدیک به پاسخ درست باشند.\n"
        "- هر ۴ گزینه هم‌سبک و تقریباً هم‌طول باشند؛ پاسخ درست نباید بلندتر، دقیق‌تر یا "
        "از نظر ساختار متفاوت باشد.\n"
        "- از مطلق‌نماهای لو‌دهنده («همیشه»، «هرگز»، «هیچ‌کدام»، «همهٔ موارد فوق») پرهیز کن؛ "
        "گزینهٔ «همهٔ موارد فوق» به‌کلی ممنوع است و پاسخ درست نباید با حذف گزینه‌ها قابل حدس باشد.\n"
        "- برای هر سؤال یک توضیح یک‌خطی فارسی بنویس که چرا گزینهٔ درست، درست است.\n"
        "- همهٔ متن‌ها فارسی و روان باشد.\n\n"
        "ترکیب سطوح شناختی:\n"
        "- حدود یک‌سوم سؤال‌ها «یادآوری»، یک‌سوم «درک» و یک‌سوم «به‌کاربستن» باشند؛ "
        "هیچ سطحی بیش از نیمی از سؤال‌ها را نگیرد.\n"
        "- گزینه‌های نادرست از خطاهای رایج همان مبحث انتخاب شوند، نه از موضوعی دیگر.\n\n"
        + source_rule
        + "خروجی فقط JSON معتبر با این ساختار باشد و هیچ متن اضافه‌ای ننویس:\n"
        '{"questions":[{"question": "متن سؤال سناریومحور", '
        '"options": ["گزینه الف", "گزینه ب", "گزینه ج", "گزینه د"], '
        '"correct": 0, "explain": "توضیح یک‌خطی", "difficulty": "hard", '
        '"source": "صفحهٔ ۳"}]}'
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
) -> list[dict[str, Any]]:
    """Validate/clamp a model answer and return at most ``count`` questions.

    Malformed questions are dropped rather than failing the whole generation.
    Surviving options are shuffled and ``correct`` is remapped, so a model that
    favours option 0 cannot create a predictable answer key.

    ``require_source`` is used for document-sourced generation, where an item
    the model could not anchor is discarded rather than delivered unattributed;
    booklet generation leaves it off and the caller back-fills the chunk locator.
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
        # CR-5: record the optional difficulty tag when the model supplies a
        # valid one. It is deliberately *not* required — a missing/invalid tag
        # must never drop a question (that would break MIN_VALID_QUESTIONS and
        # the refund path). The option shuffle above is unchanged.
        record: dict[str, Any] = {
            "question": question,
            "options": shuffled,
            "correct_index": order.index(correct),
            "explanation": explain,
        }
        difficulty = entry.get("difficulty")
        if difficulty in ("medium", "hard"):
            record["difficulty"] = difficulty
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
                "required": ["front", "back"],
            },
        }
    },
    "required": ["cards"],
}


def flashcard_system_prompt(count: int, *, source: str | None = None) -> str:
    """The Persian system prompt demanding ``count`` grounded, atomic cards.

    Encodes the two rules that separate a deck people actually finish from a
    wall of paragraphs: **minimum information** (one fact per card, lists longer
    than five items split into enumeration cards) and **cloze-first** phrasing
    for terms and formulas, Q/A for concepts and cause → effect. ``source``
    names the page/slide/section of the chunk being read.
    """
    total = int(count)
    locator = " ".join(str(source or "").split())[:MAX_SOURCE_CHARS]
    source_rule = (
        "منبع‌دهی: هر کارت فقط از متن همین بخش ساخته شود و مقدار \"source\" دقیقاً "
        f"همان نشانگر «{locator}» باشد؛ اگر مطلبی در این بخش نیست، کارت نساز.\n"
        if locator
        else ""
    )
    return (
        "تو یک طراح فلش‌کارت آموزشی فارسی‌زبان هستی. فقط از متنی که در "
        "پیام کاربر می‌آید کارت بساز؛ هیچ اطلاعات بیرونی وارد نکن.\n"
        f"دقیقاً {to_persian_digits(total)} کارت بساز.\n\n"
        "قانون طلایی (کمینه‌سازی اطلاعات):\n"
        "- هر کارت فقط یک واقعیت را بپرسد؛ اگر جواب چند نکته دارد، آن را به چند کارت بشکن.\n"
        "- فهرست‌های بیش از ۵ مورد را به کارت‌های شمارشی جداگانه بشکن "
        "(مثلاً «سه نوع … کدام‌اند؟» ← یک کارت برای هر نوع).\n"
        "- پرسش بله/خیر ممنوع است.\n"
        "- هر روی کارت حداکثر حدود ۳۲۰ نویسه باشد؛ پاسخ بلندتر را خلاصه کن.\n"
        "- هیچ کارتی دو مفهوم بی‌ربط را با هم نپرسد.\n\n"
        "قالب کارت:\n"
        "- برای اصطلاح‌ها، تعریف‌ها، فرمول‌ها و اعداد ← کارت جای‌خالی (cloze): "
        "«…… چیست؟» یا «در فرمول ……، نماد X نشانهٔ چیست؟»\n"
        "- برای مفاهیم، روابط علت و معلولی و مقایسه ← پرسش‌وپاسخ کوتاه.\n"
        "- پاسخ باید از متن قابل استناد باشد، نه دانش عمومی.\n"
        "- همهٔ متن‌ها فارسی و روان باشد.\n\n"
        + source_rule
        + "خروجی فقط JSON معتبر با این ساختار باشد و هیچ متن اضافه‌ای ننویس:\n"
        '{"cards":[{"front": "پرسش کوتاه", "back": "پاسخ یک‌خطی", "source": "صفحهٔ ۳"}]}'
    )


def parse_flashcard_payload(
    payload: dict[str, Any], count: int, *, require_source: bool = False
) -> list[dict[str, str]]:
    """Validate/clamp a model answer into at most ``count`` Q/A cards.

    ``source`` is kept when present (it is optional, exactly like a quiz
    question's difficulty) and required only for document-sourced generation.
    A yes/no front is dropped: it is never a useful card.
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
