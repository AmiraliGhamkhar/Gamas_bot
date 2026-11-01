"""Unit tests for bootstrap/runtime logging, rotation permissions, JSON/plain formatters, and secret redaction."""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import unittest

from gamas_bot.logging_config import (
     configure_logging,
    is_valid_log_level,
    log_job_id,
    redact_log_secrets,
    resolve_log_level,
)
from tests.support import make_settings


class LoggingConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        self._orig_level = root.level
        self._orig_handlers = list(root.handlers)
        for handler in list(root.handlers):
            root.removeHandler(handler)

    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        root.setLevel(self._orig_level)
        for handler in self._orig_handlers:
            root.addHandler(handler)

    def test_bootstrap_logging_allows_later_settings_reconfiguration(self) -> None:
        buf_boot = io.StringIO()
        configure_logging(stream=buf_boot)
        root = logging.getLogger()
        self.assertEqual(root.level, logging.INFO)

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "logs" / "bot.log"
            buf_run = io.StringIO()
            settings = make_settings(
                log_level="DEBUG",
                log_format="plain",
                log_file=log_path,
                log_max_bytes=65536,
                log_backup_count=3,
            )
            configure_logging(settings, stream=buf_run)

            self.assertEqual(root.level, logging.DEBUG)
            logging.getLogger("gamas_bot.test").debug("debug trace after bootstrap")
            for handler in root.handlers:
                handler.flush()

            content = log_path.read_text(encoding="utf-8")
            self.assertIn("debug trace after bootstrap", content)
            self.assertIn("job=-", content)

    def test_rotating_file_handler_enforces_private_0600_permissions_across_rotations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "private" / "bot.log"
            buf = io.StringIO()
            settings = make_settings(
                log_level="INFO",
                log_format="plain",
                log_file=log_path,
                log_max_bytes=512,
                log_backup_count=2,
            )
            configure_logging(settings, stream=buf)
            test_logger = logging.getLogger("gamas_bot.rotation_test")
            for idx in range(30):
                test_logger.info("line %02d padding=%s", idx, "x" * 40)
            for handler in logging.getLogger().handlers:
                handler.flush()

            self.assertTrue(log_path.is_file())
            backup = Path(f"{log_path}.1")
            self.assertTrue(backup.is_file())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(log_path.parent.stat().st_mode), 0o700)

    def test_unwritable_log_file_falls_back_to_stream_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not_a_dir"
            blocker.write_text("file", encoding="utf-8")
            impossible_log = blocker / "nested" / "bot.log"
            buf = io.StringIO()
            settings = make_settings(log_file=impossible_log)

            configure_logging(settings, stream=buf)
            self.assertIn("could not open LOG_FILE", buf.getvalue())

    def test_secret_redaction_scrubs_configured_secrets_and_bearer_tokens_in_plain_and_json(self) -> None:
        bot_tok = "123456789:AAHxyz_SecretBotTokenValue1234567890"
        groq_key = "gsk_abcdefghijklmnopqrstuvwxyz123456"
        redacted = redact_log_secrets(
            f"https://api.telegram.org/bot{bot_tok}/getMe Authorization: Bearer {groq_key}",
            (bot_tok, groq_key),
        )
        self.assertNotIn(bot_tok, redacted)
        self.assertNotIn(groq_key, redacted)

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "json.log"
            buf = io.StringIO()
            settings = make_settings(
                bot_token=bot_tok,
                stt_openai_api_key=groq_key,
                log_format="json",
                log_file=log_path,
            )
            configure_logging(settings, stream=buf)
            log = logging.getLogger("gamas_bot.scrub_test")
            try:
                raise RuntimeError(f"upstream failed key={groq_key} url=bot{bot_tok}")
            except RuntimeError:
                log.exception("caught error with %s", bot_tok)

            for handler in logging.getLogger().handlers:
                handler.flush()
            raw = log_path.read_text(encoding="utf-8").strip()
            self.assertNotIn(bot_tok, raw)
            self.assertNotIn(groq_key, raw)
            parsed = json.loads(raw.splitlines()[-1])
            self.assertEqual(parsed["level"], "ERROR")
            self.assertEqual(parsed["job_id"], "-")

    def test_log_job_id_context_injects_and_restores_tag(self) -> None:
        buf = io.StringIO()
        settings = make_settings(log_format="json", log_file=None)
        configure_logging(settings, stream=buf)
        log = logging.getLogger("gamas_bot.ctx_test")

        log.info("outside before")
        token = log_job_id.set("job-77")
        try:
            log.info("inside job")
        finally:
            log_job_id.reset(token)
        log.info("outside after")

        records = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        self.assertEqual([r["job_id"] for r in records], ["-", "job-77", "-"])

    def test_level_validation_and_third_party_clamping(self) -> None:
        self.assertTrue(is_valid_log_level("INFO"))
        self.assertTrue(is_valid_log_level("warning"))
        self.assertTrue(is_valid_log_level(20))
        self.assertFalse(is_valid_log_level("NOT_A_LEVEL"))
        self.assertEqual(resolve_log_level("NOT_A_LEVEL", logging.ERROR), logging.ERROR)

        buf = io.StringIO()
        configure_logging(make_settings(log_level="INFO", log_file=None), stream=buf)
        self.assertEqual(logging.getLogger("telethon").level, logging.WARNING)
        self.assertEqual(logging.getLogger("aiohttp").level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
