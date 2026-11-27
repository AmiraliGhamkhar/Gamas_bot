"""Tests for Explore feed, deterministic recommender, seed bank, and rewards."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from gamas_bot.db import Database
from gamas_bot.explore_recommender import (
    classify_engagement,
    compute_topic_affinities,
)
from gamas_bot.explore_seed import seed_explore_questions
from gamas_bot.webapp_api import make_wsgi_app
from tests.support import make_settings
from tests.test_webapp_api import BOT_TOKEN, _call_wsgi, _sign_init_data


class ExploreAndRewardsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN,
            db_path=root / "explore.db",
            temp_dir=root / "tmp",
        )
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(101, "Alice")
        await seed_explore_questions(self.db)
        await self.db.close()
        self.app = make_wsgi_app(self.settings, private_assets_dir=root / "assets")

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def test_engagement_classification_exact_weights(self) -> None:
        self.assertEqual(classify_engagement(answered=True, dwell_ms=1000), 3)
        self.assertEqual(classify_engagement(answered=False, dwell_ms=9000), 2)
        self.assertEqual(classify_engagement(answered=False, dwell_ms=1500), 1)
        self.assertEqual(classify_engagement(answered=False, dwell_ms=5000), 0)

    async def test_feed_excludes_counts_indices_and_unapproved(self) -> None:
        alice = {"X-Telegram-Init-Data": _sign_init_data(101)}
        s, body = await asyncio.to_thread(
            _call_wsgi, self.app, "GET", "/api/v1/explore/feed", headers=alice
        )
        self.assertTrue(s.startswith("200"))
        data = json.loads(body)
        self.assertEqual(len(data["items"]), 10)
        self.assertNotIn("total", data)
        self.assertNotIn("index", data["items"][0])

    async def test_dwell_skip_and_wrong_answer_grant_zero_points(self) -> None:
        alice = {"X-Telegram-Init-Data": _sign_init_data(101)}
        await asyncio.to_thread(
            _call_wsgi, self.app, "POST", "/api/v1/explore/engagement",
            headers=alice, body=json.dumps({"question_id": 1, "dwell_ms": 10000}).encode(),
        )
        await asyncio.to_thread(
            _call_wsgi, self.app, "POST", "/api/v1/explore/answers",
            headers=alice, body=json.dumps({"question_id": 2, "chosen_index": 3, "dwell_ms": 4000}).encode(),
        )
        s, body = await asyncio.to_thread(
            _call_wsgi, self.app, "GET", "/api/v1/rewards/balance", headers=alice
        )
        self.assertTrue(s.startswith("200"))
        self.assertEqual(json.loads(body)["balance"], 0)

    async def test_malformed_explore_payloads_are_400_not_500(self) -> None:
        alice = {"X-Telegram-Init-Data": _sign_init_data(101)}
        for path, payload in (
            ("/api/v1/explore/engagement", {"question_id": 1, "dwell_ms": "abc"}),
            ("/api/v1/explore/engagement", {"question_id": True, "dwell_ms": 10}),
            ("/api/v1/explore/engagement", {"question_id": 1, "dwell_ms": 10.5}),
            ("/api/v1/explore/answers", {"question_id": 1, "chosen_index": 0, "dwell_ms": "abc"}),
            ("/api/v1/explore/answers", {"question_id": True, "chosen_index": 0}),
            ("/api/v1/explore/answers", {"question_id": 1, "chosen_index": True}),
        ):
            s, _ = await asyncio.to_thread(
                _call_wsgi,
                self.app,
                "POST",
                path,
                headers=alice,
                body=json.dumps(payload).encode(),
            )
            self.assertTrue(s.startswith("400"), (path, payload, s))

    def test_topic_affinity_smoothing_and_determinism(self) -> None:
        from datetime import datetime, timezone

        now = datetime(2026, 11, 23, tzinfo=timezone.utc)
        imps = [
            {"topic": "فیزیک", "signal": 3, "shown_at": now.isoformat()},
            {"topic": "تاریخ", "signal": 1, "shown_at": now.isoformat()},
            {"topic": "شیمی", "signal": 0, "shown_at": now.isoformat()},
        ]
        a1 = compute_topic_affinities(imps, now=now)
        a2 = compute_topic_affinities(imps, now=now)
        self.assertEqual(a1, a2)
        self.assertGreater(a1["فیزیک"], a1["تاریخ"])
        self.assertNotIn("شیمی", a1)
