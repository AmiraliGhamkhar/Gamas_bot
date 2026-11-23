"""Tests for interactive offline HTML learning artifacts, migration 035, and delivered-only Library."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gamas_bot.artifact_validation import validate_learning_html_artifact
from gamas_bot.bot import GamasBot
from gamas_bot.db import MIGRATIONS_DIR, Database
from gamas_bot.learning_html import (
    LearningHtmlRenderError,
    render_quiz_html,
)
from tests.support import FakeCallbackEvent, FakeTelegramClient, make_settings
from tests.test_bot import menu_payloads

OWNER = 901
OTHER = 902


def _extract_inline_script(html_text: str) -> str:
    matches = re.findall(
        r"<script(?![^>]*\btype=)[^>]*>(.*?)</script>",
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if len(matches) != 1:
        raise AssertionError(f"expected 1 inline executable script, found {len(matches)}")
    return matches[0]


def _extract_embedded_json(html_text: str) -> dict:
    match = re.search(
        r'<script[^>]*type="application/json"[^>]*id="gamas-learning-data"[^>]*>(.*?)</script>',
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            r'<script[^>]*id="gamas-learning-data"[^>]*type="application/json"[^>]*>(.*?)</script>',
            html_text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    if match is None:
        raise AssertionError("embedded learning JSON script block missing")
    return json.loads(match.group(1))


class LearningHtmlRendererTests(unittest.TestCase):
    @staticmethod
    def _sample_questions() -> list[dict]:
        return [
            {
                "question": "اگر بیمار با HbA1c=6.8% مراجعه کند، کدام اقدام اولویت دارد؟ </script><img onerror=alert(1)>",
                "options": [
                    "تأیید تشخیص با آزمایش مجدد",
                    "قطع فوری همه داروها",
                    "نادیده گرفتن نتیجه آزمایش",
                    "شروع درمان بدون بررسی پایه",
                ],
                "correct_index": 0,
                "explanation": "تأیید تشخیص طبق راهنمای بالینی ضروری است.",
                "source": "صفحهٔ ۲",
                "difficulty": "hard",
            },
            {
                "question": "نقش اصلی میتوکندری در تنفس سلولی چیست؟",
                "options": [
                    "تولید ATP از طریق فسفوریلاسیون اکسیداتیو",
                    "ذخیرهٔ نشاسته در دیواره سلولی",
                    "رونویسی مستقیم ژن‌های هسته‌ای",
                    "انتقال اکسیژن در پلاسمای خون",
                ],
                "correct_index": 0,
                "explanation": "زنجیرهٔ انتقال الکترون در غشای داخلی میتوکندری ATP تولید می‌کند.",
                "source": "صفحهٔ ۵",
                "difficulty": "medium",
            },
        ]

    @staticmethod
    def _sample_cards() -> list[dict]:
        return [
            {
                "front": "تفاوت میتوز و میوز چیست؟ </script><b>test</b>",
                "back": "میتوز دو سلول دیپلوئید همسان و میوز چهار سلول هاپلوئید می‌سازد.",
                "source": "فصل ۱",
            },
            {
                "front": "آنزیم کلیدی گلیکولیز کدام است؟",
                "back": "فسفوفروکتوکیناز-۱ (PFK-1)",
                "source": "فصل ۲",
            },
        ]

    def test_quiz_html_is_offline_rtl_csp_validated_and_xss_safe(self) -> None:
        artifact = render_quiz_html(
            "آزمون «غدد» — <b>ویژه</b>",
            self._sample_questions(),
            source_title="جزوهٔ غدد",
        )
        self.assertTrue(artifact.filename.endswith(".html"))
        self.assertGreater(len(artifact.content), 100_000)  # embedded Vazirmatn fonts
        validate_learning_html_artifact(artifact.content, expected_kind="quiz")

        html_text = artifact.content.decode("utf-8")
        self.assertIn('lang="fa"', html_text)
        self.assertIn('dir="rtl"', html_text)
        self.assertIn("data:font/ttf;base64,", html_text)
        self.assertIn("Content-Security-Policy", html_text)
        self.assertNotIn("http://", html_text)
        self.assertNotIn("https://", html_text)
        # Raw closing script tag from untrusted text must never break out of JSON script block.
        self.assertNotIn("</script><img onerror=alert(1)>", html_text)
        self.assertNotIn("<b>ویژه</b>", html_text)

        payload = _extract_embedded_json(html_text)
        self.assertEqual(payload["kind"], "quiz")
        self.assertEqual(len(payload["questions"]), 2)
        self.assertEqual(payload["questions"][0]["difficulty"], "hard")
        self.assertEqual(payload["questions"][0]["source"], "صفحهٔ ۲")

        if shutil.which("node"):
            js_code = _extract_inline_script(html_text)
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=True) as fh:
                fh.write(js_code)
                fh.flush()
                proc = subprocess.run(
                    ["node", "--check", fh.name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_empty_inputs_raise_render_error(self) -> None:
        with self.assertRaises(LearningHtmlRenderError):
            render_quiz_html("خالی", [])

    def test_validator_rejects_wrong_kind_external_url_and_tampered_script(self) -> None:
        quiz = render_quiz_html("آزمون", self._sample_questions())

        tampered_url = quiz.content.replace(
            b"</body>", b'<img src="https://evil.example/tracker.png"></body>'
        )
        with self.assertRaises(ValueError):
            validate_learning_html_artifact(tampered_url, expected_kind="quiz")

        tampered_js = quiz.content.replace(b"</script>\n</body>", b"\n// tampered\n</script>\n</body>")
        with self.assertRaises(ValueError):
            validate_learning_html_artifact(tampered_js, expected_kind="quiz")


class Migration035UpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_035_backfills_delivered_flags_closes_active_quizzes_and_cascades_blobs(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "pre035.db"

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration.name >= "035_html_learning_and_delivered_library.sql":
                    break
                conn.executescript(migration.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    (migration.name, "2026-10-01T00:00:00+00:00"),
                )
            conn.execute(
                "INSERT INTO users(telegram_id, joined_at) VALUES (?, ?)",
                (OWNER, "2026-10-01T00:00:00+00:00"),
            )
            # 1) Delivered booklet (real Telegram message)
            conn.execute(
                "INSERT INTO library_items(item_id, user_id, kind, chat_id, message_id, title, created_at, content_text) "
                "VALUES (1, ?, 'booklet', ?, 101, 'جزوهٔ کامل', '2026-10-02T00:00:00+00:00', 'متن جزوه')",
                (OWNER, OWNER),
            )
            # 2) Undownloaded virtual booklet row (chat_id=0, message_id=0)
            conn.execute(
                "INSERT INTO library_items(item_id, user_id, kind, chat_id, message_id, title, created_at, content_text) "
                "VALUES (2, ?, 'booklet', 0, 0, 'جزوهٔ داخلی', '2026-10-02T00:00:00+00:00', 'متن داخلی')",
                (OWNER,),
            )
            # 3) User-uploaded document mirror row
            conn.execute(
                "INSERT INTO library_items(item_id, user_id, kind, chat_id, message_id, title, created_at, content_text) "
                "VALUES (3, ?, 'booklet', ?, 102, 'فایل کاربر', '2026-10-02T00:00:00+00:00', 'متن سند')",
                (OWNER, OWNER),
            )
            conn.execute(
                "INSERT INTO documents("
                "  user_id, file_msg_ref, title, source_type, page_count, sha256, text, "
                "  chunks_json, library_item_id, created_at"
                ") VALUES (?, ?, 'فایل کاربر', 'pdf', 1, 'abc123', 'متن سند', '[]', 3, '2026-10-02T00:00:00+00:00')",
                (OWNER, f"{OWNER}:102"),
            )
            # 4) Legacy active quiz session
            conn.execute(
                "INSERT INTO quiz_sessions(session_id, user_id, item_id, question_count, current_index, score, status, created_at, updated_at) "
                "VALUES (10, ?, 1, 5, 2, 20, 'active', '2026-10-02T00:00:00+00:00', '2026-10-02T00:00:00+00:00')",
                (OWNER,),
            )
            conn.commit()
        finally:
            conn.close()

        db = Database(db_path)
        await db.connect()
        try:
            # Delivered-only listing returns ONLY item 1 (not virtual item 2 or user upload item 3).
            visible = await db.library_list(OWNER, limit=20)
            self.assertEqual([row["item_id"] for row in visible], [1])

            # Internal source items still exist for learning generation and FK integrity.
            item2 = await db.library_item(OWNER, 2)
            item3 = await db.library_item(OWNER, 3)
            self.assertIsNotNone(item2)
            self.assertIsNotNone(item3)
            self.assertEqual(item2["delivered"], 0)
            self.assertEqual(item3["delivered"], 0)

            # Active legacy quiz session was cleanly closed to 'done'.
            session = await db.quiz_get_session(10, OWNER)
            self.assertIsNotNone(session)
            self.assertEqual(session["status"], "done")

            # Widened kind check supports quiz + flashcards with SQLite blob persistence and cascade delete.
            quiz_item_id = await db.library_record_delivered_file(
                OWNER,
                "quiz",
                title="آزمون زیست (۵ سؤال)",
                chat_id=OWNER,
                message_id=201,
                artifact_kind="quiz_html",
                filename="quiz_biology.html",
                content_type="text/html; charset=utf-8",
                content=b"<!doctype html><html dir='rtl'></html>",
            )
            blob = await db.library_file_blob(OWNER, quiz_item_id)
            self.assertIsNotNone(blob)
            self.assertEqual(blob["filename"], "quiz_biology.html")
            self.assertTrue(await db.library_delete(OWNER, quiz_item_id))
            self.assertIsNone(await db.library_file_blob(OWNER, quiz_item_id))
        finally:
            await db.close()


class CategorizedDeliveredLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "lib.db",
            temp_dir=root / "tmp",
            avalai_api_key="avalai-test-key",
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        await self.db.connect()
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        self.recorder = SimpleNamespace(files=[], file_payloads=[], replies=[], edits=[])
        self.client = FakeTelegramClient(self.recorder)
        self.bot.client = self.client
        await self.db.ensure_user(OWNER)
        await self.db.ensure_user(OTHER)

    async def asyncTearDown(self) -> None:
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    def _event(self, data: bytes, user_id: int = OWNER) -> FakeCallbackEvent:
        ev = FakeCallbackEvent(self.client, data=data, chat_id=user_id)
        ev.client = self.client
        return ev

    async def test_categorized_home_tabs_counts_blob_redownload_and_delete(self) -> None:
        # 1) Delivered booklet with blob
        booklet_id = await self.db.library_record_delivered_file(
            OWNER,
            "booklet",
            title="جزوهٔ فیزیولوژی",
            chat_id=OWNER,
            message_id=501,
            artifact_kind="pdf",
            filename="physiology.pdf",
            content_type="application/pdf",
            content=b"%PDF-1.4 sample booklet",
            content_text="متن کامل فیزیولوژی برای ساخت آزمون",
        )
        # 2) Delivered interactive quiz HTML with blob
        quiz_html = render_quiz_html(
            "آزمون فیزیولوژی",
            LearningHtmlRendererTests._sample_questions(),
            source_title="جزوهٔ فیزیولوژی",
        )
        quiz_id = await self.db.library_record_delivered_file(
            OWNER,
            "quiz",
            title="آزمون فیزیولوژی (۲ سؤال)",
            chat_id=OWNER,
            message_id=502,
            artifact_kind="quiz_html",
            filename=quiz_html.filename,
            content_type="text/html; charset=utf-8",
            content=quiz_html.content,
        )
        # 3) Historical delivered flashcards HTML (counted in "other")
        fc_id = await self.db.library_record_delivered_file(
            OWNER,
            "flashcards",
            title="فلش‌کارت فیزیولوژی (۲ کارت)",
            chat_id=OWNER,
            message_id=503,
            artifact_kind="flashcards_html",
            filename="fc.html",
            content_type="text/html; charset=utf-8",
            content=b"<html>historical flashcards</html>",
        )
        # 4) Delivered transcript ("other" tab)
        other_id = await self.db.library_record_delivered_file(
            OWNER,
            "transcript",
            title="متن خام جلسه اول",
            chat_id=OWNER,
            message_id=504,
            artifact_kind="txt",
            filename="transcript.txt",
            content_type="text/plain; charset=utf-8",
            content="متن خام جلسه".encode("utf-8"),
        )
        # 5) Internal undownloaded row (must NEVER appear in counts or category tabs!)
        await self.db.library_add(
            OWNER,
            "booklet",
            0,
            0,
            title="منبع داخلی",
            delivered=False,
            artifact_kind="learning_source",
        )

        # Home screen shows all 4 category buttons with exact count 1 each.
        home = self._event(b"library")
        await self.bot._handle_callback(home)
        home_buttons = home.edits[-1]["buttons"]
        payloads = menu_payloads(home_buttons)
        self.assertIn(b"lib:cat:booklet:0", payloads)
        self.assertIn(b"lib:cat:quiz:0", payloads)
        self.assertIn(b"lib:cat:other:0", payloads)
        counts = await self.db.library_category_counts(OWNER)
        self.assertEqual(
            counts, {"booklet": 1, "quiz": 1, "other": 2, "total": 4}
        )

        # Open Quizzes tab -> lists ONLY the quiz HTML item (1-click download + delete).
        quiz_tab = self._event(b"lib:cat:quiz:0")
        await self.bot._handle_callback(quiz_tab)
        tab_payloads = menu_payloads(quiz_tab.edits[-1]["buttons"])
        self.assertIn(f"lib:f:{quiz_id}".encode(), tab_payloads)
        self.assertIn(f"lib:del:{quiz_id}".encode(), tab_payloads)
        self.assertNotIn(f"lib:f:{booklet_id}".encode(), tab_payloads)
        self.assertNotIn(f"lib:f:{fc_id}".encode(), tab_payloads)
        self.assertNotIn(f"lib:f:{other_id}".encode(), tab_payloads)

        # 1-click re-download works straight from SQLite blob EVEN IF Telegram message was deleted!
        self.client.forward_messages = mock.AsyncMock(
            side_effect=RuntimeError("MESSAGE_ID_INVALID")
        )
        redownload = self._event(f"lib:f:{quiz_id}".encode())
        await self.bot._handle_callback(redownload)
        self.client.forward_messages.assert_not_awaited()
        self.assertTrue(
            any(str(d["path"]).endswith(".html") for d in self.client.deliveries)
        )
        self.assertEqual(self.recorder.file_payloads[-1][1], quiz_html.content)

        # Foreign user cannot access or delete OWNER's library items or blobs.
        foreign_open = self._event(f"lib:f:{quiz_id}".encode(), user_id=OTHER)
        await self.bot._handle_callback(foreign_open)
        self.assertEqual(foreign_open.answers[-1][1], True)
        self.assertIsNone(await self.db.library_file_blob(OTHER, quiz_id))
        foreign_del = self._event(f"lib:delok:{quiz_id}".encode(), user_id=OTHER)
        await self.bot._handle_callback(foreign_del)
        self.assertIsNotNone(await self.db.library_item(OWNER, quiz_id))

        # Owner deletes the quiz item via confirmation flow -> blob cascades cleanly.
        confirm = self._event(f"lib:del:{quiz_id}".encode())
        await self.bot._handle_callback(confirm)
        self.assertIn(f"lib:delok:{quiz_id}".encode(), menu_payloads(confirm.edits[-1]["buttons"]))
        delete = self._event(f"lib:delok:{quiz_id}".encode())
        await self.bot._handle_callback(delete)
        self.assertIsNone(await self.db.library_item(OWNER, quiz_id))
        self.assertIsNone(await self.db.library_file_blob(OWNER, quiz_id))
        self.assertEqual(
            await self.db.library_category_counts(OWNER),
            {"booklet": 1, "quiz": 0, "other": 2, "total": 3},
        )


if __name__ == "__main__":
    unittest.main()
