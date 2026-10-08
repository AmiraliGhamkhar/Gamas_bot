"""Regression tests for shared-hosting (cPanel) deployment and the review fixes."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from gamas_bot import launcher
from gamas_bot.bot import (
    _emphasis_is_nested,
    _parse_user_id,
    markdown_to_telegram_html,
)
from gamas_bot.config import PROJECT_ROOT, Settings, parse_proxy
from gamas_bot.docx_export import (
    DocumentMeta,
    build_notes_docx,
    build_plain_docx,
    notes_docx_filename,
    xml_safe,
)
from gamas_bot.instance_lock import AlreadyRunningError, InstanceLock, is_locked
from gamas_bot.structuring import StructuredNotes

from support import docx_text

REQUIRED_ENV = {
    "TELEGRAM_BOT_TOKEN": "token",
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "hash",
    "DEEPGRAM_API_KEY": "key",
}


def _env(**extra: str) -> dict[str, str]:
    return {**REQUIRED_ENV, **extra}


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "nested" / "bot.lock"

    def test_second_holder_is_rejected_until_release(self):
        first = InstanceLock(self.path)
        first.acquire()
        with self.assertRaises(AlreadyRunningError):
            InstanceLock(self.path).acquire()
        self.assertTrue(is_locked(self.path))
        first.release()
        self.assertFalse(is_locked(self.path))
        with InstanceLock(self.path):
            self.assertTrue(is_locked(self.path))

    def test_lock_is_freed_when_holder_is_killed(self):
        code = (
            "import sys, time; from pathlib import Path; "
            "from gamas_bot.instance_lock import InstanceLock; "
            "InstanceLock(Path(sys.argv[1])).acquire(); print('held', flush=True); time.sleep(60)"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", code, str(self.path)],
            stdout=subprocess.PIPE, cwd=PROJECT_ROOT, text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertTrue(is_locked(self.path))
            holder.send_signal(signal.SIGKILL)
            holder.wait(timeout=10)
            self.assertFalse(is_locked(self.path), "a crash must never leave a stale lock")
        finally:
            holder.kill()
            holder.stdout.close()

    def test_second_bot_process_exits_with_code_3(self):
        session = Path(self._tmp.name) / "telegram_bot"
        settings_env = _env(
            TELEGRAM_SESSION_PATH=str(session),
            DATABASE_PATH=str(Path(self._tmp.name) / "db.sqlite3"),
            TEMP_DIR=str(Path(self._tmp.name) / "tmp"),
        )
        with patch.dict(os.environ, settings_env, clear=True):
            lock_path = Settings.from_env("/nonexistent").lock_path
        with InstanceLock(lock_path):
            done = subprocess.run(
                [sys.executable, "-m", "gamas_bot"],
                cwd=PROJECT_ROOT,
                env={**os.environ, **settings_env},
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("Not starting", done.stdout + done.stderr)


class SelfCheckCommandTests(unittest.TestCase):
    """``python -m gamas_bot --check``: documented, offline, side-effect free."""

    def _run_check(self, env: dict[str, str]):
        return subprocess.run(
            [sys.executable, "-m", "gamas_bot", "--check"],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_reports_missing_configuration_with_exit_code_2(self):
        # Empty strings win over any project .env (load_dotenv does not
        # override), so this is deterministic even on a configured machine.
        env = {
            **os.environ,
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_API_ID": "",
            "TELEGRAM_API_HASH": "",
            "ADMIN_IDS": "",
            "SPEECHMATICS_API_KEY": "",
            "DEEPGRAM_API_KEY": "",
            "STT_OPENAI_BASE_URL": "",
            "PROVIDER_CREDENTIALS_ENCRYPTION_KEY": "",
        }
        done = self._run_check(env)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("FAIL configuration", done.stdout)
        self.assertNotIn("PASS media worker", done.stdout)

    def test_valid_configuration_passes_without_creating_any_state(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            env = _env(
                TELEGRAM_BOT_TOKEN="123:TEST",
                TELEGRAM_API_ID="1",
                TELEGRAM_API_HASH="0123456789abcdef0123456789abcdef",
                ADMIN_IDS="1",
                DEEPGRAM_API_KEY="key",
                TELEGRAM_SESSION_PATH=str(state / "session"),
                DATABASE_PATH=str(state / "db.sqlite3"),
                TEMP_DIR=str(state / "tmp"),
            )
            done = self._run_check(env)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("PASS configuration", done.stdout)
            self.assertIn("PASS dependencies", done.stdout)
            self.assertIn("PASS media worker", done.stdout)
            # No database, no session, no lock, no temp work: the check never
            # mutates state, so it is safe while a healthy bot is running.
            self.assertEqual(
                list(state.iterdir()), [],
                "--check must not create database, session, lock or temp files",
            )
            # The check never starts (or refuses to start) the bot itself.
            self.assertNotIn("Not starting", done.stdout + done.stderr)
            self.assertNotIn("Persian study assistant is online", done.stdout)

    def test_check_is_never_confused_with_a_start_request(self):
        # ``--check`` must exit before the instance lock: running it twice
        # concurrently against a free lock is not a lock conflict (exit 3).
        env = _env(
            TELEGRAM_SESSION_PATH=str(Path(tempfile.gettempdir()) / "gamas-check-session"),
            DATABASE_PATH=str(Path(tempfile.gettempdir()) / "gamas-check.sqlite3"),
            TEMP_DIR=str(Path(tempfile.gettempdir()) / "gamas-check-tmp"),
        )
        first = self._run_check(env)
        second = self._run_check(env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)


class GracefulShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_runs_the_same_shutdown_path_as_ctrl_c(self):
        import asyncio

        from gamas_bot import __main__ as entry

        events: list[str] = []

        class FakeBot:
            def __init__(self, _settings):
                pass

            async def run(self):
                try:
                    events.append("running")
                    await asyncio.sleep(30)
                finally:
                    events.append("shutdown")

        asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
        with patch.object(entry, "StudyBot", FakeBot), self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(entry.main(object()), timeout=10)
        self.assertEqual(events, ["running", "shutdown"])


class PathAndProxyConfigTests(unittest.TestCase):
    def test_relative_paths_are_anchored_to_the_project_not_the_cwd(self):
        with tempfile.TemporaryDirectory() as elsewhere, patch.dict(
            os.environ, _env(LOG_FILE="logs/x.log"), clear=True
        ):
            previous = os.getcwd()
            os.chdir(elsewhere)
            try:
                settings = Settings.from_env("/nonexistent")
            finally:
                os.chdir(previous)
        for path in (
            settings.database_path, settings.session_path, settings.temp_dir, settings.log_file,
        ):
            self.assertTrue(path.is_absolute(), path)
            self.assertTrue(str(path).startswith(str(PROJECT_ROOT)), path)

    def test_absolute_and_home_paths_are_respected(self):
        # Pin HOME for the whole assertion: with the environment cleared,
        # Path.expanduser() falls back to the passwd entry, which can differ
        # from the caller's HOME (e.g. containers running as root with a
        # non-root HOME), making this test fail spuriously.
        home = os.environ.get("HOME") or str(Path.home())
        with patch.dict(
            os.environ,
            _env(
                DATABASE_PATH="/srv/x/db.sqlite3",
                TEMP_DIR="~/gamas-tmp",
                HOME=home,
            ),
            clear=True,
        ):
            settings = Settings.from_env("/nonexistent")
        self.assertEqual(settings.database_path, Path("/srv/x/db.sqlite3"))
        self.assertEqual(settings.temp_dir, Path(home) / "gamas-tmp")

    def test_env_file_is_found_via_project_root_when_cwd_has_none(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cwd:
            (Path(root) / ".env").write_text("TELEGRAM_BOT_TOKEN=from-project-env\n")
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                with patch("gamas_bot.config.PROJECT_ROOT", Path(root)), patch.dict(
                    os.environ, {}, clear=True
                ):
                    settings = Settings.from_env()
            finally:
                os.chdir(previous)
        self.assertEqual(settings.telegram_bot_token, "from-project-env")

    def test_lock_path_follows_the_session_path(self):
        with patch.dict(os.environ, _env(TELEGRAM_SESSION_PATH="/srv/s/tg"), clear=True):
            settings = Settings.from_env("/nonexistent")
        self.assertEqual(settings.lock_path, Path("/srv/s/tg.lock"))

    def test_proxy_parsing(self):
        self.assertEqual(
            parse_proxy("socks5://u%40x:p%3Aw@proxy.example:1080"),
            ("socks5", "proxy.example", 1080, True, "u@x", "p:w"),
        )
        self.assertEqual(parse_proxy("http://h:3128"), ("http", "h", 3128, True, None, None))
        for bad in ("proxy.example:1080", "ftp://h:1", "socks5://h", "socks5://:1080", "socks5://h:x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_proxy(bad)

    def test_proxy_reaches_settings_and_client(self):
        with patch.dict(os.environ, _env(TELEGRAM_PROXY="socks5://h:1080"), clear=True):
            settings = Settings.from_env("/nonexistent")
        self.assertEqual(settings.telegram_proxy, ("socks5", "h", 1080, True, None, None))
        with tempfile.TemporaryDirectory() as tmp, patch("gamas_bot.bot.TelegramClient") as client:
            from dataclasses import replace

            from gamas_bot.bot import StudyBot

            StudyBot(replace(settings, session_path=Path(tmp) / "s", database_path=Path(tmp) / "d"))
        self.assertEqual(client.call_args.kwargs["proxy"], settings.telegram_proxy)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / "data"
        env = _env(
            TELEGRAM_SESSION_PATH=str(self.data / "tg"),
            DATABASE_PATH=str(self.data / "db.sqlite3"),
        )
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        env_patch = patch("gamas_bot.config._default_env_file", return_value=Path("/nonexistent"))
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_running_bot_is_left_alone(self):
        with InstanceLock(self.data / "tg.lock"), patch("subprocess.Popen") as popen:
            self.assertEqual(launcher.ensure_running(), launcher.RUNNING)
        popen.assert_not_called()

    def test_stopped_bot_is_started_with_log_file_and_detached(self):
        with patch("subprocess.Popen") as popen:
            self.assertEqual(launcher.ensure_running(), launcher.STARTED)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "gamas_bot"])
        self.assertEqual(kwargs["cwd"], PROJECT_ROOT)
        self.assertEqual(kwargs["env"]["LOG_FILE"], str(self.data / "logs" / "bot.log"))
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        self.assertNotIn("shell", kwargs)

    def test_configured_log_file_is_not_overridden(self):
        with patch.dict(os.environ, {"LOG_FILE": "/srv/bot.log"}), patch("subprocess.Popen") as popen:
            launcher.ensure_running()
        self.assertEqual(popen.call_args.kwargs["env"]["LOG_FILE"], "/srv/bot.log")

    def test_crash_looping_bot_is_not_relaunched_every_request(self):
        with patch("subprocess.Popen") as popen:
            self.assertEqual(launcher.ensure_running(), launcher.STARTED)
            self.assertEqual(launcher.ensure_running(), launcher.THROTTLED)
            self.assertEqual(popen.call_count, 1)
            time.sleep(0.05)
            self.assertEqual(launcher.ensure_running(min_interval=0.01), launcher.STARTED)
            self.assertEqual(popen.call_count, 2)

    def test_missing_configuration_is_reported_not_started(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}), patch("subprocess.Popen") as popen:
            self.assertEqual(launcher.ensure_running(), launcher.MISCONFIGURED)
        popen.assert_not_called()


class PassengerAppTests(unittest.TestCase):
    def _call(self, path="/", method="GET", state=launcher.RUNNING):
        sys.path.insert(0, str(PROJECT_ROOT))
        self.addCleanup(sys.path.remove, str(PROJECT_ROOT))
        import passenger_wsgi

        captured = {}

        def start_response(status, headers):
            captured["status"], captured["headers"] = status, dict(headers)

        with patch.object(passenger_wsgi, "ensure_running", return_value=state):
            body = b"".join(
                passenger_wsgi.application(
                    {"PATH_INFO": path, "REQUEST_METHOD": method}, start_response
                )
            )
        return captured, body

    def test_running_bot_reports_200_without_leaking_details(self):
        captured, body = self._call()
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(json.loads(body), {"service": "gamas-bot", "status": "running"})

    def test_starting_and_error_states_are_503(self):
        for state, label in ((launcher.STARTED, "starting"), (launcher.THROTTLED, "starting"),
                             (launcher.MISCONFIGURED, "error")):
            with self.subTest(state=state):
                captured, body = self._call(state=state)
                self.assertTrue(captured["status"].startswith("503"))
                self.assertEqual(json.loads(body)["status"], label)

    def test_other_paths_and_methods_do_not_trigger_a_launch(self):
        for path, method in (("/.env", "GET"), ("/data/bot.sqlite3", "GET"), ("/", "POST")):
            with self.subTest(path=path, method=method):
                captured, _ = self._call(path, method)
                self.assertTrue(captured["status"].startswith("404"))

    def test_head_has_no_body(self):
        captured, body = self._call(method="HEAD")
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(body, b"")


class XmlSafetyTests(unittest.TestCase):
    META = DocumentMeta(
        reference="GMS-000001", source_name="lec\x0bture\x00.mp3", created_at=datetime(2026, 1, 1)
    )

    def test_xml_safe_helper(self):
        self.assertEqual(xml_safe("a\x00b\x01c\x0bd\x0ce\ud800f\ufffeg"), "abc d efg")
        self.assertEqual(xml_safe("سلام\nخط\tتب"), "سلام\nخط\tتب")

    def test_control_characters_no_longer_cost_the_word_document(self):
        notes = StructuredNotes.from_payload({
            "title": "عنوان\x0b",
            "summary": "خلاصه\x1f",
            "sections": [{
                "heading": "بخش\x00",
                "paragraphs": ["متن\x0c\x08"],
                "table": {"headers": ["ستون\x01"], "rows": [["سلول\x02"]]},
            }],
            "glossary": [{"term": "اصطلاح\x03", "definition": "تعریف\x04"}],
        })
        text = docx_text(build_notes_docx(notes, font="Tahoma", meta=self.META))
        self.assertIn("متن", text)
        self.assertIn("تعریف", text)
        self.assertNotIn("\x00", text)
        self.assertIn("خط اول", docx_text(build_plain_docx("عنوان\x0b", "خط\x00 اول", font="Tahoma", meta=self.META)))

    def test_filenames_never_contain_control_characters(self):
        notes = StructuredNotes.from_payload({"title": "a\x00b\x1fc", "summary": "s"})
        name = notes_docx_filename(notes, "GMS-000001")
        self.assertFalse(any(ord(ch) < 32 for ch in name), repr(name))


class TelegramMarkupTests(unittest.TestCase):
    def test_crossed_emphasis_never_produces_unparseable_html(self):
        for source in ("**a *b** c*", "*a **b* c**", "_x **y_ z**"):
            with self.subTest(source=source):
                rendered = markdown_to_telegram_html(source)
                self.assertTrue(_emphasis_is_nested(rendered), rendered)

    def test_proper_nesting_still_renders(self):
        self.assertEqual(
            markdown_to_telegram_html("**bold *both* end**"), "<b>bold <i>both</i> end</b>"
        )
        self.assertEqual(markdown_to_telegram_html("`c` **b**"), "<code>c</code> <b>b</b>")

    def test_user_id_parsing(self):
        self.assertEqual(_parse_user_id(" 123456 "), 123456)
        self.assertEqual(_parse_user_id("+42"), 42)
        self.assertEqual(_parse_user_id("۱۲۳"), 123)
        for bad in ("", "abc", "12a", "²³", "-5", "1" * 40):
            with self.subTest(bad=bad):
                self.assertIsNone(_parse_user_id(bad))


class PreflightTests(unittest.TestCase):
    def test_flags_projects_inside_the_web_root(self):
        from scripts import cpanel_preflight as pf

        self.assertEqual(pf.check_location(Path("/home/u/public_html/gamas")).level, "FAIL")
        self.assertEqual(pf.check_location(Path("/home/u/gamas_bot")).level, "PASS")

    def test_lock_support_and_directories(self):
        from scripts import cpanel_preflight as pf

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pf.check_lock_support(Path(tmp)).level, "PASS")
            self.assertIn(pf.check_directory("d", Path(tmp) / "new").level, {"PASS", "WARN"})

    def test_static_toc_preflight_follows_the_page_number_policy(self):
        from types import SimpleNamespace

        from scripts import cpanel_preflight as pf

        settings = SimpleNamespace(
            docx_toc_enabled=True,
            docx_pagination_renderer_bin=None,
            docx_toc_page_numbers="auto",
        )
        with patch.object(pf.shutil, "which", return_value=None):
            # auto degrades to a link-only TOC; it is a warning, not a failure.
            self.assertEqual(pf.check_docx_pagination(settings).level, "WARN")
            settings.docx_toc_page_numbers = "required"
            self.assertEqual(pf.check_docx_pagination(settings).level, "FAIL")
            settings.docx_toc_page_numbers = "off"
            self.assertEqual(pf.check_docx_pagination(settings).level, "PASS")
            settings.docx_toc_page_numbers = "auto"
        settings.docx_toc_enabled = False
        self.assertEqual(pf.check_docx_pagination(settings).level, "PASS")

    def test_provider_master_key_preflight_never_prints_the_secret(self):
        from types import SimpleNamespace

        from cryptography.fernet import Fernet

        from scripts import cpanel_preflight as pf

        key = Fernet.generate_key().decode("ascii")
        result = pf.check_credential_encryption(
            SimpleNamespace(provider_credentials_encryption_key=key)
        )
        self.assertEqual(result.level, "PASS")
        self.assertNotIn(key, result.detail)
        invalid = pf.check_credential_encryption(
            SimpleNamespace(provider_credentials_encryption_key="not-a-key")
        )
        self.assertEqual(invalid.level, "FAIL")
        self.assertNotIn("not-a-key", invalid.detail)

    def test_receipts_must_live_in_private_non_web_directory(self):
        from scripts import cpanel_preflight as pf

        with tempfile.TemporaryDirectory() as tmp:
            result = pf.check_private_receipt_directory(Path(tmp) / "receipts")
            self.assertEqual(result.level, "PASS")
            self.assertEqual((Path(tmp) / "receipts").stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                pf.check_private_receipt_directory(Path(tmp) / "public_html" / "receipts").level,
                "FAIL",
            )

    def test_unreachable_telegram_is_a_failure(self):
        from scripts import cpanel_preflight as pf

        with patch.object(pf, "tcp_reachable", return_value="timed out"):
            results = pf.check_network(None)
        self.assertEqual([r.level for r in results], ["FAIL"])
        with patch.object(pf, "tcp_reachable", return_value=None):
            self.assertEqual(pf.check_network(None)[0].level, "PASS")

    def test_offline_run_reports_missing_configuration(self):
        from scripts import cpanel_preflight as pf

        out = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(
            pf, "ROOT", Path("/nonexistent")
        ), patch("sys.stdout", out):
            code = pf.main(["--offline"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] Configuration", out.getvalue())


if __name__ == "__main__":
    unittest.main()
