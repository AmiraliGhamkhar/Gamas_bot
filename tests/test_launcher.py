"""Tests for watchdog serialization and single-spawn behavior."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from gamas_bot import launcher
from tests.support import make_settings


class LauncherConcurrencyTests(unittest.TestCase):
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
            start = threading.Barrier(3)

            def fake_popen(*_args, **_kwargs):
                nonlocal spawn_count
                with spawn_lock:
                    spawn_count += 1
                time.sleep(0.1)
                return object()

            def call_launcher():
                start.wait()
                return launcher.ensure_running(min_interval=45)

            with (
                mock.patch.object(launcher, "from_env", return_value=settings),
                mock.patch.object(launcher, "is_locked", return_value=False),
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


if __name__ == "__main__":
    unittest.main()
