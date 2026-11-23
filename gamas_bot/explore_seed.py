"""Idempotent seeding of the curated Explore question bank.

The bank itself lives in :mod:`gamas_bot.explore_bank` (200 hand-written
questions). This module owns everything about *putting it in the database*:

* a stable identity per question (``explore_questions.seed_key``, migration 042),
  so re-running the seed inserts nothing twice and never rewrites a user's row;
* a normalized-text duplicate check, so a seed question never shadows - or is
  shadowed by - a question that already exists under different wording;
* retirement of the placeholder rows written by the previous generator, which
  emitted ten questions twenty times each with a ``(پرسش N)`` suffix. Those rows
  are machine-made duplicates of ten facts, so they are demoted out of the feed
  (``status='rejected'``) rather than deleted: no impression, reward or audit row
  is touched, and an admin can restore one if they wish;
* validation, so a malformed bank fails the test suite and the CLI instead of
  reaching production.

Nothing here resets a database, deletes a row, or touches a question that a user
or an admin created (``created_by IS NOT NULL``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from functools import lru_cache
from typing import Any, Iterable

from .explore_bank import BANK, TOPICS, SeedQuestion

#: Any pair of questions whose normalized token sets overlap at least this much
#: is treated as a conceptual duplicate (Jaccard). The real bank stays far below
#: it; see ``tests/test_explore_seed_bank.py`` for the measured maximum.
CONCEPT_SIMILARITY_LIMIT = 0.60

#: The exact question texts the pre-042 generator shipped (ten facts, each
#: repeated with a `` (پرسش N)`` suffix). Only rows that match one of these -
#: as-is or with that suffix - and carry no author are ever retired.
_LEGACY_GENERATED_TEXTS: tuple[str, ...] = (
    "واحد بنیادی ساختار و عملکرد در موجودات زنده چیست؟",
    "پایتخت کشور ایران کدام شهر است؟",
    "واحد اندازه‌گیری نیرو در دستگاه SI چیست؟",
    "نماد شیمیایی عنصر اکسیژن کدام است؟",
    "حاصل جمع زوایای داخلی یک مثلث چند درجه است؟",
    "تمدن هخامنشی به دست چه کسی بنیان‌گذاری شد؟",
    "سرایندهٔ شاهنامه کیست؟",
    "نزدیک‌ترین ستاره به زمین کدام است؟",
    "مخفف CPU در رایانه به چه معناست؟",
    "طولانی‌ترین شب سال در تقویم ایرانی چه نام دارد؟",
)

_LEGACY_SUFFIX_RE = re.compile(r"\s*\(پرسش\s*[0-9۰-۹]+\s*\)\s*$")

class _DryRunRollback(Exception):
    """Signals ``dry_run`` to undo the trial transaction."""


#: Arabic/Persian letter variants folded to one form before comparing texts.
_LETTER_FOLD = {
    "ي": "ی",
    "ى": "ی",
    "ك": "ک",
    "ة": "ه",
    "ۀ": "ه",
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ؤ": "و",
    "ئ": "ی",
}
_DIACRITICS_RE = re.compile(r"[\u064B-\u0652\u0670\u0640\u200c\u200d\u200e\u200f]")


def normalize_question_text(text: str) -> str:
    """Fold a question to the form used for duplicate detection.

    Case, Arabic/Persian letter variants, diacritics, ZWNJ, punctuation and
    spacing all disappear, so ``«آب در چه دمایی یخ می‌زند؟»`` and
    ``"اب در چه دمایی یخ میزند"`` compare equal.
    """
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    folded = _DIACRITICS_RE.sub("", folded)
    folded = "".join(_LETTER_FOLD.get(ch, ch) for ch in folded)
    kept = [ch for ch in folded if ch.isalnum() or ch.isspace()]
    return " ".join("".join(kept).split())


#: Words that appear in almost every question and therefore say nothing about
#: what it asks. Dropping them makes the similarity check compare *content*.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "کدام", "کدامیک", "کدام‌یک", "چه", "چی", "چیست", "چند", "چقدر", "کجا", "کی",
        "است", "هستند", "هست", "بود", "باشند", "باشد", "شد", "شده", "شود", "شوند",
        "می", "نمی", "کند", "کنند", "کرد", "دارد", "دارند", "داشت", "نام", "نامیده",
        "را", "در", "از", "به", "با", "برای", "که", "این", "آن", "یک", "و", "یا",
        "بر", "روی", "بین", "هر", "همه", "هیچ", "نیز", "ترین", "si", "کدامند",
    }
)


@lru_cache(maxsize=2048)
def question_tokens(text: str) -> frozenset[str]:
    """Content tokens used for the conceptual-duplicate check (cached)."""
    return frozenset(
        token
        for token in normalize_question_text(text).split()
        if (len(token) > 1 or token.isdigit()) and token not in _STOPWORDS
    )


def question_similarity(left: str, right: str) -> float:
    """Jaccard similarity of two question texts' token sets."""
    a = question_tokens(left)
    b = question_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_legacy_generated(text: str) -> bool:
    """Whether *text* was produced by the pre-042 placeholder generator."""
    stripped = _LEGACY_SUFFIX_RE.sub("", str(text or ""))
    normalized = normalize_question_text(stripped)
    return any(
        normalize_question_text(legacy) == normalized
        for legacy in _LEGACY_GENERATED_TEXTS
    )


