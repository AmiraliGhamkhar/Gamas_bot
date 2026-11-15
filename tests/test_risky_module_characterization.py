"""Direct characterization checks for security-relevant composition seams."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gamas_bot._prompt_block import build_chunk_message, build_system_prompt
from gamas_bot.bot import GamasBot
from gamas_bot.bot_admin import AdminWorkflowMixin
from gamas_bot.bot_documents import DocumentOfficeWorkflowMixin
from gamas_bot.db import Database
from gamas_bot.db_billing import BillingRepositoryMixin
from gamas_bot.launcher import FAILED, STARTED, _verify_child_startup
from tests.support import make_settings


class _ExitedProcess:
    def __init__(self, return_code: int) -> None:
        self.return_code = return_code

    def poll(self) -> int:
        return self.return_code


class RiskyModuleCharacterizationTests(unittest.TestCase):
    def test_prompt_contract_keeps_document_text_out_of_the_static_system_prompt(self):
        injected = "UNTRUSTED_INSTRUCTION_9f1a"
        self.assertNotIn(injected, build_system_prompt(context_block=injected))
        message = build_chunk_message(injected, index=1, total=1)
        self.assertTrue(message.endswith(injected))

    def test_composed_bot_uses_admin_document_and_billing_workflow_mixins(self):
        self.assertTrue(issubclass(GamasBot, AdminWorkflowMixin))
        self.assertTrue(issubclass(GamasBot, DocumentOfficeWorkflowMixin))
        self.assertTrue(issubclass(Database, BillingRepositoryMixin))

    def test_launcher_does_not_report_an_immediate_child_failure_as_started(self):
        with tempfile.TemporaryDirectory() as folder:
            outcome, return_code = _verify_child_startup(
                _ExitedProcess(2), Path(folder) / "unused.lock", timeout=0.0
            )
        self.assertEqual((outcome, return_code), (FAILED, 2))

    def test_launcher_accepts_a_non_process_test_double_without_claiming_a_failure(self):
        outcome, return_code = _verify_child_startup(object(), Path("unused.lock"), timeout=0.0)
        self.assertEqual((outcome, return_code), (STARTED, None))

    def test_document_buttons_keep_document_id_in_callback_payloads(self):
        bot = GamasBot(make_settings(office_enabled=True))
        callbacks = [
            button.to_dict()["type"]["data"].decode("ascii")
            for row in bot._document_buttons(321)
            for button in row
        ]
        self.assertTrue(any(value.startswith("doc:note:321:") for value in callbacks))
        self.assertIn("doc:export:321", callbacks)
