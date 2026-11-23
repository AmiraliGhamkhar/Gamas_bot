"""Tests for /api/v1/* REST API: auth, ownership, review, explore, and rewards."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from gamas_bot.db import Database
from gamas_bot.study_storage import save_private_asset
from gamas_bot.webapp_api import make_wsgi_app
from tests.support import make_settings
from tests.test_webapp_api import BOT_TOKEN, _call_wsgi, _sign_init_data


class ApiV1Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN,
            db_path=root / "v1.db",
            temp_dir=root / "tmp",
        )
        self.assets_dir = root / "private_assets"
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(101, "Alice")
        await self.db.ensure_user(202, "Bob")
        await self.db.db.execute(
            """
            INSERT INTO explore_questions(
                question_id, topic, difficulty, question_text, options_json,
                correct_index, explanation, status, created_at
            ) VALUES (1, 'زیست', 'easy', 'واحد سازنده حیات چیست؟',
                      '["سلول","اتم","مولکول","بافت"]', 0, 'سلول', 'approved', '2026-01-01')
            """
        )
        await self.db.db.commit()
        await self.db.close()
        self.app = make_wsgi_app(self.settings, private_assets_dir=self.assets_dir)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_auth_and_account_endpoints(self) -> None:
        s, _ = await asyncio.to_thread(_call_wsgi, self.app, "GET", "/api/v1/me")
        self.assertTrue(s.startswith("401"))

        hdr = {"X-Telegram-Init-Data": _sign_init_data(101)}
        for ep in (
            "/api/v1/me",
            "/api/v1/me/subscription",
            "/api/v1/me/credits",
            "/api/v1/me/referrals",
            "/api/v1/me/payments",
        ):
            s, _ = await asyncio.to_thread(_call_wsgi, self.app, "GET", ep, headers=hdr)
            self.assertTrue(s.startswith("200"), f"{ep} failed: {s}")

    async def test_library_ownership_and_downloads(self) -> None:
        alice = {"X-Telegram-Init-Data": _sign_init_data(101)}
        bob = {"X-Telegram-Init-Data": _sign_init_data(202)}

        db = Database(self.settings.db_path)
        await db.connect()
        pack_id = await db.study_packs.study_pack_create(101, "فصل ۱")
        rel, size, sha = save_private_asset(self.assets_dir, 101, pack_id, "a.txt", b"ok")
        asset_id = await db.study_packs.study_asset_add(
            pack_id, 101, kind="transcript", title="متن",
            storage_path=rel, mime_type="text/plain", byte_size=size, sha256=sha,
        )
        await db.close()

        s, _ = await asyncio.to_thread(_call_wsgi, self.app, "GET", f"/api/v1/library/{pack_id}", headers=bob)
        self.assertTrue(s.startswith("404"))
        s, _ = await asyncio.to_thread(
            _call_wsgi, self.app, "GET", f"/api/v1/library/{pack_id}/download/{asset_id}", headers=bob
        )
        self.assertTrue(s.startswith("404"))
        s, body = await asyncio.to_thread(
            _call_wsgi, self.app, "GET", f"/api/v1/library/{pack_id}/download/{asset_id}", headers=alice
        )
        self.assertTrue(s.startswith("200"))
        self.assertEqual(body, b"ok")

    async def test_explore_duplicate_answer_and_rewards_redeem(self) -> None:
        alice = {"X-Telegram-Init-Data": _sign_init_data(101)}

        # Invalid question ID
        s, _ = await asyncio.to_thread(
            _call_wsgi, self.app, "POST", "/api/v1/explore/answers",
            headers=alice, body=json.dumps({"question_id": 999, "chosen_index": 0}).encode(),
        )
        self.assertTrue(s.startswith("404"))

        # First correct answer awards 10 points
        s, body = await asyncio.to_thread(
            _call_wsgi, self.app, "POST", "/api/v1/explore/answers",
            headers=alice, body=json.dumps({"question_id": 1, "chosen_index": 0, "dwell_ms": 4000}).encode(),
        )
        self.assertTrue(s.startswith("200"))
        self.assertEqual(json.loads(body)["points_awarded"], 10)

        # Duplicate correct answer awards 0 points
        s, body = await asyncio.to_thread(
            _call_wsgi, self.app, "POST", "/api/v1/explore/answers",
            headers=alice, body=json.dumps({"question_id": 1, "chosen_index": 0, "dwell_ms": 2000}).encode(),
        )
        self.assertTrue(s.startswith("200"))
        self.assertEqual(json.loads(body)["points_awarded"], 0)

        # Insufficient points / invalid reward code
        s, _ = await asyncio.to_thread(
            _call_wsgi, self.app, "POST", "/api/v1/rewards/redeem",
            headers=alice, body=json.dumps({"reward_code": "credit_5m"}).encode(),
        )
        self.assertTrue(s.startswith("400"))