def validate_seed_bank(bank: Iterable[SeedQuestion] = BANK) -> list[str]:
    """Return every structural or uniqueness problem in *bank* (empty = valid)."""
    items = list(bank)
    errors: list[str] = []
    if len(items) != 200:
        errors.append(f"expected 200 questions, found {len(items)}")

    seen_keys: dict[str, str] = {}
    seen_texts: dict[str, str] = {}
    for item in items:
        where = f"{item.key}"
        if not item.key.strip():
            errors.append(f"{where}: empty key")
        if item.key in seen_keys:
            errors.append(f"{where}: duplicate key (also {seen_keys[item.key]})")
        seen_keys.setdefault(item.key, item.key)

        if item.topic not in TOPICS:
            errors.append(f"{where}: unknown topic {item.topic!r}")
        if not item.text.strip() or not item.text.strip().endswith("؟"):
            errors.append(f"{where}: question text must be a non-empty '؟' question")
        if _LEGACY_SUFFIX_RE.search(item.text):
            errors.append("{}: numbered placeholder suffix is not allowed".format(where))

        normalized = normalize_question_text(item.text)
        if not normalized:
            errors.append(f"{where}: empty normalized text")
        if normalized in seen_texts:
            errors.append(
                f"{where}: duplicate question text (also {seen_texts[normalized]})"
            )
        seen_texts.setdefault(normalized, item.key)

        options = tuple(item.options)
        if len(options) != 4:
            errors.append(f"{where}: needs exactly 4 options")
        if any(not str(option).strip() for option in options):
            errors.append(f"{where}: empty option")
        if len({str(option).strip() for option in options}) != len(options):
            errors.append(f"{where}: duplicated options")
        if not isinstance(item.correct_index, int) or not 0 <= item.correct_index < len(options):
            errors.append(f"{where}: correct_index {item.correct_index!r} out of range")
        if not item.explanation.strip():
            errors.append(f"{where}: empty explanation")

    for index, first in enumerate(items):
        for second in items[index + 1:]:
            similarity = question_similarity(first.text, second.text)
            if similarity >= CONCEPT_SIMILARITY_LIMIT:
                errors.append(
                    f"{first.key}/{second.key}: conceptual duplicate "
                    f"(similarity {similarity:.2f})"
                )
    return errors


def topic_distribution(bank: Iterable[SeedQuestion] = BANK) -> dict[str, int]:
    """Count questions per topic, in the canonical topic order."""
    counts: dict[str, int] = dict.fromkeys(TOPICS, 0)
    for item in bank:
        counts[item.topic] = counts.get(item.topic, 0) + 1
    return counts


