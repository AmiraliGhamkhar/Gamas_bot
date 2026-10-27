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
                },
                "required": ["question", "options", "correct", "explain"],
            },
        }
    },
    "required": ["questions"],
}


def quiz_system_prompt(count: int) -> str:
    """The Persian system prompt demanding ``count`` grounded, harder MCQs.

    CR-5: questions move from pure recall to mid-to-hard Apply/Analyze/Evaluate
    items built around a short scenario, with homogeneous, misconception-based
    distractors and no giveaway cues. Pure (no I/O) so it is unit-testable.
    """
    total = int(count)
    medium = max(1, round(total * 0.4))
    hard = max(0, total - medium)
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
        "خروجی فقط JSON معتبر با این ساختار باشد و هیچ متن اضافه‌ای ننویس:\n"
        '{"questions":[{"question": "متن سؤال سناریومحور", '
        '"options": ["گزینه الف", "گزینه ب", "گزینه ج", "گزینه د"], '
        '"correct": 0, "explain": "توضیح یک‌خطی", "difficulty": "hard"}]}'
    )


def _clean_str(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()


def parse_quiz_payload(
    payload: dict[str, Any], count: int, *, rng: random.Random | None = None
) -> list[dict[str, Any]]:
    """Validate/clamp a model answer and return at most ``count`` questions.

    Malformed questions are dropped rather than failing the whole generation.
    Surviving options are shuffled and ``correct`` is remapped, so a model that
    favours option 0 cannot create a predictable answer key.
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
                },
                "required": ["front", "back"],
            },
        }
    },
    "required": ["cards"],
}


def flashcard_system_prompt(count: int) -> str:
    """The Persian system prompt demanding ``count`` grounded Q/A pairs."""
    return (
        "تو یک طراح فلش‌کارت آموزشی فارسی‌زبان هستی. فقط از متن جزوه‌ای که در "
        "پیام کاربر می‌آید کارت بساز؛ هیچ اطلاعات بیرونی وارد نکن.\n"
        f"دقیقاً {int(count)} کارت پرسش‌وپاسخ بساز.\n"
        "قوانین:\n"
        "- «front» پرسش کوتاه و «back» پاسخ کامل در یک پاراگراف کوتاه است.\n"
        "- کارت‌ها روی نکات کلیدی، تعریف‌ها و مفاهیم مهم تمرکز کنند.\n"
        "- همهٔ متن‌ها فارسی و روان باشد.\n"
        "خروجی فقط JSON معتبر با این ساختار باشد و هیچ متن اضافه‌ای ننویس:\n"
        '{"cards":[{"front": "پرسش", "back": "پاسخ"}]}'
    )


def parse_flashcard_payload(
    payload: dict[str, Any], count: int
) -> list[dict[str, str]]:
    """Validate/clamp a model answer into at most ``count`` Q/A cards."""
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
        validated.append({"front": front, "back": back})
        if len(validated) >= int(count):
            break
    return validated
