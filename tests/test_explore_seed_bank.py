"""Phase 4: the Explore seed bank is 200 genuinely distinct questions.

The old bank was ten questions repeated with a "(پرسش N)" suffix, so "200
questions" was a number, not a bank. These tests hold the replacement to the
claim: 200 distinct concepts, valid shapes, an idempotent seed, no damage to
anything a user published, compatibility with the recommender, and no way for
the seed's size to leak into the product.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from gamas_bot.db import Database
from gamas_bot.explore_bank import BANK, TOPICS, SeedQuestion
from gamas_bot.explore_recommender import recommend_explore_feed
from gamas_bot.explore_seed import (
    CONCEPT_SIMILARITY_LIMIT,
    _main,
    normalize_question_text,
    question_similarity,
    seed_explore_questions,
    topic_distribution,
    validate_seed_bank,
)
from gamas_bot.webapp_api import make_wsgi_app
from tests.support import make_settings
from tests.test_webapp_api import BOT_TOKEN, _call_wsgi, _sign_init_data

USER = 777


def _max_similarity() -> float:
    worst = 0.0
    for index, first in enumerate(BANK):
        for second in BANK[index + 1:]:
            worst = max(worst, question_similarity(first.text, second.text))
    return worst


class SeedBankContentTests(unittest.TestCase):
    """The bank itself: 200 distinct, valid, balanced questions."""

    def test_bank_has_exactly_two_hundred_questions(self) -> None:
        self.assertEqual(len(BANK), 200)
        self.assertEqual(len({q.key for q in BANK}), 200)

    def test_no_exact_duplicates(self) -> None:
        texts = [normalize_question_text(q.text) for q in BANK]
        self.assertEqual(len(set(texts)), 200)
        self.assertEqual([t for t, n in Counter(texts).items() if n > 1], [])

    def test_no_conceptual_duplicates(self) -> None:
        worst = _max_similarity()
        self.assertLess(worst, CONCEPT_SIMILARITY_LIMIT, f"max similarity {worst:.2f}")
        self.assertEqual(validate_seed_bank(), [])

    def test_no_suffix_or_numbering_tricks(self) -> None:
        for question in BANK:
            self.assertNotRegex(question.text, r"\(پرسش|#\s*\d+|سؤال\s*\d+")
        # Every question is a different concept, not a re-worded prefix of another.
        prefixes = Counter(
            normalize_question_text(q.text)[:24] for q in BANK
        )
        self.assertEqual([p for p, n in prefixes.items() if n > 1], [])

    def test_correct_answers_are_valid_and_not_positionally_predictable(self) -> None:
        for question in BANK:
            self.assertIsInstance(question.correct_index, int)
            self.assertIn(question.correct_index, range(4), question.key)
            self.assertEqual(len(question.options), 4)
            self.assertEqual(len(set(question.options)), 4, question.key)
            self.assertTrue(all(option.strip() for option in question.options))
            self.assertTrue(question.options[question.correct_index].strip())
            self.assertTrue(question.explanation.strip(), question.key)
        positions = Counter(q.correct_index for q in BANK)
        for position in range(4):
            self.assertGreaterEqual(
                positions[position], 30, f"answer at position {position}: {positions}"
            )

    def test_topic_metadata_is_valid_and_balanced(self) -> None:
        for question in BANK:
            self.assertIn(question.topic, TOPICS, question.key)
            self.assertIsInstance(question.topic, str)
        counts = topic_distribution()
        self.assertEqual(set(counts), set(TOPICS))
        self.assertEqual(sum(counts.values()), 200)
        # No topic crowds out the rest, and no topic is nominal.
        self.assertLessEqual(max(counts.values()), 24)
        self.assertGreaterEqual(min(counts.values()), 12)
        self.assertGreaterEqual(len(TOPICS), 8)

    def test_validation_rejects_broken_entries(self) -> None:
        good = BANK[0]
        cases = {
            "unknown topic": SeedQuestion("x-1", "ناموجود", good.text, good.options, 0, "شرح"),
            "empty explanation": SeedQuestion("x-2", good.topic, good.text, good.options, 0, " "),
            "bad index": SeedQuestion("x-3", good.topic, good.text, good.options, 7, "شرح"),
            "duplicated option": SeedQuestion(
                "x-4", good.topic, good.text, (good.options[0],) * 4, 0, "شرح"
            ),
            "suffixed placeholder": SeedQuestion(
                "x-5", good.topic, f"{good.text} (پرسش 12)", good.options, 0, "شرح"
            ),
        }
        for label, bad in cases.items():
            errors = validate_seed_bank([*BANK[1:], bad])
            self.assertTrue(errors, label)
        # Two copies of one question are caught by the normalized text.
        errors = validate_seed_bank([*BANK[:-1], BANK[0]._replace(key="dup-key")])
        self.assertIn("duplicate question text", " ".join(errors))

    def test_normalization_folds_variants(self) -> None:
        self.assertEqual(
            normalize_question_text("آب در چه دمایی يخ می‌زند؟"),
            normalize_question_text("اب در چه دمایی یخ میزند"),
        )
        self.assertEqual(
            normalize_question_text("آب  در چه دمایی،  یخ می‌زند!"),
            normalize_question_text("آب در چه دمایی یخ می‌زند"),
        )
        self.assertNotEqual(
            normalize_question_text("نماد اکسیژن چیست؟"),
            normalize_question_text("نماد آهن چیست؟"),
        )


class SeedBankDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN,
            db_path=self.root / "explore_seed.db",
            temp_dir=self.root / "tmp",
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(USER, "Seed Reader")
        await self.db.ensure_user(888, "Publisher")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def _count(self, sql: str, params: tuple = ()) -> int:
        rows = await self.db.db.execute_fetchall(sql, params)
        return int(rows[0][0])

    async def test_seed_inserts_two_hundred_distinct_approved_rows(self) -> None:
        report = await seed_explore_questions(self.db)

        self.assertEqual(report["inserted"], 200)
        self.assertEqual(report["skipped_existing"], 0)
        self.assertEqual(report["bank"], 200)
        self.assertEqual(
            await self._count("SELECT COUNT(*) FROM explore_questions"), 200
        )
        self.assertEqual(
            await self._count(
                "SELECT COUNT(*) FROM explore_questions WHERE status = 'approved'"
            ),
            200,
        )
        self.assertEqual(
            await self._count(
                "SELECT COUNT(*) FROM explore_questions WHERE seed_key IS NOT NULL"
            ),
            200,
        )
        # Every stored row is a different question, stored with 4 options.
        rows = await self.db.db.execute_fetchall(
            "SELECT seed_key, topic, question_text, options_json, correct_index, "
            "explanation FROM explore_questions"
        )
        self.assertEqual(len({str(r["seed_key"]) for r in rows}), 200)
        self.assertEqual(
            len({normalize_question_text(str(r["question_text"])) for r in rows}), 200
        )
        for row in rows:
            options = json.loads(row["options_json"])
            self.assertEqual(len(options), 4)
            self.assertEqual(len(set(options)), 4)
            self.assertIn(int(row["correct_index"]), range(4))
            self.assertIn(str(row["topic"]), TOPICS)
            self.assertTrue(str(row["explanation"]).strip())

    async def test_seed_is_idempotent_and_never_duplicates(self) -> None:
        first = await seed_explore_questions(self.db)
        second = await seed_explore_questions(self.db)
        third = await seed_explore_questions(self.db)

        self.assertEqual(first["inserted"], 200)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["skipped_existing"], 200)
        self.assertEqual(third["inserted"], 0)
        self.assertEqual(
            await self._count("SELECT COUNT(*) FROM explore_questions"), 200
        )
        # A re-run also leaves the rows themselves untouched.
        before = await self.db.db.execute_fetchall(
            "SELECT question_id, question_text, correct_index FROM explore_questions "
            "ORDER BY question_id"
        )
        await seed_explore_questions(self.db)
        after = await self.db.db.execute_fetchall(
            "SELECT question_id, question_text, correct_index FROM explore_questions "
            "ORDER BY question_id"
        )
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    async def test_dry_run_writes_nothing(self) -> None:
        report = await seed_explore_questions(self.db, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["inserted"], 200)
        self.assertEqual(
            await self._count("SELECT COUNT(*) FROM explore_questions"), 0
        )

    async def test_seed_key_uniqueness_is_enforced_by_the_database(self) -> None:
        await seed_explore_questions(self.db)
        row = await self.db.db.execute_fetchall(
            "SELECT seed_key, topic, question_text, options_json, correct_index "
            "FROM explore_questions LIMIT 1"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.db.execute(
                "INSERT INTO explore_questions(topic, question_text, options_json, "
                "correct_index, explanation, status, created_at, seed_key) "
                "VALUES ('تاریخ', 'تکراری؟', '[]', 0, '', 'approved', '2026-01-01', ?)",
                (str(row[0]["seed_key"]),),
            )
        await self.db.db.rollback()

    async def test_user_published_questions_are_never_touched(self) -> None:
        # An admin-approved user question and a pending share, both authored.
        await self.db.db.execute(
            "INSERT INTO explore_questions(topic, difficulty, question_text, "
            "options_json, correct_index, explanation, status, created_by, created_at) "
            "VALUES ('عمومی', 'easy', 'سؤال کاربر دربارهٔ آسمан؟', '[\"الف\",\"ب\",\"ج\",\"د\"]', "
            "1, 'شرح کاربر', 'approved', 888, '2026-01-01')"
        )
        await self.db.db.execute(
            "INSERT INTO explore_questions(topic, difficulty, question_text, "
            "options_json, correct_index, explanation, status, created_by, created_at) "
            "VALUES ('عمومی', 'easy', 'سؤال در انتظار بررسی؟', '[\"الف\",\"ب\",\"ج\",\"د\"]', "
            "0, '', 'pending', 888, '2026-01-01')"
        )
        await self.db.db.commit()

        report = await seed_explore_questions(self.db)

        self.assertEqual(report["inserted"], 200)
        rows = await self.db.db.execute_fetchall(
            "SELECT question_text, status, created_by, seed_key FROM explore_questions "
            "WHERE created_by IS NOT NULL ORDER BY question_id"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(str(rows[0]["status"]), "approved")
        self.assertEqual(str(rows[1]["status"]), "pending")
        self.assertTrue(all(row["seed_key"] is None for row in rows))

    async def test_seed_never_inserts_a_twin_of_a_user_question(self) -> None:
        twin = BANK[0]
        await self.db.db.execute(
            "INSERT INTO explore_questions(topic, difficulty, question_text, "
            "options_json, correct_index, explanation, status, created_by, created_at) "
            "VALUES (?, 'easy', ?, ?, 2, 'پاسخ کاربر', 'approved', 888, '2026-01-01')",
            (twin.topic, twin.text, json.dumps(list(twin.options), ensure_ascii=False)),
        )
        await self.db.db.commit()

        report = await seed_explore_questions(self.db)

        self.assertEqual(report["inserted"], 199)
        self.assertEqual(report["skipped_duplicate"], 1)
        self.assertEqual(
            await self._count(
                "SELECT COUNT(*) FROM explore_questions WHERE question_text = ?",
                (twin.text,),
            ),
            1,
        )
        self.assertEqual(
            await self._count(
                "SELECT COUNT(*) FROM explore_questions WHERE seed_key IS NOT NULL"
            ),
            199,
        )

    async def test_legacy_placeholder_rows_are_retired_not_deleted(self) -> None:
        """The old generator's numbered duplicates leave the feed, keep their rows."""
        legacy_question = await self.db.db.execute(
            "INSERT INTO explore_questions(topic, difficulty, question_text, "
            "options_json, correct_index, explanation, status, created_by, created_at) "
            "VALUES ('جغرافیا', 'easy', 'پایتخت کشور ایران کدام شهر است؟ (پرسش 7)', "
            "'[\"تهران\",\"شیراز\",\"یزد\",\"کرج\"]', 0, '', 'approved', NULL, '2026-01-01')"
        )
        legacy_id = int(legacy_question.lastrowid or 0)
        await self.db.db.execute(
            "INSERT INTO explore_impressions(user_id, question_id, shown_at, dwell_ms, signal) "
            "VALUES (?, ?, '2026-01-01', 5000, 3)",
            (USER, legacy_id),
        )
        await self.db.db.commit()

        report = await seed_explore_questions(self.db)

        self.assertEqual(report["retired_legacy"], 1)
        row = await self.db.db.execute_fetchall(
            "SELECT status FROM explore_questions WHERE question_id = ?", (legacy_id,)
        )
        self.assertEqual(str(row[0]["status"]), "rejected")
        # The row and the engagement history that referenced it survive.
        self.assertEqual(
            await self._count(
                "SELECT COUNT(*) FROM explore_impressions WHERE question_id = ?",
                (legacy_id,),
            ),
            1,
        )
        self.assertEqual(report["inserted"], 200)

    async def test_upgrading_a_legacy_bank_yields_two_hundred_real_questions(self) -> None:
        """The exact pre-042 output (10 questions × 20, with suffixes) is replaced."""
        legacy = (
            ("زیست‌شناسی", "واحد بنیادی ساختار و عملکرد در موجودات زنده چیست؟", "سلول"),
            ("جغرافیا", "پایتخت کشور ایران کدام شهر است؟", "تهران"),
            ("فیزیک", "واحد اندازه‌گیری نیرو در دستگاه SI چیست؟", "نیوتن"),
            ("شیمی", "نماد شیمیایی عنصر اکسیژن کدام است؟", "O"),
            ("ریاضی", "حاصل جمع زوایای داخلی یک مثلث چند درجه است؟", "۱۸۰"),
            ("تاریخ", "تمدن هخامنشی به دست چه کسی بنیان‌گذاری شد؟", "کوروش بزرگ"),
            ("ادبیات", "سرایندهٔ شاهنامه کیست؟", "فردوسی"),
            ("نجوم", "نزدیک‌ترین ستاره به زمین کدام است؟", "خورشید"),
            ("فناوری", "مخفف CPU در رایانه به چه معناست؟", "واحد پردازش مرکزی"),
            ("فرهنگ عمومی", "طولانی‌ترین شب سال در تقویم ایرانی چه نام دارد؟", "شب یلدا"),
        )
        for index in range(200):
            topic, text, answer = legacy[index % len(legacy)]
            suffix = "" if index < len(legacy) else f" (پرسش {index + 1})"
            options = [answer, "گزینهٔ دوم", "گزینهٔ سوم", "گزینهٔ چهارم"]
            await self.db.db.execute(
                "INSERT INTO explore_questions(topic, difficulty, question_text, "
                "options_json, correct_index, explanation, status, created_at) "
                "VALUES (?, 'easy', ?, ?, 0, 'شرح قدیمی', 'approved', '2026-01-01')",
                (topic, f"{text}{suffix}", json.dumps(options, ensure_ascii=False)),
            )
        await self.db.db.commit()
        self.assertEqual(
            await self._count(
                "SELECT COUNT(*) FROM explore_questions WHERE status = 'approved'"
            ),
            200,
        )

        report = await seed_explore_questions(self.db)

        self.assertEqual(report["retired_legacy"], 200)
        self.assertEqual(report["inserted"], 200)
        self.assertEqual(report["skipped_duplicate"], 0)
        rows = await self.db.db.execute_fetchall(
            "SELECT question_text FROM explore_questions WHERE status = 'approved'"
        )
        self.assertEqual(len(rows), 200)
        # Nothing numbered and none of the four-option placeholders survive in the feed.
        self.assertFalse(
            [r for r in rows if "(پرسش" in str(r["question_text"])]
        )
        feed = await recommend_explore_feed(self.db, USER, limit=10)
        self.assertEqual(len(feed), 10)
        served = {item["question_text"] for item in feed}
        self.assertFalse({text for text in served if "(پرسش" in text})

    async def test_legacy_retirement_can_be_disabled(self) -> None:
        await self.db.db.execute(
            "INSERT INTO explore_questions(topic, question_text, options_json, "
            "correct_index, explanation, status, created_at) "
            "VALUES ('جغرافیا', 'پایتخت کشور ایران کدام شهر است؟ (پرسش 3)', "
            "'[\"تهران\",\"یزد\",\"کرج\",\"قم\"]', 0, '', 'approved', '2026-01-01')"
        )
        await self.db.db.commit()

        report = await seed_explore_questions(self.db, retire_legacy=False)

        self.assertEqual(report["retired_legacy"], 0)
        self.assertEqual(
            await self._count(
                "SELECT COUNT(*) FROM explore_questions WHERE status = 'approved'"
            ),
            201,
        )

    async def test_seeded_questions_are_served_by_the_recommender(self) -> None:
        await seed_explore_questions(self.db)
        feed = await recommend_explore_feed(self.db, USER, limit=10)

        self.assertEqual(len(feed), 10)
        self.assertEqual(len({item["question_id"] for item in feed}), 10)
        for item in feed:
            self.assertIn(item["topic"], TOPICS)
            self.assertEqual(item["difficulty"], "easy")
            self.assertEqual(len(item["options"]), 4)
            self.assertNotIn("explanation", item)
            self.assertNotIn("correct_index", item)
        # Diversity: no more than two neighbours share a topic.
        topics = [item["topic"] for item in feed]
        for index in range(2, len(topics)):
            self.assertFalse(topics[index] == topics[index - 1] == topics[index - 2])

    async def test_seen_questions_are_excluded_and_topics_drive_affinity(self) -> None:
        await seed_explore_questions(self.db)
        first = await recommend_explore_feed(self.db, USER, limit=10)
        # Whatever the user answered/skipped is recorded as an impression...
        for item in first[:3]:
            await self.db.db.execute(
                "INSERT INTO explore_impressions(user_id, question_id, shown_at, "
                "dwell_ms, signal, is_correct) VALUES (?, ?, '2026-11-23T00:00:00+00:00', "
                "4000, 3, 1)",
                (USER, int(item["question_id"])),
            )
        await self.db.db.commit()

        second = await recommend_explore_feed(self.db, USER, limit=10)
        seen = {int(item["question_id"]) for item in first[:3]}
        served = {int(item["question_id"]) for item in second}
        self.assertEqual(seen & served, set())
        self.assertTrue(served)

    async def test_new_questions_stay_reachable_when_seen_fill_the_window(self) -> None:
        """Fresh approved questions survive even when the user has already seen
        everything an old fixed candidate window would have scanned (regression:
        seen rows used to consume the window and starve newer content)."""
        await seed_explore_questions(self.db)
        for i in range(60):
            await self.db.db.execute(
                "INSERT INTO explore_questions(topic, difficulty, question_text, "
                "options_json, correct_index, explanation, status, created_at) "
                "VALUES ('فیزیک', 'easy', ?, '[\"الف\", \"ب\", \"ج\", \"د\"]', "
                "0, '', 'approved', '2026-11-24T00:00:00+00:00')",
                (f"سؤال تازهٔ شمارهٔ {i}",),
            )
        # The user has already seen the oldest 250 approved questions.
        await self.db.db.execute(
            "INSERT INTO explore_impressions(user_id, question_id, shown_at, "
            "dwell_ms, signal) SELECT ?, question_id, '2026-11-23T00:00:00+00:00', "
            "4000, 3 FROM explore_questions WHERE status = 'approved' "
            "ORDER BY question_id ASC LIMIT 250",
            (USER,),
        )
        await self.db.db.commit()

        feed = await recommend_explore_feed(self.db, USER, limit=10)
        self.assertEqual(len(feed), 10)
        # Only the questions beyond that seen window may be served.
        self.assertTrue(all(int(item["question_id"]) > 250 for item in feed))

    async def test_feed_and_cli_never_expose_the_bank_size(self) -> None:
        await seed_explore_questions(self.db)
        app = make_wsgi_app(self.settings)
        headers = {"X-Telegram-Init-Data": _sign_init_data(USER)}

        status, body = await asyncio.to_thread(
            _call_wsgi, app, "GET", "/api/v1/explore/feed", headers=headers
        )
        self.assertTrue(status.startswith("200"))
        payload = json.loads(body)
        self.assertEqual(set(payload), {"items"})
        self.assertEqual(len(payload["items"]), 10)
        for item in payload["items"]:
            self.assertEqual(
                set(item), {"question_id", "topic", "difficulty", "question_text", "options"}
            )
        # No count, progress, index or seed terminology anywhere in the payload.
        text = json.dumps(payload, ensure_ascii=False)
        for leak in ("200", "total", "count", "progress", "seed", "index"):
            self.assertNotIn(leak, text.lower().replace("question_text", ""))

        # The Mini App renders only what the feed returns.
        app_source = (Path(__file__).resolve().parent.parent / "miniapp/src/App.tsx").read_text()
        self.assertNotRegex(app_source, r"x\s*/\s*200|۲۰۰|of 200|total_questions")

    async def test_seeded_questions_never_enter_the_moderation_queue(self) -> None:
        await seed_explore_questions(self.db)
        self.assertEqual(
            await self._count(
                "SELECT COUNT(*) FROM explore_questions WHERE status = 'pending'"
            ),
            0,
        )

    async def test_cli_validates_the_bank(self) -> None:
        self.assertEqual(await _main(["--validate-only"]), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