async def seed_explore_questions(
    db: Any,
    *,
    dry_run: bool = False,
    retire_legacy: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    """Insert every missing bank question; safe to run any number of times.

    Returns a report of what happened (``inserted``, ``skipped_existing``,
    ``skipped_duplicate``, ``retired_legacy``, ``bank``). ``dry_run=True`` runs
    the same checks and then rolls the transaction back.
    """
    errors = validate_seed_bank()
    if errors:
        raise ValueError("invalid Explore seed bank: " + "; ".join(errors[:5]))

    report: dict[str, Any] = {
        "bank": len(BANK),
        "inserted": 0,
        "skipped_existing": 0,
        "skipped_duplicate": 0,
        "retired_legacy": 0,
        "dry_run": bool(dry_run),
        "topics": topic_distribution(),
    }

    async def _run() -> dict[str, Any]:
        from .db_core import _rows, iso

        created = now or iso(db._now())
        retired_ids: set[int] = set()
        if retire_legacy:
            rows = await _rows(
                db.db,
                "SELECT question_id, question_text FROM explore_questions "
                "WHERE status = 'approved' AND created_by IS NULL AND seed_key IS NULL",
            )
            legacy_ids = [
                int(row["question_id"])
                for row in rows
                if _is_legacy_generated(str(row["question_text"]))
            ]
            for question_id in legacy_ids:
                await db.db.execute(
                    "UPDATE explore_questions SET status = 'rejected' WHERE question_id = ?",
                    (question_id,),
                )
            retired_ids = set(legacy_ids)
            report["retired_legacy"] = len(legacy_ids)

        existing_keys = {
            str(row["seed_key"])
            for row in await _rows(
                db.db,
                "SELECT seed_key FROM explore_questions WHERE seed_key IS NOT NULL",
            )
        }
        # A placeholder retired a moment ago must not block its replacement: the
        # bank is allowed to reuse the wording of a legacy question, and the row
        # that carries that wording is no longer in the feed.
        existing_texts = {
            normalize_question_text(str(row["question_text"]))
            for row in await _rows(
                db.db, "SELECT question_id, question_text FROM explore_questions"
            )
            if int(row["question_id"]) not in retired_ids
        }

        for item in BANK:
            if item.key in existing_keys:
                report["skipped_existing"] += 1
                continue
            normalized = normalize_question_text(item.text)
            if normalized in existing_texts:
                # A real question already says this; never insert a twin.
                report["skipped_duplicate"] += 1
                continue
            await db.db.execute(
                """
                INSERT INTO explore_questions(
                    topic, difficulty, question_text, options_json,
                    correct_index, explanation, status, created_by, created_at,
                    seed_key
                ) VALUES (?, 'easy', ?, ?, ?, ?, 'approved', NULL, ?, ?)
                """,
                (
                    item.topic,
                    item.text,
                    json.dumps(list(item.options), ensure_ascii=False),
                    int(item.correct_index),
                    item.explanation,
                    created,
                    item.key,
                ),
            )
            existing_keys.add(item.key)
            existing_texts.add(normalized)
            report["inserted"] += 1
        if dry_run:
            raise _DryRunRollback
        return report

    try:
        async with db._transaction():
            return await _run()
    except _DryRunRollback:
        return report


def _format_report(report: dict[str, Any]) -> str:
    lines = [
        f"bank: {report['bank']} questions (dry_run={report['dry_run']})",
        f"inserted: {report['inserted']}",
        f"skipped (already seeded): {report['skipped_existing']}",
        f"skipped (duplicate text): {report['skipped_duplicate']}",
        f"retired legacy placeholders: {report['retired_legacy']}",
        "topics:",
    ]
    for topic, count in report.get("topics", {}).items():
        lines.append(f"  {topic}: {count}")
    return "\n".join(lines)


async def _main(argv: list[str] | None = None) -> int:
    from .db import Database

    parser = argparse.ArgumentParser(
        description="Validate and seed the curated Explore question bank."
    )
    parser.add_argument("--db", help="path to the SQLite database to seed")
    parser.add_argument("--dry-run", action="store_true", help="check without writing")
    parser.add_argument(
        "--keep-legacy",
        action="store_true",
        help="do not demote the numbered placeholder rows of the old generator",
    )
    parser.add_argument(
        "--validate-only", action="store_true", help="only validate the bank"
    )
    args = parser.parse_args(argv)

    errors = validate_seed_bank()
    if errors:
        for error in errors[:20]:
            print("ERROR:", error)
        return 1
    if args.validate_only or not args.db:
        print(
            f"OK: {len(BANK)} unique, well-formed Explore questions across "
            f"{len(TOPICS)} topics."
        )
        for topic, count in topic_distribution().items():
            print(f"  {topic}: {count}")
        return 0

    db = Database(args.db)
    await db.connect()
    try:
        report = await seed_explore_questions(
            db, dry_run=args.dry_run, retire_legacy=not args.keep_legacy
        )
    finally:
        await db.close()
    print(_format_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(asyncio.run(_main()))
