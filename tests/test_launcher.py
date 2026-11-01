"""Tests for watchdog serialization, readiness verification, lifecycle CLI, and Passenger WSGI."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from gamas_bot import launcher
from tests.support import make_settings


class _FakeChildProc:
    def __init__(self, *, exit_code: int | None = None) -> None:
        self._exit_code = exit_code

    def poll(self) -> int | None:
        return self._exit_code


class LauncherLifecycleTests(unittest.TestCase):
    def test_concurrent_watchdogs_spawn_at_most_one_bot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = make_settings(
                session_path=root / "data" / "gamas",
                db_path=root / "data" / "bot.db",
                temp_dir=root / "data" / "tmp",
                log_file=None,
            )
            spawn_count = 0
            spawn_lock = threading.Lock()
            bot_locked = False
            start = threading.Barrier(3)

            def fake_popen(*_args, **_kwargs):
                nonlocal spawn_count, bot_locked
                with spawn_lock:
                    spawn_count += 1
                    bot_locked = True
                time.sleep(0.05)
                return _FakeChildProc(exit_code=None)

            def fake_is_locked(_path):
                return bot_locked

            def call_launcher():
                start.wait()
                return launcher.ensure_running(
                    min_interval=45, startup_verify_seconds=0.4
                )

            with (
                mock.patch.object(launcher, "from_env", return_value=settings),
                mock.patch.object(launcher, "check", return_value=[]),
                mock.patch.object(launcher, "is_locked", side_effect=fake_is_locked),
                mock.patch.object(launcher.subprocess, "Popen", side_effect=fake_popen),
            ):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = [pool.submit(call_launcher) for _ in range(2)]
                    start.wait()
                    statuses = [result.result(timeout=5) for result in results]

            self.assertEqual(spawn_count, 1)
            self.assertEqual(statuses.count(launcher.STARTED), 1)
            self.assertTrue(
                statuses.count(launcher.THROTTLED) + statuses.count(launcher.RUNNING) == 1
            )

    def test_misconfigured_settings_fail_fast_without_spawning_and_write_launcher_err(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = make_settings(
                session_path=root / "data" / "gamas",
                db_path=root / "data" / "bot.db",
                temp_dir=root / "data" / "tmp",
                log_file=root / "data" / "logs" / "bot.log",
            )
            with (
                mock.patch.object(launcher, "from_env", return_value=settings),
                mock.patch.object(launcher, "is_locked", return_value=False),
                mock.patch.object(
                    launcher,
                    "check",
                    return_value=["no STT provider is configured"],
                ),
                mock.patch.object(launcher.subprocess, "Popen") as popen,
            ):
                status = launcher.ensure_running(min_interval=45)

            self.assertEqual(status, launcher.MISCONFIGURED)
            popen.assert_not_called()
            err_path = root / "data" / "logs" / "launcher.err"
            self.assertTrue(err_path.is_file())
            self.assertIn("no STT provider is configured", err_path.read_text(encoding="utf-8"))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(err_path.stat().st_mode), 0o600)

    def test_child_crash_during_startup_returns_failed_and_records_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = make_settings(
                session_path=root / "data" / "gamas",
                db_path=root / "data" / "bot.db",
                temp_dir=root / "data" / "tmp",
                log_file=root / "data" / "logs" / "bot.log",
            )
            with (
                mock.patch.object(launcher, "from_env", return_value=settings),
                mock.patch.object(launcher, "check", return_value=[]),
                mock.patch.object(launcher, "is_locked", return_value=False),
                mock.patch.object(
                    launcher.subprocess,
                    "Popen",
                    return_value=_FakeChildProc(exit_code=2),
                ) as popen,
            ):
                outcome = launcher.ensure_running(
                    min_interval=0.0, startup_verify_seconds=0.25
                )

            self.assertEqual(outcome, launcher.FAILED)
            popen.assert_called_once()
            err_text = (root / "data" / "logs" / "launcher.err").read_text(encoding="utf-8")
            self.assertIn("exited immediately during startup (exit code 2)", err_text)

    def test_ensure_running_cli_propagates_misconfigured_exit_code_2(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": temp,
                "TELEGRAM_API_ID": "12345",
                "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_SESSION": str(Path(temp) / "data" / "gamas"),
                "DB_PATH": str(Path(temp) / "data" / "bot.db"),
                "TEMP_DIR": str(Path(temp) / "data" / "tmp"),
                "RECEIPT_DIR": str(Path(temp) / "data" / "receipts"),
                "LOG_FILE": str(Path(temp) / "data" / "logs" / "bot.log"),
            }
            proc = subprocess.run(
                [sys.executable, str(repo_root / "scripts" / "ensure_running.py")],
                cwd=temp,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout.strip(), "misconfigured")
            self.assertIn("TELEGRAM_BOT_TOKEN", proc.stderr)

    def test_passenger_wsgi_reports_accurate_http_status_and_handles_head_and_404(self):
        import passenger_wsgi

        captured: dict[str, object] = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        with mock.patch.object(passenger_wsgi, "ensure_running", return_value="misconfigured"):
            body = b"".join(
                passenger_wsgi.application(
                    {"PATH_INFO": "/health", "REQUEST_METHOD": "GET"}, start_response
                )
            )
        self.assertEqual(captured["status"], "503 Service Unavailable")
        self.assertEqual(json.loads(body), {"service": "gamas-bot", "status": "error"})

        with mock.patch.object(passenger_wsgi, "ensure_running", return_value="running"):
            head_body = b"".join(
                passenger_wsgi.application(
                    {"PATH_INFO": "/", "REQUEST_METHOD": "HEAD"}, start_response
                )
            )
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(head_body, b"")

        not_found = b"".join(
            passenger_wsgi.application(
                {"PATH_INFO": "/.env", "REQUEST_METHOD": "GET"}, start_response
            )
        )
        self.assertEqual(captured["status"], "404 Not Found")
        self.assertEqual(json.loads(not_found), {"error": "not found"})


if __name__ == "__main__":
    unittest.main()
