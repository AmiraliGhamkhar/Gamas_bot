"""Regression tests for the academic-source Explore feed redesign.

Covers the four things the redesign changed in the backend contract:

* the source catalog itself — real, safe, tiered, and complete;
* source metadata on every seeded question, and the migration that adds it;
* cursor pagination — stable, non-repeating, prefetch-safe, bounded;
* the recommender's new explainability and academic-quality weighting.

Nothing here needs PostgreSQL, Redis or a network: the whole suite runs against
the same SQLite database the production Mini App uses.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from gamas_bot.db import Database
from gamas_bot.explore_bank import BANK
from gamas_bot.explore_recommender import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    FeedCursor,
    recommend_explore_feed,
    recommend_explore_page,
)
from gamas_bot.explore_seed import seed_explore_questions
from gamas_bot.explore_sources import (
    EVIDENCE_LEVELS,
    SOURCES,
    is_safe_source_url,
    row_source_payload,
    validate_catalog,
    validate_source,
)
from gamas_bot.webapp_api import make_wsgi_app
from tests.support import make_settings
from tests.test_webapp_api import BOT_TOKEN, _call_wsgi, _sign_init_data

USER = 777


def _source_row(**overrides: str) -> dict:
    row = {
        "source_type": "textbook",
        "source_title": "Biology 2e",
        "source_author": "OpenStax",
        "source_institution": "Rice University",
        "source_url": "https://openstax.org/details/books/biology-2e",
        "source_year": "2018",
        "source_page_or_section": "Chapters on cells",
        "evidence_level": "textbook",
    }
    row.update(overrides)
    return row


class SourceCatalogTests(unittest.TestCase):
    """The catalog is the only place a citation may come from."""

    def test_catalog_is_valid(self) -> None:
        self.assertEqual(validate_catalog(), [])

    def test_every_source_is_https_and_tiered(self) -> None:
        for source in SOURCES:
            self.assertTrue(is_safe_source_url(source.url), source.key)
            self.assertIn(source.evidence_level, EVIDENCE_LEVELS, source.key)
            self.assertTrue(source.institution.strip(), source.key)

    def test_unsafe_urls_are_rejected(self) -> None:
        """The link guard is about safety; source *quality* is the catalog's job."""
        for banned in (
            "http://example.com/post",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "https://user:pass@example.com/x",
            "//example.com/x",
            "https://",
            "",
        ):
            self.assertFalse(is_safe_source_url(banned), banned)
        for allowed in ("https://www.who.int/news-room/fact-sheets/detail/anaemia",):
            self.assertTrue(is_safe_source_url(allowed), allowed)

    def test_validate_source_reports_each_problem(self) -> None:
        broken = type(SOURCES[0])(
            key="x", source_type="", title="", author="", institution="",
            url="http://nope", year="", section="", evidence_level="rumour",
        )
        problems = " ".join(validate_source(broken))
        self.assertIn("title", problems)
        self.assertIn("institution", problems)
        self.assertIn("https", problems)
        self.assertIn("evidence level", problems)

    def test_every_bank_question_has_a_real_source(self) -> None:
        for item in BANK:
            self.assertIsNotNone(item.source, item.key)
            self.assertEqual(validate_source(item.source), [], item.key)

    def test_no_medical_question_relies_on_a_weak_source(self) -> None:
        """Health questions must be backed by a Tier 1/2 source, never a blog."""
        for item in BANK:
            if item.topic not in {"سلامت و پزشکی", "بدن انسان"}:
                continue
            self.assertIn(
                item.source.evidence_level,
                {"textbook", "institutional"},
                f"{item.key} uses {item.source.key}",
            )

    def test_row_payload_refuses_incomplete_sources(self) -> None:
        self.assertIsNotNone(row_source_payload(_source_row()))
        self.assertIsNone(row_source_payload(_source_row(source_title="")))
        self.assertIsNone(row_source_payload(_source_row(source_institution="")))
        self.assertIsNone(row_source_payload(_source_row(source_url="http://x.test")))
        self.assertIsNone(row_source_payload(_source_row(evidence_level="rumour")))
        self.assertIsNone(row_source_payload({}))

    def test_catalog_has_no_wikipedia_primary_source(self) -> None:
        for source in SOURCES:
            self.assertNotIn("wikipedia.org", source.url, source.key)


