"""Content generation for the learning loop: quizzes and flashcard decks.

This module owns everything between "the user asked for a quiz" and "the
database has a validated question set": the two Persian prompts, the JSON
schemas sent as structured-output hints, and — most importantly — the
validation that turns whatever a provider returned into something the bot is
willing to charge for.

Two rules shape the validation:

* **Nothing unvalidated is persisted.** A model answer is untrusted input. A
  question with three options, an out-of-range correct index or a blank stem is
  dropped rather than repaired, because a quiz the user paid for must never
  contain a question that cannot be answered.
* **Options are shuffled before storage.** Providers tend to put the correct
  answer first; shuffling here (and remapping the index) is what makes the
  stored order the order the user saw, so ``correct_index`` stays meaningful
  without a second "was it shuffled?" flag.

Generation itself stays in this module so the bot's handlers only deal with
screens and credit — the same separation ``notes.py`` uses for booklets.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import aiohttp

from .config import Settings
from .llm import complete_json

logger = logging.getLogger(__name__)

#: Upper bound on the artifact text handed to the model. A booklet is normally
#: far smaller; the cap exists so one pathological transcript cannot turn a quiz
#: request into a context-overflow retry storm.
CONTENT_TEXT_LIMIT = 60_000

#: Minimum usable items below which generation is considered failed (and the
#: credit is refunded) rather than delivered half-empty.
MIN_QUIZ_QUESTIONS = 3
MIN_FLASHCARDS = 5

#: Field budgets. These bound the rendered Telegram message, not the model's
#: context: a 3 000-character "question" is unusable in a chat bubble.
MAX_QUESTION_CHARS = 300
MAX_OPTION_CHARS = 120
MAX_EXPLANATION_CHARS = 400
MAX_CARD_FRONT_CHARS = 160
MAX_CARD_BACK_CHARS = 400

QUIZ_SYSTEM_PROMPT = (
    "شما طرّاح آزمون فارسی «گاماس» هستید. ورودی شما متن یک جزوهٔ درسی است و خروجی شما "
    "فقط یک شیء JSON معتبر، بدون هیچ متن اضافه، با این ساختار است: "
    '{"questions":[{"question":"...","options":["...","...","...","..."],'
    '"correct":0,"explain":"..."}]}\n\n'
    "قواعد:\n"
    "- دقیقاً به همان تعدادی که خواسته شده سؤال بسازید.\n"
    "- هر سؤال چهار گزینه داشته باشد و فقط یکی از آن‌ها بی‌ابهام درست باشد.\n"
    "- سؤال و گزینه‌ها و توضیح کاملاً فارسی و کوتاه باشند؛ گزینه‌ها هم‌طول و هم‌جنس باشند.\n"
    "- سؤال‌ها فقط از همین متن ساخته شوند؛ هیچ دانش بیرونی، عدد تازه یا حدس اضافه نکنید.\n"
    "- «correct» شمارهٔ گزینهٔ درست از صفر است (۰ تا ۳).\n"
    "- «explain» یک جملهٔ کوتاه توضیح می‌دهد که چرا آن گزینه درست است و به متن جزوه تکیه دارد.\n"
    "- سؤال‌های تکراری یا سؤال‌هایی که پاسخشان در متن نیست نسازید.\n"
    "- گزینه‌ها را با شماره یا حرف («۱)»، «الف») شروع نکنید؛ فقط متن گزینه را بنویسید."
)

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
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "correct": {"type": "integer", "minimum": 0, "maximum": 3},
                    "explain": {"type": "string"},
                },
                "required": ["question", "options", "correct"],
            },
        }
    },
    "required": ["questions"],
}

FLASHCARD_SYSTEM_PROMPT = (
    "شما سازندهٔ فلش‌کارت فارسی «گاماس» هستید. ورودی شما متن یک جزوهٔ درسی است و خروجی شما "
    "فقط یک شیء JSON معتبر، بدون هیچ متن اضافه، با این ساختار است: "
    '{"cards":[{"front":"...","back":"..."}]}\n\n'
    "قواعد:\n"
    "- دقیقاً به همان تعدادی که خواسته شده کارت بسازید.\n"
    "- «front» یک پرسش کوتاه و روشن از متن جزوه است و «back» پاسخ کامل اما فشردهٔ همان پرسش.\n"
    "- هر کارت فقط یک نکته را بپرسد؛ پرسش‌های بله/خیر و پرسش‌های چندبخشی نسازید.\n"
    "- همهٔ کارت‌ها فقط از همین متن ساخته شوند؛ هیچ دانش بیرونی یا حدس اضافه نکنید.\n"
    "- کارت‌های تکراری نسازید و پاسخ را داخل «front» لو ندهید."
)

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


class LearningContentError(RuntimeError):
    """The provider answered, but not with content worth charging for."""


def bound_content(text: str | None, limit: int = CONTENT_TEXT_LIMIT) -> str:
    """Clamp archived text, cutting on a paragraph boundary when possible.

    Cutting mid-sentence is what makes a generated question unanswerable, so the
    last paragraph break before the limit wins; a single huge paragraph falls
    back to a hard cut rather than keeping nothing.
    """
    if not text:
        return ""
    value = str(text).strip()
    if len(value) <= limit:
        return value
    window = value[:limit]
    for separator in ("\n\n", "\n", "。"):
        cut = window.rfind(separator)
        if cut >= limit // 2:
            return window[:cut].rstrip()
    return window.rstrip()


def _clean(value: object, limit: int) -> str:
    """Collapse whitespace and clamp one provider-supplied string."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()


