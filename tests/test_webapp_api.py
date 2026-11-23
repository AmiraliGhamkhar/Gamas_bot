"""Tests for Phase 2: Study Packs schema, initData HMAC auth, ownership, and WSGI API."""

from __future__ import annotations

import hashlib
import hmac
import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from gamas_bot.db import Database
from gamas_bot.study_storage import resolve_private_asset, save_private_asset
from gamas_bot.telegram_auth import InitDataError, validate_init_data
from gamas_bot.webapp_api import make_wsgi_app
from tests.support import make_settings

BOT_TOKEN = "123456:TEST_BOT_TOKEN_FOR_HMAC"


def _sign_init_data(user_id: int, *, auth_date: int | None = None, token: str = BOT_TOKEN) -> str:
    ts = int(time.time()) if auth_date is None else auth_date
    user_json = json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":"))
    fields = {"auth_date": str(ts), "user": user_json}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    fields["hash"] = sig
    return urlencode(fields)


def _call_wsgi(app, method: str, path: str, *, headers: dict[str, str] | None = None, body: bytes = b""):
    status_holder: list[str] = []

    def start_response(status, _headers, _exc=None):
        status_holder.append(status)

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    for k, v in (headers or {}).items():
        environ[f"HTTP_{k.upper().replace('-', '_')}"] = v
    chunks = b"".join(app(environ, start_response))
    return status_holder[0], chunks


class TelegramInitDataTests(unittest.TestCase):
    def test_valid_init_data_returns_user_id(self) -> None:
        raw = _sign_init_data(777)
        res = validate_init_data(raw, BOT_TOKEN)
        self.assertEqual(res["user_id"], 777)

    def test_tampered_signature_is_rejected(self) -> None:
        raw = _sign_init_data(777) + "x"
        with self.assertRaises(InitDataError):
            validate_init_data(raw, BOT_TOKEN)

    def test_expired_init_data_is_rejected(self) -> None:
        raw = _sign_init_data(777, auth_date=1000)
        with self.assertRaises(InitDataError):
            validate_init_data(raw, BOT_TOKEN, max_age_seconds=60)


class StudyStorageAndApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN,
            db_path=root / "phase2.db",
            temp_dir=root / "tmp",
        )
        self.assets_dir = root / "private_assets"
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        await self.db.ensure_user(101, "Alice")
        await self.db.ensure_user(202, "Bob")
        await self.db.close()
        self.app = make_wsgi_app(
            self.settings,
            private_assets_dir=self.assets_dir,
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_private_storage_rejects_traversal(self) -> None:
        rel, size, sha = save_private_asset(
            self.assets_dir, 101, 1, "../evil.txt", b"hello"
        )
        self.assertEqual(size, 5)
        self.assertEqual(len(sha), 64)
        resolved = resolve_private_asset(self.assets_dir, rel)
        self.assertEqual(resolved.read_bytes(), b"hello")
        with self.assertRaises(ValueError):
            resolve_private_asset(self.assets_dir, "../../etc/passwd")

    async def test_api_requires_valid_init_data_and_enforces_ownership(self) -> None:
        status, _ = await asyncio.to_thread(_call_wsgi, self.app, "GET", "/api/me")
        self.assertTrue(status.startswith("401"))

        alice_hdr = {"X-Telegram-Init-Data": _sign_init_data(101)}
        bob_hdr = {"X-Telegram-Init-Data": _sign_init_data(202)}

        status, body = await asyncio.to_thread(_call_wsgi, self.app, "GET", "/api/me", headers=alice_hdr)
        self.assertTrue(status.startswith("200"))
        me = json.loads(body)
        self.assertEqual(me["user_id"], 101)
        self.assertIn("credit_seconds", me)
        self.assertIn("reward_points", me)

        # Create pack as Alice
        status, body = await asyncio.to_thread(_call_wsgi, 
            self.app,
            "POST",
            "/api/packs",
            headers=alice_hdr,
            body=json.dumps({"title": "فصل اول"}).encode(),
        )
        self.assertTrue(status.startswith("201"))
        pack_id = json.loads(body)["pack"]["pack_id"]

        # Bob cannot read or delete Alice's pack
        status, _ = await asyncio.to_thread(_call_wsgi, self.app, "GET", f"/api/packs/{pack_id}", headers=bob_hdr)
        self.assertTrue(status.startswith("404"))
        status, _ = await asyncio.to_thread(_call_wsgi, self.app, "DELETE", f"/api/packs/{pack_id}", headers=bob_hdr)
        self.assertTrue(status.startswith("404"))

        # Attach a private asset to Alice's pack and download it
        rel, size, sha = save_private_asset(
            self.assets_dir, 101, pack_id, "notes.txt", b"secret notes"
        )
        db = Database(self.settings.db_path)
        await db.connect()
        asset_id = await db.study_packs.study_asset_add(
            pack_id,
            101,
            kind="transcript",
            title="متن",
            storage_path=rel,
            mime_type="text/plain",
            byte_size=size,
            sha256=sha,
        )
        await db.close()

        status, dl = await asyncio.to_thread(_call_wsgi, 
            self.app, "GET", f"/api/assets/{asset_id}/download", headers=alice_hdr
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(dl, b"secret notes")

        # Bob cannot download Alice's asset
        status, _ = await asyncio.to_thread(_call_wsgi, 
            self.app, "GET", f"/api/assets/{asset_id}/download", headers=bob_hdr
        )
        self.assertTrue(status.startswith("404"))
