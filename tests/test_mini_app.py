"""The Mini App: initData verification, authorization, and booklet editing.

The security property under test is narrow and absolute: a request is served
only if Telegram signed it for *this* bot, recently, and then only for the user
whose id is inside that signature. Everything else is a rendering detail.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from gamas_bot.mini_app import (
    AppError,
    MAX_BOOKLET_CHARS,
    MiniAppData,
    booklets_payload,
    booklet_payload,
    create_app,
    leaderboard_payload,
    save_booklet_payload,
    verify_init_data,
)
from gamas_bot.mini_app_store import MiniAppStore

BOT_TOKEN = "123456:TEST-token-for-signatures"
NOW = 1_800_000_000.0


def sign(fields: dict[str, str], *, bot_token: str = BOT_TOKEN) -> str:
    """Build ``initData`` the way Telegram does, signature included."""
    check_string = "\n".join(f"{name}={fields[name]}" for name in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": signature})


def credentials(user_id: int = 42, *, auth_date: float | None = None) -> str:
    return sign(
        {
            "auth_date": str(int(auth_date if auth_date is not None else NOW)),
            "query_id": "AAH-test",
            "user": json.dumps(
                {"id": user_id, "first_name": "آزمون", "username": "tester"},
                ensure_ascii=False,
            ),
        }
    )


class VerifyInitDataTests(unittest.TestCase):
    def test_a_valid_signature_yields_the_user(self):
        user = verify_init_data(credentials(), bot_token=BOT_TOKEN, now=NOW)
        self.assertEqual(user.user_id, 42)
        self.assertEqual(user.first_name, "آزمون")
        self.assertEqual(user.username, "tester")

    def test_a_tampered_field_breaks_the_signature(self):
        signed = credentials()
        tampered = signed.replace("%D8%A2%D8%B2%D9%85%D9%88%D9%86", "admin")
        if tampered == signed:  # pragma: no cover - encoding drift guard
            tampered = signed.replace("tester", "root")
        with self.assertRaises(AppError) as caught:
            verify_init_data(tampered, bot_token=BOT_TOKEN, now=NOW)
        self.assertEqual(caught.exception.status, 401)

    def test_a_different_bot_token_does_not_verify(self):
        with self.assertRaises(AppError) as caught:
            verify_init_data(
                credentials(), bot_token="999:another-bot", now=NOW
            )
        self.assertEqual(caught.exception.status, 401)

    def test_an_unsigned_payload_is_refused(self):
        raw = urlencode({"auth_date": str(int(NOW)), "user": '{"id": 1}'})
        with self.assertRaises(AppError):
            verify_init_data(raw, bot_token=BOT_TOKEN, now=NOW)

    def test_an_expired_payload_is_refused(self):
        stale = credentials(auth_date=NOW - 3600)
        with self.assertRaises(AppError):
            verify_init_data(stale, bot_token=BOT_TOKEN, now=NOW, max_age_seconds=300)

    def test_a_payload_from_the_future_is_refused(self):
        with self.assertRaises(AppError):
            verify_init_data(
                credentials(auth_date=NOW + 600), bot_token=BOT_TOKEN, now=NOW
            )

    def test_empty_input_is_refused(self):
        for value in ("", "   ", "hash="):
            with self.assertRaises(AppError):
                verify_init_data(value, bot_token=BOT_TOKEN, now=NOW)

    def test_a_missing_token_is_a_server_error_not_a_client_error(self):
        with self.assertRaises(AppError) as caught:
            verify_init_data(credentials(), bot_token="", now=NOW)
        self.assertEqual(caught.exception.status, 500)

    def test_the_signature_field_is_ignored(self):
        """Newer clients add an Ed25519 ``signature`` that Telegram excludes."""
        fields = {
            "auth_date": str(int(NOW)),
            "user": json.dumps({"id": 7}),
        }
        signed = sign(fields)
        with_extra = signed + "&signature=deadbeef"
        user = verify_init_data(with_extra, bot_token=BOT_TOKEN, now=NOW)
        self.assertEqual(user.user_id, 7)


class FakeData:
    """A MiniAppData double that records writes."""

    def __init__(self) -> None:
        self.saved: list[tuple[int, int, str]] = []
        self.rows = [{"first_name": "الف", "quiz_points": 90}]
        self.items = [
            {"id": 1, "title": "جزوهٔ یک", "content_text": "متن", "editable": True}
        ]

    def as_facade(self) -> MiniAppData:
        return MiniAppData(
            leaderboard=lambda limit: self.rows[:limit],
            rank=lambda _user_id: {"rank": 4, "points": 70},
            booklets=lambda _user_id, limit: self.items[:limit],
            booklet=lambda _user_id, item_id: next(
                (item for item in self.items if item["id"] == item_id), None
            ),
            save_booklet=self._save,
        )

    def _save(self, user_id: int, item_id: int, text: str) -> bool:
        if not any(item["id"] == item_id for item in self.items):
            return False
        self.saved.append((user_id, item_id, text))
        return True


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeData()
        self.data = self.fake.as_facade()

    def test_the_leaderboard_carries_the_callers_own_place(self):
        payload = leaderboard_payload(self.data, 42)
        self.assertEqual(payload["rows"][0]["rank"], 1)
        self.assertEqual(payload["me"], {"rank": 4, "points": 70})

    def test_a_missing_booklet_is_a_404(self):
        with self.assertRaises(AppError) as caught:
            booklet_payload(self.data, 42, 999)
        self.assertEqual(caught.exception.status, 404)

    def test_an_edit_is_scoped_to_the_caller(self):
        save_booklet_payload(self.data, 42, 1, {"text": "متن تازه"})
        self.assertEqual(self.fake.saved, [(42, 1, "متن تازه")])

    def test_an_empty_edit_is_refused(self):
        for body in ({"text": ""}, {"text": "   "}, {}, {"text": 5}):
            with self.assertRaises(AppError) as caught:
                save_booklet_payload(self.data, 42, 1, body)
            self.assertEqual(caught.exception.status, 400, body)

    def test_an_oversized_edit_is_refused_before_it_is_written(self):
        with self.assertRaises(AppError) as caught:
            save_booklet_payload(
                self.data, 42, 1, {"text": "ب" * (MAX_BOOKLET_CHARS + 1)}
            )
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(self.fake.saved, [])

    def test_a_booklet_the_caller_does_not_own_is_a_404(self):
        facade = MiniAppData(
            leaderboard=lambda limit: [],
            rank=lambda _user_id: {"rank": 0, "points": 0},
            booklets=lambda _user_id, limit: [],
            booklet=lambda _user_id, _item_id: None,
            save_booklet=lambda _user_id, _item_id, _text: False,
        )
        with self.assertRaises(AppError):
            save_booklet_payload(facade, 42, 1, {"text": "x"})

    def test_booklet_listing_is_what_the_store_returned(self):
        payload = booklets_payload(self.data, 42)
        self.assertEqual(len(payload["items"]), 1)


class WsgiTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeData()
        self.app = create_app(
            data=self.fake.as_facade(),
            bot_token=BOT_TOKEN,
            now=lambda: NOW,
        )

    def request(self, path, *, method="POST", body=None, init_data=None):
        raw = b""
        if body is not None:
            payload = dict(body)
            if init_data is not None:
                payload["initData"] = init_data
            raw = json.dumps(payload).encode()
        environ = {
            "PATH_INFO": path,
            "REQUEST_METHOD": method,
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        chunks = self.app(environ, start_response)
        return captured, json.loads(b"".join(chunks) or b"{}")

    def test_health_needs_no_credentials(self):
        for method in ("GET", "HEAD"):
            status, body = self.request("/health", method=method)
            self.assertTrue(status["status"].startswith("200"))
            self.assertTrue(body["ok"])

    def test_the_leaderboard_refuses_an_anonymous_caller(self):
        status, body = self.request("/api/leaderboard", body={})
        self.assertTrue(status["status"].startswith("401"))
        self.assertEqual(body["error"], "missing Telegram credentials")

    def test_the_leaderboard_serves_a_signed_caller(self):
        status, body = self.request(
            "/api/leaderboard", body={}, init_data=credentials()
        )
        self.assertTrue(status["status"].startswith("200"))
        self.assertEqual(body["rows"][0]["name"], "الف")
        self.assertEqual(body["me"]["rank"], 4)

    def test_the_limit_is_clamped_and_validated(self):
        _status, body = self.request(
            "/api/leaderboard", body={"limit": 500}, init_data=credentials()
        )
        self.assertIn("rows", body)
        status, _body = self.request(
            "/api/leaderboard", body={"limit": "abc"}, init_data=credentials()
        )
        self.assertTrue(status["status"].startswith("400"))

    def test_the_booklet_list_requires_a_signature(self):
        status, _body = self.request("/api/booklets", body={})
        self.assertTrue(status["status"].startswith("401"))
        status, body = self.request(
            "/api/booklets", body={}, init_data=credentials()
        )
        self.assertTrue(status["status"].startswith("200"))
        self.assertEqual(body["items"][0]["title"], "جزوهٔ یک")

    def test_a_get_on_a_write_route_is_a_405(self):
        status, _body = self.request("/api/leaderboard", method="GET", body={})
        self.assertTrue(status["status"].startswith("405"))

    def test_an_edit_without_an_id_is_a_400(self):
        status, _body = self.request(
            "/api/booklet", method="PUT", body={"text": "x"}, init_data=credentials()
        )
        self.assertTrue(status["status"].startswith("400"))

    def test_an_edit_is_saved_for_the_signed_user(self):
        status, body = self.request(
            "/api/booklet",
            method="PUT",
            body={"id": 1, "text": "متن ویرایش‌شده"},
            init_data=credentials(user_id=777),
        )
        self.assertTrue(status["status"].startswith("200"))
        self.assertTrue(body["ok"])
        self.assertEqual(self.fake.saved, [(777, 1, "متن ویرایش‌شده")])

    def test_a_read_returns_the_booklet_text(self):
        status, body = self.request(
            "/api/booklet", body={"id": 1}, init_data=credentials()
        )
        self.assertTrue(status["status"].startswith("200"))
        self.assertEqual(body["text"], "متن")
        self.assertTrue(body["editable"])

    def test_an_invalid_body_is_a_400_not_a_crash(self):
        environ = {
            "PATH_INFO": "/api/booklet",
            "REQUEST_METHOD": "PUT",
            "CONTENT_LENGTH": "5",
            "wsgi.input": io.BytesIO(b"not{}"),
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status

        body = b"".join(self.app(environ, start_response))
        self.assertTrue(captured["status"].startswith("400"))
        self.assertIn("JSON", json.loads(body)["error"])

    def test_an_oversized_body_is_refused_unread(self):
        environ = {
            "PATH_INFO": "/api/booklet",
            "REQUEST_METHOD": "PUT",
            "CONTENT_LENGTH": str(5_000_000),
            "wsgi.input": io.BytesIO(b"x"),
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status

        b"".join(self.app(environ, start_response))
        self.assertTrue(captured["status"].startswith("413"))

    def test_an_unknown_path_is_a_404(self):
        status, _body = self.request("/api/nope", body={})
        self.assertTrue(status["status"].startswith("404"))

    def test_every_response_forbids_storage(self):
        status, _body = self.request("/api/leaderboard", body={}, init_data=credentials())
        self.assertEqual(status["headers"]["Cache-Control"], "no-store")

    def test_a_crashing_dependency_is_a_500_without_a_traceback(self):
        app = create_app(
            data=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            bot_token=BOT_TOKEN,
            now=lambda: NOW,
        )
        raw = json.dumps({"initData": credentials()}).encode()
        environ = {
            "PATH_INFO": "/api/booklets",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status

        body = json.loads(b"".join(app(environ, start_response)))
        self.assertTrue(captured["status"].startswith("500"))
        self.assertEqual(body["error"], "internal error")
        self.assertNotIn("boom", json.dumps(body))


class StaticBundleTests(unittest.TestCase):
    def test_the_bundle_is_served_with_a_hardened_policy(self):
        app = create_app(
            data=FakeData().as_facade(),
            bot_token=BOT_TOKEN,
            static_dir=str(Path(__file__).resolve().parent.parent / "mini_app"),
        )
        environ = {
            "PATH_INFO": "/",
            "REQUEST_METHOD": "GET",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(b""),
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        body = b"".join(app(environ, start_response))
        self.assertTrue(captured["status"].startswith("200"))
        self.assertIn(b"telegram-web-app.js", body)
        self.assertEqual(
            captured["headers"]["X-Content-Type-Options"], "nosniff"
        )
        self.assertIn("frame-ancestors", captured["headers"]["Content-Security-Policy"])

    def test_a_traversal_attempt_cannot_escape_the_bundle(self):
        app = create_app(
            data=FakeData().as_facade(),
            bot_token=BOT_TOKEN,
            static_dir=str(Path(__file__).resolve().parent.parent / "mini_app"),
        )
        environ = {
            "PATH_INFO": "/static/../../.env",
            "REQUEST_METHOD": "GET",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(b""),
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status

        b"".join(app(environ, start_response))
        self.assertTrue(captured["status"].startswith("404"))


class StoreTests(unittest.IsolatedAsyncioTestCase):
    """The SQLite store against a schema with the same shape as the bot's."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "app.db"
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                first_name TEXT DEFAULT '',
                quiz_points INTEGER NOT NULL DEFAULT 0,
                banned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE quiz_point_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE library_items (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                submission_id INTEGER,
                kind TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                unavailable INTEGER NOT NULL DEFAULT 0,
                content_text TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO users(telegram_id, first_name, quiz_points, banned) "
            "VALUES (?, ?, ?, ?)",
            [(1, "یک", 90, 0), (2, "دو", 50, 0), (3, "سه", 0, 0), (4, "چهار", 999, 1)],
        )
        connection.executemany(
            "INSERT INTO quiz_point_events(user_id, points, created_at) "
            "VALUES (?, ?, datetime('now'))",
            [(1, 90), (2, 50), (4, 999)],  # user 4 is banned
        )
        connection.executemany(
            "INSERT INTO library_items(user_id, kind, chat_id, message_id, title, "
            "created_at, content_text) VALUES (?, 'booklet', 1, 1, ?, '2026-01-01', ?)",
            [(1, "جزوهٔ الف", "متن الف"), (1, "جزوهٔ ب", ""), (2, "جزوهٔ دیگر", "x")],
        )
        connection.execute(
            "INSERT INTO library_items(user_id, kind, chat_id, message_id, title, "
            "created_at, content_text) VALUES (1, 'transcript', 1, 2, 'متن خام', "
            "'2026-01-01', 'y')"
        )
        connection.commit()
        connection.close()
        self.store = MiniAppStore(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_leaderboard_ranks_and_excludes_banned_users(self):
        rows = self.store.leaderboard(10)
        self.assertEqual([row["first_name"] for row in rows], ["یک", "دو"])
        self.assertEqual(rows[0]["quiz_points"], 90)

    def test_the_rank_counts_only_higher_active_users(self):
        self.assertEqual(self.store.rank(2)["rank"], 2)
        self.assertEqual(self.store.rank(3)["rank"], 3)
        self.assertEqual(self.store.rank(4)["rank"], 1)  # banned: 0 points
        self.assertEqual(self.store.rank(999)["points"], 0)

    def test_windowed_boards_and_ranks_ignore_banned_users(self):
        weekly = self.store.leaderboard_window(10, "weekly")
        self.assertEqual([row["first_name"] for row in weekly], ["یک", "دو"])
        self.assertEqual(self.store.rank_window(2, "weekly")["rank"], 2)
        self.assertEqual(self.store.rank_window(2, "weekly")["points"], 50)
        # A banned scorer with 999 weekly points must not push user 3 down.
        self.assertEqual(self.store.rank_window(3, "weekly")["rank"], 3)

    def test_only_editable_rows_are_listed(self):
        items = self.store.booklets(1, 10)
        self.assertEqual([item["title"] for item in items], ["جزوهٔ ب", "جزوهٔ الف"])
        self.assertEqual(items[0]["chars"], 0)

    def test_a_booklet_is_read_back(self):
        record = self.store.booklet(1, 1)
        self.assertEqual(record["content_text"], "متن الف")
        self.assertIsNone(self.store.booklet(2, 1))  # someone else's row

    def test_a_transcript_is_not_editable_as_a_booklet(self):
        self.assertIsNone(self.store.booklet(1, 4))

    def test_an_edit_is_persisted(self):
        self.assertTrue(self.store.save_booklet(1, 1, "متن تازه"))
        self.assertEqual(self.store.booklet(1, 1)["content_text"], "متن تازه")

    def test_an_edit_cannot_reach_another_users_row(self):
        self.assertFalse(self.store.save_booklet(2, 1, "دستکاری"))
        self.assertEqual(self.store.booklet(1, 1)["content_text"], "متن الف")

    def test_an_unavailable_row_is_not_editable(self):
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE library_items SET unavailable = 1 WHERE item_id = 1")
        connection.commit()
        connection.close()
        self.assertFalse(self.store.save_booklet(1, 1, "x"))
        self.assertFalse(self.store.booklet(1, 1)["editable"])

    def test_the_facade_listeners_match_the_protocol(self):
        data = MiniAppData(
            leaderboard=self.store.leaderboard,
            rank=self.store.rank,
            booklets=self.store.booklets,
            booklet=self.store.booklet,
            save_booklet=self.store.save_booklet,
        )
        self.assertEqual(leaderboard_payload(data, 1)["rows"][0]["name"], "یک")


if __name__ == "__main__":
    unittest.main()


class MiniAppEntryButtonTests(unittest.TestCase):
    """The bot must only advertise a Mini App it can actually open."""

    @staticmethod
    def build(url: str):
        from types import SimpleNamespace

        from gamas_bot.bot_learning import LearningWorkflowMixin

        holder = SimpleNamespace(settings=SimpleNamespace(mini_app_url=url))
        return LearningWorkflowMixin._mini_app_buttons(holder)

    def test_no_url_means_no_button(self):
        for url in ("", "   ", "http://insecure.example/app"):
            self.assertEqual(self.build(url), [], url)

    def test_an_https_url_becomes_a_webview_button(self):
        rows = self.build("https://example.com/miniapp")
        self.assertEqual(len(rows), 1)
        button = rows[0][0]
        self.assertEqual(button.text, "✏️ ویرایش جزوه در Gamas Studio")
        # A WebView button is what gives the page a signed initData session; a
        # plain URL button would open a browser tab the app must refuse.
        self.assertEqual(type(button.type).__name__, "InlineButtonTypeWebView")
        self.assertEqual(button.type.url, "https://example.com/miniapp")