def normalize_questions(
    payload: object,
    count: int,
    *,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """Validate, clamp, shuffle and truncate a provider's quiz payload.

    Anything malformed is dropped; the result is then cut to ``count`` so a
    chatty provider cannot quietly hand the user (or charge them for) more
    questions than they picked.
    """
    raw = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    chooser = rng or random.Random()
    questions: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        question = _clean(entry.get("question"), MAX_QUESTION_CHARS)
        options_raw = entry.get("options")
        if not isinstance(options_raw, list) or len(options_raw) != 4:
            continue
        options = [_clean(option, MAX_OPTION_CHARS) for option in options_raw]
        if not question or any(not option for option in options):
            continue
        # Duplicate options make more than one answer defensible, which is
        # exactly the ambiguity the prompt asks the model to avoid.
        if len(set(options)) != 4:
            continue
        correct_raw = entry.get("correct")
        if isinstance(correct_raw, bool) or not isinstance(correct_raw, int):
            continue
        if not 0 <= correct_raw <= 3:
            continue
        explanation = _clean(entry.get("explain"), MAX_EXPLANATION_CHARS)
        # Remap the index across the shuffle: options and ``correct`` are always
        # written together, so the stored index refers to the stored order.
        indices = list(range(4))
        chooser.shuffle(indices)
        questions.append(
            {
                "question": question,
                "options": [options[index] for index in indices],
                "correct": indices.index(correct_raw),
                "explanation": explanation,
            }
        )
        if len(questions) >= max(0, int(count)):
            break
    return questions


def normalize_cards(payload: object) -> list[dict[str, str]]:
    """Validate and clamp a provider's flashcard payload, dropping malformed cards."""
    raw = payload.get("cards") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        front = _clean(entry.get("front"), MAX_CARD_FRONT_CHARS)
        back = _clean(entry.get("back"), MAX_CARD_BACK_CHARS)
        if not front or not back:
            continue
        key = front.casefold()
        if key in seen:
            continue
        seen.add(key)
        cards.append({"front": front, "back": back})
    return cards


async def generate_quiz(
    session: aiohttp.ClientSession,
    settings: Settings,
    text: str,
    count: int,
    *,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """Ask the note provider for ``count`` questions grounded in ``text``.

    Raises :class:`LearningContentError` when the usable result is below
    :data:`MIN_QUIZ_QUESTIONS`; the caller refunds the charge in that case, so a
    weak provider answer never costs the user credit.
    """
    payload = await complete_json(
        session,
        settings,
        system=QUIZ_SYSTEM_PROMPT,
        user=f"تعداد سؤال: {int(count)}\n\nمتن جزوه:\n{bound_content(text)}",
        schema=QUIZ_SCHEMA,
    )
    questions = normalize_questions(payload, count, rng=rng)
    if len(questions) < min(MIN_QUIZ_QUESTIONS, int(count)):
        raise LearningContentError(
            f"only {len(questions)} usable questions of {count} requested"
        )
    logger.info("quiz generated questions=%d requested=%d", len(questions), count)
    return questions


async def generate_flashcards(
    session: aiohttp.ClientSession,
    settings: Settings,
    text: str,
    size: int,
) -> list[dict[str, str]]:
    """Ask the note provider for a flashcard deck grounded in ``text``."""
    payload = await complete_json(
        session,
        settings,
        system=FLASHCARD_SYSTEM_PROMPT,
        user=f"تعداد کارت: {int(size)}\n\nمتن جزوه:\n{bound_content(text)}",
        schema=FLASHCARD_SCHEMA,
    )
    cards = normalize_cards(payload)
    if len(cards) < MIN_FLASHCARDS:
        raise LearningContentError(
            f"only {len(cards)} usable cards of {size} requested"
        )
    return cards[: max(MIN_FLASHCARDS, int(size))]


def deck_document_body(cards: list[dict[str, Any]]) -> str:
    """Render a deck as the plain Markdown the RTL export understands."""
    blocks = [
        f"پرسش {index}: {card['front']}\nپاسخ: {card['back']}"
        for index, card in enumerate(cards, start=1)
    ]
    return "\n\n".join(blocks)
