"""Tests for the capacity-improvement changes (batches 1 and 2).

Covered: the shared-connection DB broker (reuse, staleness, serialisation),
the ``from_env`` settings cache, gzip static serving, ``/api/v1/healthz``,
library pagination, delivered-row ``content_text`` dedupe, the CPU-build
offload gate, the daily jobs dashboard, and the RAM-based job sizing helpers.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from gamas_bot import cpu_pool
from gamas_bot.config import recommended_max_active_jobs
from gamas_bot.db import Database
from gamas_bot.db_broker import DatabaseBroker, db_reuse_enabled, reset_broker
from gamas_bot.jobs_report import fetch_daily_job_report, summarize_daily
from gamas_bot.webapp_api import make_wsgi_app
from tests.support import make_settings
from tests.test_webapp_api import BOT_TOKEN, _sign_init_data


def _call_full(
    app, method: str, path: str, *, headers: dict[str, str] | None = None, body: bytes = b""
):
    """Like ``tests.test_webapp_api._call_wsgi`` but also returns the headers."""
    status_holder: list[str] = []
    header_holder: list[list[tuple[str, str]]] = []

    def start_response(status, response_headers, _exc=None):
        status_holder.append(status)
        header_holder.append(list(response_headers))

    path_only, _, query = path.partition("?")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path_only,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    for key, value in (headers or {}).items():
        environ[f"HTTP_{key.upper().replace('-', '_')}"] = value
    chunks = b"".join(app(environ, start_response))
    return status_holder[0], dict(header_holder[0]), chunks


def _hdrs(**extra: str) -> dict[str, str]:
    return {"X-Telegram-Init-Data": _sign_init_data(101), **extra}


class DatabaseBrokerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_broker()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "broker.db"

    def tearDown(self) -> None:
        reset_broker()
        self.tmp.cleanup()

    async def test_connection_is_reused_across_calls(self) -> None:
        broker = DatabaseBroker()
        self.addCleanup(broker.close)
        seen: list[Database] = []

        async def fn(db: Database) -> None:
            await db.ensure_user(1, "a")
            seen.append(db)

        await broker.run(self.db_path, fn)
        await broker.run(self.db_path, fn)
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], seen[1])

    async def test_replaced_database_file_reopens_the_connection(self) -> None:
        broker = DatabaseBroker()
        self.addCleanup(broker.close)
        seen: list[Database] = []

        async def fn(db: Database) -> None:
            await db.ensure_user(1, "a")
            seen.append(db)

        await broker.run(self.db_path, fn)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()
        await broker.run(self.db_path, fn)
        self.assertEqual(len(seen), 2)
        self.assertIsNot(seen[0], seen[1])

    async def test_concurrent_requests_serialize_without_error(self) -> None:
        broker = DatabaseBroker()
        self.addCleanup(broker.close)

        async def fn(db: Database) -> int:
            await db.ensure_user(7, "x")
            ident = await db.user_identity(7)
            return int(ident["telegram_id"])

        results = await asyncio.gather(*(broker.run(self.db_path, fn) for _ in range(20)))
        self.assertEqual(results, [7] * 20)

    async def test_kill_switch_disables_reuse(self) -> None:
        with mock.patch.dict(os.environ, {"GAMAS_DB_REUSE": "0"}):
            self.assertFalse(db_reuse_enabled())
        with mock.patch.dict(os.environ, {"GAMAS_DB_REUSE": "1"}):
            self.assertTrue(db_reuse_enabled())


class WsgiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Broker + settings cache behaviour through the real WSGI app."""

    def setUp(self) -> None:
        reset_broker()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN,
            db_path=root / "app.db",
            temp_dir=root / "tmp",
        )
        self.app = make_wsgi_app(self.settings)

    def tearDown(self) -> None:
        reset_broker()
        self.tmp.cleanup()

    async def test_requests_share_one_connection(self) -> None:
        for _ in range(3):
            status, _, body = await asyncio.to_thread(
                _call_full, self.app, "GET", "/api/v1/healthz"
            )
            self.assertTrue(status.startswith("200"), body)
        from gamas_bot.db_broker import get_broker

        self.assertEqual(len(get_broker()._entries), 1)

    async def test_healthz_reports_database_health(self) -> None:
        status, headers, body = await asyncio.to_thread(
            _call_full, self.app, "GET", "/api/v1/healthz"
        )
        self.assertTrue(status.startswith("200"))
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["db"], "ok")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    async def test_healthz_fails_when_database_is_unreachable(self) -> None:
        root = Path(self.tmp.name)
        blocker = root / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        settings = make_settings(
            bot_token=BOT_TOKEN,
            db_path=blocker / "impossible" / "app.db",
            temp_dir=root / "tmp",
        )
        app = make_wsgi_app(settings)
        status, _, body = await asyncio.to_thread(
            _call_full, app, "GET", "/api/v1/healthz"
        )
        self.assertTrue(status.startswith("503"), body)
        self.assertEqual(json.loads(body)["db"], "down")

    async def test_settings_are_parsed_once_per_env_change(self) -> None:
        import gamas_bot.webapp_api as webapp_api

        counter = {"calls": 0}

        def fake_from_env():
            counter["calls"] += 1
            return self.settings

        try:
            with (
                mock.patch.object(webapp_api, "from_env", side_effect=fake_from_env),
                mock.patch.object(webapp_api, "_env_file_stamp", return_value=("fixed",)),
            ):
                webapp_api._env_settings_cache = None
                app = make_wsgi_app()
                for _ in range(3):
                    await asyncio.to_thread(_call_full, app, "GET", "/api/v1/healthz")
                self.assertEqual(counter["calls"], 1)
                with mock.patch.object(
                    webapp_api, "_env_file_stamp", return_value=("changed",)
                ):
                    await asyncio.to_thread(_call_full, app, "GET", "/api/v1/healthz")
            self.assertEqual(counter["calls"], 2)
        finally:
            webapp_api._env_settings_cache = None


class GzipStaticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dist = root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        self.js_body = b"// payload\n" + b"x = 1;\n" * 400  # > 1 KB
        (self.dist / "assets" / "index-DEADBEEF.js").write_bytes(self.js_body)
        (self.dist / "index.html").write_bytes(b"<html>" + b"y" * 1500)
        (self.dist / "assets" / "tiny.css").write_bytes(b".a{color:red}")
        self.gz_bytes = b"\x1f\x8b precompressed"
        (self.dist / "assets" / "legacy.js").write_bytes(b"z" * 2000)
        (self.dist / "assets" / "legacy.js.gz").write_bytes(self.gz_bytes)
        self.settings = make_settings(db_path=root / "g.db", temp_dir=root / "tmp")
        self.app = make_wsgi_app(self.settings, miniapp_dist=self.dist)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _get(self, path: str, accept: str | None = None):
        headers = {"Accept-Encoding": accept} if accept is not None else {}
        return _call_full(self.app, "GET", path, headers=headers)

    def test_text_assets_are_served_compressed(self) -> None:
        status, headers, body = self._get(
            "/app/assets/index-DEADBEEF.js", accept="gzip, deflate, br"
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(headers.get("Vary"), "Accept-Encoding")
        self.assertEqual(gzip.decompress(body), self.js_body)

    def test_identity_when_client_does_not_accept_gzip(self) -> None:
        status, headers, body = self._get("/app/assets/index-DEADBEEF.js")
        self.assertTrue(status.startswith("200"))
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(headers.get("Vary"), "Accept-Encoding")
        self.assertEqual(body, self.js_body)

    def test_gzip_refused_with_zero_quality(self) -> None:
        _, headers, body = self._get("/app/assets/index-DEADBEEF.js", accept="gzip;q=0")
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(body, self.js_body)

    def test_precompressed_sibling_wins(self) -> None:
        _, headers, body = self._get("/app/assets/legacy.js", accept="gzip")
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(body, self.gz_bytes)

    def test_small_assets_stay_identity(self) -> None:
        _, headers, body = self._get("/app/assets/tiny.css", accept="gzip")
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(body, b".a{color:red}")

    def test_index_html_keeps_no_store_and_gzips(self) -> None:
        _, headers, body = self._get("/app/", accept="gzip")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(body)[:6], b"<html>")


class LibraryPaginationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_broker()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = make_settings(
            bot_token=BOT_TOKEN, db_path=root / "lib.db", temp_dir=root / "tmp"
        )
        self.app = make_wsgi_app(self.settings)
        self.db = Database(self.settings.db_path)

    async def asyncSetUp(self) -> None:
        await self.db.connect()
        await self.db.ensure_user(101, "Alice")
        for i in range(5):
            await self.db.study_packs.study_pack_create(
                101, f"pack {i}", source_type="upload"
            )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        reset_broker()
        self.tmp.cleanup()

    async def test_default_listing_reports_total(self) -> None:
        status, _, body = await asyncio.to_thread(
            _call_full, self.app, "GET", "/api/v1/library", headers=_hdrs()
        )
        self.assertTrue(status.startswith("200"), body)
        payload = json.loads(body)
        self.assertEqual(payload["total"], 5)
        self.assertEqual(len(payload["packs"]), 5)

    async def test_limit_and_offset_page_through_packs(self) -> None:
        status, _, body = await asyncio.to_thread(
            _call_full,
            self.app,
            "GET",
            "/api/v1/library?limit=2&offset=2",
            headers=_hdrs(),
        )
        self.assertTrue(status.startswith("200"), body)
        payload = json.loads(body)
        self.assertEqual(payload["total"], 5)
        self.assertEqual(payload["limit"], 2)
        self.assertEqual(payload["offset"], 2)
        self.assertEqual(len(payload["packs"]), 2)
        titles = [pack["title"] for pack in payload["packs"]]
        # Newest first: pages run pack 4, 3, 2, 1, 0.
        self.assertEqual(titles, ["pack 2", "pack 1"])


class TranscriptDedupeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "dedupe.db")

    async def asyncSetUp(self) -> None:
        await self.db.connect()
        await self.db.ensure_user(5, "u")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_source_text_is_stored_once_per_submission(self) -> None:
        first = await self.db.library_record_delivered_file(
            5,
            "booklet",
            title="PDF",
            chat_id=5,
            message_id=1,
            submission_id=42,
            artifact_kind="pdf",
            content_text="متن کامل جزوه",
        )
        second = await self.db.library_record_delivered_file(
            5,
            "booklet",
            title="Word",
            chat_id=5,
            message_id=2,
            submission_id=42,
            artifact_kind="docx",
            content_text="متن کامل جزوه",
        )
        first_detail = await self.db.library_item_detail(5, first)
        second_detail = await self.db.library_item_detail(5, second)
        self.assertEqual(first_detail["content_text"], "متن کامل جزوه")
        self.assertIsNone(second_detail["content_text"])
        self.assertEqual(
            await self.db.library_submission_source_text(5, 42), "متن کامل جزوه"
        )

    async def test_rows_without_submission_keep_their_text(self) -> None:
        item_id = await self.db.library_record_delivered_file(
            5,
            "transcript",
            title="متن",
            chat_id=5,
            message_id=3,
            content_text="متن مستقل",
        )
        detail = await self.db.library_item_detail(5, item_id)
        self.assertEqual(detail["content_text"], "متن مستقل")


def _double(value: int) -> int:
    """Module-level (picklable) helper for the process-pool test."""
    return value * 2


class CpuPoolTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        cpu_pool.shutdown_pool()

    async def test_thread_path_by_default(self) -> None:
        with mock.patch.dict(os.environ, {"GAMAS_BUILD_POOL": ""}):
            self.assertFalse(cpu_pool.pool_enabled())
            self.assertEqual(await cpu_pool.run_cpu_bound(_double, 3), 6)

    async def test_unpicklable_callable_falls_back_to_thread(self) -> None:
        with mock.patch.dict(os.environ, {"GAMAS_BUILD_POOL": "1"}):
            self.assertEqual(
                await cpu_pool.run_cpu_bound(lambda x: x + 1, 4),
                5,
            )

    async def test_picklable_callable_runs_in_pool_when_enabled(self) -> None:
        with mock.patch.dict(os.environ, {"GAMAS_BUILD_POOL": "1"}):
            self.assertEqual(await cpu_pool.run_cpu_bound(_double, 21), 42)


class JobsReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "report.db")

    async def asyncSetUp(self) -> None:
        await self.db.connect()
        await self.db.ensure_user(9, "u")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    def test_summarize_daily_groups_and_percentiles(self) -> None:
        rows = [
            {"created_at": "2026-11-27T01:00:00+00:00", "status": "done", "kind": "audio",
             "duration_seconds": 10},
            {"created_at": "2026-11-27T02:00:00+00:00", "status": "failed", "kind": "audio",
             "duration_seconds": 0},
            {"created_at": "2026-11-26T01:00:00+00:00", "status": "done", "kind": "video",
             "duration_seconds": 30},
            {"created_at": "2026-11-26T02:00:00+00:00", "status": "partial", "kind": "video",
             "duration_seconds": 50},
            {"created_at": "2026-11-20T01:00:00+00:00", "status": "done", "kind": "audio",
             "duration_seconds": 10},  # outside the window
        ]
        report = summarize_daily(
            rows, days=7, now=datetime(2026, 11, 27, tzinfo=timezone.utc)
        )
        self.assertEqual(len(report["days"]), 2)
        newest = report["days"][0]
        self.assertEqual(newest["day"], "2026-11-27")
        self.assertEqual(newest["total"], 2)
        self.assertEqual(newest["by_status"], {"done": 1, "failed": 1})
        self.assertEqual(newest["success_rate"], 0.5)
        totals = report["totals"]
        self.assertEqual(totals["total"], 4)
        self.assertEqual(totals["succeeded"], 3)
        self.assertEqual(totals["p50_duration_s"], 30)
        self.assertEqual(totals["p95_duration_s"], 50)

    async def test_fetch_report_reads_the_ledger(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for i, (status, duration) in enumerate(
            [("done", 20.0), ("failed", 0.0), ("partial", 40.0)], start=1
        ):
            await self.db.db.execute(
                """
                INSERT INTO submissions(
                    submission_id, user_id, kind, filename, status, error,
                    duration_seconds, created_at, finished_at
                ) VALUES (?, 9, 'audio', 'a.ogg', ?, NULL, ?, ?, ?)
                """,
                (
                    i,
                    status,
                    duration,
                    f"{today}T0{i}:00:00+00:00",
                    f"{today}T0{i}:30:00+00:00",
                ),
            )
        await self.db.db.commit()
        report = await fetch_daily_job_report(self.db, days=7)
        self.assertEqual(report["totals"]["total"], 3)
        self.assertEqual(report["totals"]["terminal"], 3)
        self.assertEqual(report["totals"]["succeeded"], 2)
        self.assertEqual(report["days"][0]["day"], today)


class SizingTests(unittest.TestCase):
    def test_recommendation_follows_available_memory(self) -> None:
        self.assertEqual(recommended_max_active_jobs(mem_available_mib=4096), 6)
        self.assertEqual(recommended_max_active_jobs(mem_available_mib=2048), 6)
        self.assertEqual(recommended_max_active_jobs(mem_available_mib=2047), 3)
        self.assertEqual(recommended_max_active_jobs(mem_available_mib=128), 3)
        with mock.patch(
            "gamas_bot.config.available_memory_mib", return_value=None
        ):
            self.assertEqual(recommended_max_active_jobs(), 3)


class LauncherRamGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = make_settings(
            db_path=root / "g.db",
            temp_dir=root / "tmp",
            session_path=root / "data" / "session",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_watchdog_defers_spawn_when_memory_is_critical(self) -> None:
        from gamas_bot import launcher

        with (
            mock.patch.object(launcher, "from_env", return_value=self.settings),
            mock.patch.object(launcher, "check", return_value=[]),
            mock.patch.object(launcher, "is_locked", return_value=False),
            mock.patch.object(launcher, "available_memory_mib", return_value=1),
            mock.patch.object(launcher, "subprocess") as subprocess_mock,
        ):
            state = launcher.ensure_running(min_interval=0, min_free_memory_mib=64)
        self.assertEqual(state, launcher.THROTTLED)
        subprocess_mock.Popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