class CursorTests(unittest.TestCase):
    def test_cursor_round_trips(self) -> None:
        token = FeedCursor(served=[3, 7, 11], topics=["تاریخ"]).encode()
        decoded = FeedCursor.decode(token)
        self.assertEqual(decoded.served, [3, 7, 11])
        self.assertEqual(decoded.topics, ["تاریخ"])

    def test_malformed_cursor_degrades_to_a_fresh_session(self) -> None:
        for junk in ("", "!!!not-base64!!!", "eyJ2IjoyfQ", "null", "W10"):
            decoded = FeedCursor.decode(junk)
            self.assertEqual(decoded.served, [])
            self.assertEqual(decoded.topics, [])

    def test_cursor_accepts_non_integer_entries(self) -> None:
        decoded = FeedCursor.decode(FeedCursor(served=[1, "x", None], topics=[]).encode())
        self.assertEqual(decoded.served, [1])


class ExploreFeedDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN,
            db_path=root / "gamas.db",
            temp_dir=root / "tmp",
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.app = make_wsgi_app(self.settings, private_assets_dir=root / "assets")
        await self.db.ensure_user(USER, "Feeder")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    def _headers(self) -> dict[str, str]:
        return {"X-Telegram-Init-Data": _sign_init_data(USER)}

    async def _get(self, url: str) -> dict:
        status, body = await asyncio.to_thread(
            _call_wsgi, self.app, "GET", url, headers=self._headers()
        )
        self.assertTrue(status.startswith("200"), status)
        return json.loads(body)

    # ----------------------------------------------------------- migration
    async def test_migration_added_the_source_columns(self) -> None:
        rows = await self.db.db.execute_fetchall("PRAGMA table_info(explore_questions)")
        columns = {str(row["name"]) for row in rows}
        for column in (
            "source_type",
            "source_title",
            "source_author",
            "source_institution",
            "source_url",
            "source_year",
            "source_page_or_section",
            "evidence_level",
            "verified_at",
            "updated_at",
        ):
            self.assertIn(column, columns)

    async def test_migration_preserves_pre_existing_rows(self) -> None:
        """A question written before 044 keeps its data and gains NULL sources."""
        await self.db.db.execute(
            "INSERT INTO explore_questions(topic, question_text, options_json, "
            "correct_index, explanation, status, created_at) "
            "VALUES ('تاریخ', 'سؤال قدیمی بی‌منبع است؟', '[\"الف\",\"ب\",\"ج\",\"د\"]', "
            "0, 'شرح', 'approved', '2026-01-01')"
        )
        await self.db.db.commit()
        row = await self.db.db.execute_fetchall(
            "SELECT * FROM explore_questions WHERE question_text = ?",
            ("سؤال قدیمی بی‌منبع است؟",),
        )
        self.assertEqual(len(row), 1)
        self.assertIsNone(row[0]["source_title"])
        self.assertEqual(row[0]["explanation"], "شرح")

    # -------------------------------------------------------------- seeding
    async def test_seeding_writes_verified_sources(self) -> None:
        report = await seed_explore_questions(self.db)
        self.assertEqual(report["inserted"], 200)
        rows = await self.db.db.execute_fetchall(
            "SELECT * FROM explore_questions WHERE status = 'approved'"
        )
        self.assertEqual(len(rows), 200)
        for row in rows:
            payload = row_source_payload(dict(row))
            self.assertIsNotNone(payload, row["question_text"])
            self.assertTrue(payload["source_url"].startswith("https://"))
            self.assertTrue(str(row["verified_at"]))
            self.assertIn(payload["evidence_level"], EVIDENCE_LEVELS)

    async def test_reseeding_backfills_without_duplicating_or_overwriting(self) -> None:
        await seed_explore_questions(self.db)
        # Simulate a pre-044 row: same seed_key, sources stripped.
        await self.db.db.execute(
            "UPDATE explore_questions SET source_title = NULL, source_url = NULL, "
            "evidence_level = NULL, source_institution = NULL "
            "WHERE seed_key = 'bio-01'"
        )
        # A curator's own correction on another row must survive.
        await self.db.db.execute(
            "UPDATE explore_questions SET source_page_or_section = 'p. 42' "
            "WHERE seed_key = 'bio-02'"
        )
        await self.db.db.commit()

        report = await seed_explore_questions(self.db)

        self.assertEqual(report["inserted"], 0)
        self.assertEqual(report["sources_backfilled"], 1)
        rows = await self.db.db.execute_fetchall(
            "SELECT seed_key, source_title, source_page_or_section "
            "FROM explore_questions WHERE seed_key IN ('bio-01', 'bio-02') "
            "ORDER BY seed_key"
        )
        self.assertEqual(rows[0]["source_title"], "Biology 2e")
        self.assertEqual(rows[1]["source_page_or_section"], "p. 42")
        approved = await self.db.db.execute_fetchall(
            "SELECT COUNT(*) AS n FROM explore_questions WHERE status = 'approved'"
        )
        self.assertEqual(int(approved[0]["n"]), 200)

    # ----------------------------------------------------------- pagination
    async def test_pages_never_repeat_a_question_within_a_session(self) -> None:
        await seed_explore_questions(self.db)
        seen: list[int] = []
        cursor = ""
        for _ in range(20):
            page = await recommend_explore_page(self.db, USER, limit=DEFAULT_PAGE_SIZE, cursor=cursor)
            if not page.items:
                break
            seen.extend(item["question_id"] for item in page.items)
            self.assertLessEqual(len(page.items), DEFAULT_PAGE_SIZE)
            cursor = page.next_cursor
            if not page.has_more:
                break
        self.assertEqual(len(seen), len(set(seen)), "a question was served twice")
        self.assertEqual(len(seen), 200, "the whole unseen bank should be reachable")

    async def test_page_size_is_clamped(self) -> None:
        await seed_explore_questions(self.db)
        big = await recommend_explore_page(self.db, USER, limit=500)
        self.assertLessEqual(len(big.items), MAX_PAGE_SIZE)
        small = await recommend_explore_page(self.db, USER, limit=0)
        self.assertEqual(len(small.items), 1)

    async def test_second_page_can_be_prefetched_while_first_is_unread(self) -> None:
        """The cursor carries served ids, so prefetch cannot duplicate a card.

        No impression is recorded between the two calls — exactly the situation
        of a prefetch that lands while the reader is still on card 2.
        """
        await seed_explore_questions(self.db)
        first = await recommend_explore_page(self.db, USER, limit=5)
        second = await recommend_explore_page(self.db, USER, limit=5, cursor=first.next_cursor)

        first_ids = {item["question_id"] for item in first.items}
        second_ids = {item["question_id"] for item in second.items}
        self.assertEqual(len(first_ids), 5)
        self.assertFalse(first_ids & second_ids)

    async def test_cursor_forgery_cannot_leak_seen_questions(self) -> None:
        await seed_explore_questions(self.db)
        first = await recommend_explore_page(self.db, USER, limit=3)
        for item in first.items:
            await self.db.db.execute(
                "INSERT INTO explore_impressions(user_id, question_id, shown_at, dwell_ms, signal)"
                " VALUES (?, ?, '2026-11-01T00:00:00+00:00', 1000, 1)",
                (USER, item["question_id"]),
            )
        await self.db.db.commit()
        # A garbage cursor must still be filtered by the SQL seen-clause.
        page = await recommend_explore_page(self.db, USER, limit=10, cursor="garbage")
        self.assertFalse({i["question_id"] for i in page.items} & {i["question_id"] for i in first.items})

    async def test_feed_endpoint_returns_the_pagination_envelope(self) -> None:
        await seed_explore_questions(self.db)
        page = await self._get("/api/v1/explore/feed")
        self.assertEqual(set(page), {"items", "next_cursor", "has_more"})
        self.assertEqual(len(page["items"]), DEFAULT_PAGE_SIZE)
        self.assertTrue(page["has_more"])
        self.assertTrue(page["next_cursor"])

        following = await self._get(
            f"/api/v1/explore/feed?cursor={page['next_cursor']}&limit=5"
        )
        self.assertEqual(len(following["items"]), 5)
        self.assertFalse(
            {i["question_id"] for i in page["items"]}
            & {i["question_id"] for i in following["items"]}
        )

    async def test_every_feed_item_carries_its_source_and_explanation_of_rank(self) -> None:
        await seed_explore_questions(self.db)
        page = await self._get("/api/v1/explore/feed?limit=12")
        for item in page["items"]:
            self.assertTrue(item["verified"])
            source = item["source"]
            self.assertEqual(source["verified"], "true")
            self.assertTrue(source["source_url"].startswith("https://"))
            self.assertTrue(source["source_title"])
            self.assertTrue(source["source_institution"])
            self.assertIn(source["evidence_level"], EVIDENCE_LEVELS)
            self.assertEqual(
                set(item["explain"]),
                {"affinity", "quality", "freshness", "discovery", "score"},
            )
            # The answer must never ship in the feed payload.
            self.assertNotIn("correct_index", item)
            self.assertNotIn("explanation", item)

    async def test_unverified_questions_are_marked_not_dressed_as_verified(self) -> None:
        await self.db.db.execute(
            "INSERT INTO explore_questions(topic, question_text, options_json, "
            "correct_index, explanation, status, created_at) "
            "VALUES ('تاریخ', 'سؤال بدون منبع چیست؟', '[\"الف\",\"ب\",\"ج\",\"د\"]', "
            "0, 'شرح', 'approved', '2026-11-20')"
        )
        await self.db.db.commit()
        page = await self._get("/api/v1/explore/feed?limit=1")
        item = page["items"][0]
        self.assertFalse(item["verified"])
        self.assertIsNone(item["source"])

    async def test_answering_returns_the_citation(self) -> None:
        await seed_explore_questions(self.db)
        page = await self._get("/api/v1/explore/feed?limit=1")
        qid = page["items"][0]["question_id"]
        status, body = await asyncio.to_thread(
            _call_wsgi,
            self.app,
            "POST",
            "/api/v1/explore/answers",
            headers=self._headers(),
            body=json.dumps({"question_id": qid, "chosen_index": 0, "dwell_ms": 2000}).encode(),
        )
        self.assertTrue(status.startswith("200"))
        payload = json.loads(body)
        self.assertTrue(payload["verified"])
        self.assertTrue(payload["source"]["source_url"].startswith("https://"))

    # ---------------------------------------------------------- engagement
    async def test_source_opened_is_recorded_as_a_read_signal(self) -> None:
        await seed_explore_questions(self.db)
        page = await self._get("/api/v1/explore/feed?limit=1")
        qid = page["items"][0]["question_id"]
        status, _ = await asyncio.to_thread(
            _call_wsgi,
            self.app,
            "POST",
            "/api/v1/explore/engagement",
            headers=self._headers(),
            body=json.dumps(
                {"question_id": qid, "dwell_ms": 500, "source_opened": True}
            ).encode(),
        )
        self.assertTrue(status.startswith("200"))
        rows = await self.db.db.execute_fetchall(
            "SELECT signal FROM explore_impressions WHERE user_id = ? AND question_id = ?",
            (USER, qid),
        )
        self.assertEqual(rows[0]["signal"], 2)

    async def test_engagement_still_rejects_invalid_payloads(self) -> None:
        for body in (
            {"question_id": True, "dwell_ms": 10},
            {"question_id": 1, "dwell_ms": 1.5},
            {"question_id": 1, "dwell_ms": -1},
        ):
            status, _ = await asyncio.to_thread(
                _call_wsgi,
                self.app,
                "POST",
                "/api/v1/explore/engagement",
                headers=self._headers(),
                body=json.dumps(body).encode(),
            )
            self.assertTrue(status.startswith("400"), body)

    # -------------------------------------------------------- recommenders
    async def test_diversity_holds_across_a_page_boundary(self) -> None:
        await seed_explore_questions(self.db)
        first = await recommend_explore_page(self.db, USER, limit=6)
        second = await recommend_explore_page(self.db, USER, limit=6, cursor=first.next_cursor)
        topics = [item["topic"] for item in first.items + second.items]
        for index in range(2, len(topics)):
            self.assertFalse(
                topics[index] == topics[index - 1] == topics[index - 2],
                f"three {topics[index]} cards in a row",
            )

    async def test_personalisation_does_not_eliminate_discovery(self) -> None:
        await seed_explore_questions(self.db)
        # Twenty strong signals for one topic.
        for _ in range(20):
            await self.db.db.execute(
                "INSERT INTO explore_impressions(user_id, question_id, shown_at, dwell_ms, signal)"
                " VALUES (?, 1, '2026-11-26T00:00:00+00:00', 12000, 3)",
                (USER,),
            )
        await self.db.db.commit()
        page = await recommend_explore_page(self.db, USER, limit=12)
        topics = {item["topic"] for item in page.items}
        self.assertGreater(len(topics), 1, "the feed collapsed into one subject")
        self.assertIn("زیست‌شناسی", topics)
        self.assertTrue(topics - {"زیست‌شناسی"}, "no discovery topics were served")

    async def test_ranking_is_deterministic_for_the_same_state(self) -> None:
        await seed_explore_questions(self.db)
        first = await recommend_explore_page(self.db, USER, limit=10)
        second = await recommend_explore_page(self.db, USER, limit=10)
        self.assertEqual(
            [i["question_id"] for i in first.items],
            [i["question_id"] for i in second.items],
        )

    async def test_verified_sources_outrank_unverified_ones(self) -> None:
        """Two identical questions, one sourced: the sourced one wins."""
        for sourced in (False, True):
            if sourced:
                await self.db.db.execute(
                    "INSERT INTO explore_questions(topic, difficulty, question_text, "
                    "options_json, correct_index, explanation, status, created_at, "
                    "source_type, source_title, source_author, source_institution, "
                    "source_url, evidence_level) "
                    "VALUES ('تاریخ', 'easy', 'سؤال منبع‌دار کدام است؟', "
                    "'[\"الف\",\"ب\",\"ج\",\"د\"]', 0, 'شرح', 'approved', "
                    "'2026-11-25', 'textbook', 'A History', 'Author', 'Oxford', "
                    "'https://global.oup.com/', 'textbook')"
                )
            else:
                await self.db.db.execute(
                    "INSERT INTO explore_questions(topic, difficulty, question_text, "
                    "options_json, correct_index, explanation, status, created_at) "
                    "VALUES ('تاریخ', 'easy', 'سؤال بی‌منبع کدام است؟', "
                    "'[\"الف\",\"ب\",\"ج\",\"د\"]', 0, 'شرح', 'approved', '2026-11-25')"
                )
        await self.db.db.commit()
        page = await recommend_explore_page(self.db, USER, limit=2)
        self.assertEqual([item["verified"] for item in page.items], [True, False])
        self.assertGreater(page.items[0]["explain"]["quality"], page.items[1]["explain"]["quality"])

    async def test_legacy_list_entry_point_still_works(self) -> None:
        await seed_explore_questions(self.db)
        feed = await recommend_explore_feed(self.db, USER, limit=4)
        self.assertEqual(len(feed), 4)
        # The old integer cursor (a question_id floor) is still understood.
        floor = max(item["question_id"] for item in feed)
        rest = await recommend_explore_feed(self.db, USER, limit=4, cursor=floor)
        self.assertFalse(
            {i["question_id"] for i in feed} & {i["question_id"] for i in rest}
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
