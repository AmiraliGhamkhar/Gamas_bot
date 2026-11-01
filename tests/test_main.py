"""Regression tests for ``gamas-bot`` CLI entry point, startup validation, and locks."""

from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import gamas_bot.__main__ as entry
from gamas_bot.__main__ import main
from gamas_bot.config import check
from gamas_bot.instance_lock import InstanceLock, is_locked, read_lock_pid
from tests.support import make_settings


class ConsoleEntryPointTests(unittest.TestCase):
    def test_main_is_a_synchronous_no_argument_entry_point(self):
        """Setuptools invokes a console script as ``sys.exit(main())``."""
        self.assertFalse(inspect.iscoroutinefunction(main))
        with mock.patch("gamas_bot.__main__.run") as run_fn:
            self.assertIsNone(main())
        run_fn.assert_called_once_with()

    def test_invalid_telegram_and_logging_settings_are_rejected_by_config_check(self):
        settings = make_settings(
            bot_token="",
            api_id=0,
            api_hash="",
            log_level="NOT_A_VALID_LEVEL",
            log_format="xml",
        )
        problems = check(settings)
        self.assertTrue(any("TELEGRAM_BOT_TOKEN" in err for err in problems))
        self.assertTrue(any("TELEGRAM_API_ID" in err for err in problems))
        self.assertTrue(any("TELEGRAM_API_HASH" in err for err in problems))
        self.assertTrue(any("LOG_LEVEL" in err for err in problems))
        self.assertTrue(any("LOG_FORMAT" in err for err in problems))

    def test_cli_exits_2_and_writes_fatal_and_advisory_diagnostics_to_log_file(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            log_file = Path(temp) / "logs" / "bot.log"
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(repo_root),
                "HOME": temp,
                "TELEGRAM_API_ID": "12345",
                "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_SESSION": str(Path(temp) / "data" / "gamas"),
                "DB_PATH": str(Path(temp) / "data" / "bot.db"),
                "TEMP_DIR": str(Path(temp) / "data" / "tmp"),
                "RECEIPT_DIR": str(Path(temp) / "data" / "receipts"),
                "LOG_FILE": str(log_file),
            }
            proc = subprocess.run(
                [sys.executable, "-m", "gamas_bot"],
                cwd=temp,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("FAIL configuration:", proc.stderr)
            self.assertIn("TELEGRAM_BOT_TOKEN", proc.stderr)
            self.assertTrue(log_file.is_file())
            logged = log_file.read_text(encoding="utf-8")
            self.assertIn("TELEGRAM_BOT_TOKEN", logged)
            # Advisory warnings must be emitted AFTER configure_logging(settings)
            # so they are recorded in LOG_FILE too.
            self.assertIn("Note generation is not configured", logged)

    def test_instance_lock_records_pid_and_enforces_0600_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            lock_file = Path(temp) / "locks" / "bot.lock"
            self.assertIsNone(read_lock_pid(lock_file))
            self.assertFalse(is_locked(lock_file))

            with InstanceLock(lock_file):
                self.assertTrue(is_locked(lock_file))
                self.assertEqual(read_lock_pid(lock_file), os.getpid())
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(lock_file.stat().st_mode), 0o600)

            self.assertFalse(is_locked(lock_file))

    def test_run_logs_unhandled_crash_invokes_shutdown_and_exits_2(self):
        root = logging.getLogger()
        orig_handlers = list(root.handlers)
        orig_level = root.level
        try:
            with tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                log_file = base / "logs" / "bot.log"
                settings = make_settings(
                    session_path=base / "data" / "gamas",
                    db_path=base / "data" / "bot.db",
                    temp_dir=base / "data" / "tmp",
                    receipt_dir=base / "data" / "receipts",
                    log_file=log_file,
                )
                shutdown_called = False

                class _ExplodingBot:
                    def __init__(self, _settings):
                        pass

                    async def run(self):
                        raise RuntimeError("boom during Telegram connect")

                    async def shutdown(self):
                        nonlocal shutdown_called
                        shutdown_called = True

                with (
                    mock.patch("gamas_bot.config.from_env", return_value=settings),
                    mock.patch.object(entry, "check", return_value=[]),
                    mock.patch("gamas_bot.bot.GamasBot", _ExplodingBot),
                ):
                    with self.assertRaises(SystemExit) as ctx:
                        entry.run([])
                    self.assertEqual(ctx.exception.code, 2)

                self.assertTrue(shutdown_called)
                for handler in logging.getLogger().handlers:
                    handler.flush()
                logged = log_file.read_text(encoding="utf-8")
                self.assertIn("Startup or runtime failure", logged)
                self.assertIn("boom during Telegram connect", logged)
        finally:
            for h in list(root.handlers):
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
            root.setLevel(orig_level)
            for h in orig_handlers:
                root.addHandler(h)


if __name__ == "__main__":
    unittest.main()
