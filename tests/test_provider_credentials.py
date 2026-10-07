from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from gamas_bot.database import Database
from gamas_bot.provider_credentials import (
    CredentialStoreError,
    ProviderCredentialManager,
    use_provider_credentials,
)
from gamas_bot.structuring import ProviderHTTPError, StructuringError, _structure_chunk
from gamas_bot.stt import (
    STTAuthenticationError,
    STTError,
    STTRequestError,
    STTTransientError,
    Transcript,
    transcribe,
)
from support import make_settings


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class ProviderCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "credentials.sqlite3")
        await self.db.open()
        self.master_key = Fernet.generate_key().decode("ascii")
        self.settings = make_settings(
            database_path=self.root / "credentials.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            provider_credentials_encryption_key=self.master_key,
            speechmatics_api_key=None,
            deepgram_api_key=None,
            stt_primary="deepgram",
            stt_fallback_enabled=False,
        )
        self.manager = ProviderCredentialManager(self.db, self.settings)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _add_stt_keys(self) -> tuple[int, int]:
        first = await self.manager.add_credential(
            service="stt",
            provider="deepgram",
            label="primary",
            secret="dg-secret-primary-1234",
            admin_id=700,
            priority=1,
        )
        second = await self.manager.add_credential(
            service="stt",
            provider="deepgram",
            label="backup",
            secret="dg-secret-backup-5678",
            admin_id=700,
            priority=2,
        )
        return first, second

    async def test_api_keys_are_encrypted_at_rest_and_masked_in_views(self):
        secret = "sk-live-very-private-provider-key-9876"
        credential_id = await self.manager.add_credential(
            service="notes",
            provider="gemini",
            label="primary note key",
            secret=secret,
            admin_id=700,
        )
        record = await self.db.provider_credential_record(credential_id)
        self.assertIsNotNone(record)
        self.assertNotEqual(record["secret_ciphertext"], secret)
        self.assertNotIn(secret, str(record))
        self.assertEqual(record["secret_last4"], "9876")

        summaries = await self.manager.list_summaries()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["secret_last4"], "9876")
        self.assertNotIn(secret, repr(summaries))
        audit = await self.db.audit_entries(limit=10)
        self.assertNotIn(secret, repr(audit))

        stored_bytes = b"".join(
            path.read_bytes()
            for path in self.root.glob("credentials.sqlite3*")
            if path.is_file()
        )
        self.assertNotIn(secret.encode(), stored_bytes)

        candidates = await self.manager.candidates("notes", "gemini")
        self.assertEqual(candidates[0].secret, secret)
        self.assertEqual(candidates[0].masked, "••••••••9876")
        self.assertNotIn(secret, repr(candidates[0]))

    async def test_settings_repr_never_discloses_keys_or_other_authentication_secrets(self):
        secrets = (
            "telegram-token-private",
            "telegram-hash-private",
            "speechmatics-key-private",
            "deepgram-key-private",
            "gemini-key-private",
            "stt-openai-key-private",
            "note-api-key-private",
            "master-encryption-key-private",
            "proxy-password-private",
            "custom-header-secret-private",
        )
        settings = make_settings(
            telegram_bot_token=secrets[0],
            telegram_api_hash=secrets[1],
            speechmatics_api_key=secrets[2],
            deepgram_api_key=secrets[3],
            gemini_api_key=secrets[4],
            stt_openai_api_key=secrets[5],
            note_api_key=secrets[6],
            provider_credentials_encryption_key=secrets[7],
            telegram_proxy=("socks5", "proxy.example", 1080, True, "operator", secrets[8]),
            note_api_extra_headers=(("x-private-token", secrets[9]),),
        )
        rendered = repr(settings)
        for secret in secrets:
            self.assertNotIn(secret, rendered)

    async def test_base_url_cannot_smuggle_credentials_into_plaintext_columns(self):
        with self.assertRaises(CredentialStoreError):
            await self.manager.add_credential(
                service="notes",
                provider="openai_compatible",
                label="unsafe",
                secret="url-secret",
                admin_id=700,
                base_url="https://example.test/v1?api_key=url-secret",
            )
        with self.assertRaises(CredentialStoreError):
            await self.manager.add_credential(
                service="notes",
                provider="openai_compatible",
                label="unsafe",
                secret="url-secret",
                admin_id=700,
                base_url="https://user:url-secret@example.test/v1",
            )
        self.assertEqual(await self.manager.list_summaries(), [])

    async def test_plaintext_metadata_rejects_full_keys_and_key_fragments(self):
        secret = "private-provider-secret-987654"
        for metadata in (
            {"label": f"display-{secret[:12]}"},
            {"label": "display", "model": secret[10:22]},
            {"label": "display", "base_url": f"https://api.example.test/v1/{secret[-15:]}"},
        ):
            with self.subTest(metadata=metadata):
                with self.assertRaises(CredentialStoreError):
                    await self.manager.add_credential(
                        service="notes",
                        provider="openai_compatible",
                        label=str(metadata["label"]),
                        secret=secret,
                        admin_id=700,
                        base_url=metadata.get("base_url"),
                        model=metadata.get("model"),
                    )
        self.assertEqual(await self.manager.list_summaries(), [])

    async def test_keyless_openai_compatible_endpoint_remains_supported_without_stored_keys(self):
        candidates = await self.manager.candidates(
            "stt", "openai_compatible", fallback_base_url="https://stt.example.test/v1"
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].secret, "")
        self.assertEqual(candidates[0].base_url, "https://stt.example.test/v1")

        await self.manager.add_credential(
            service="stt",
            provider="openai_compatible",
            label="configured but disabled",
            secret="configured-key",
            admin_id=700,
        )
        row = (await self.manager.list_summaries())[0]
        self.assertTrue(await self.manager.disable(int(row["id"]), admin_id=700))
        # A disabled/quarantined configured key must not be bypassed by quietly
        # making an unauthenticated request to the same endpoint.
        candidates = await self.manager.candidates(
            "stt", "openai_compatible", fallback_base_url="https://stt.example.test/v1"
        )
        self.assertEqual(candidates, [])

    async def test_disabled_note_key_is_not_bypassed_by_keyless_fallback(self):
        base_url = "https://notes.example.test/v1"
        candidates = await self.manager.candidates(
            "notes", "openai_compatible", fallback_base_url=base_url
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].secret, "")

        credential_id = await self.manager.add_credential(
            service="notes",
            provider="openai_compatible",
            label="configured note key",
            secret="notes-configured-key",
            admin_id=700,
        )
        self.assertTrue(await self.manager.disable(credential_id, admin_id=700))
        settings = make_settings(
            note_api_provider="openai_compatible",
            note_api_key=None,
            note_api_base_url=base_url,
        )
        with use_provider_credentials(self.manager):
            with self.assertRaises(StructuringError):
                await _structure_chunk("درس", settings, _Session())

    async def _run_stt_with_pool(self, fail_first: Exception):
        first, second = await self._add_stt_keys()
        seen: list[str | None] = []

        async def fake_attempt(_engine, _session, _audio, request_settings, **_kwargs):
            seen.append(request_settings.deepgram_api_key)
            if request_settings.deepgram_api_key == "dg-secret-primary-1234":
                raise fail_first
            return Transcript("deepgram", "سلام", 0.9)

        with tempfile.NamedTemporaryFile() as audio:
            audio.write(b"a")
            audio.flush()
            with patch("gamas_bot.stt.aiohttp.ClientSession", lambda **_kwargs: _Session()), patch(
                "gamas_bot.stt._attempt_with_retries", side_effect=fake_attempt
            ):
                transcript = await transcribe(Path(audio.name), self.settings, credentials=self.manager)
        return transcript, seen, first, second

    async def test_429_cools_down_the_first_key_and_rotates_to_the_next(self):
        transcript, seen, first, second = await self._run_stt_with_pool(
            STTTransientError("rate limited", retry_after=120, status=429)
        )
        self.assertEqual(transcript.text, "سلام")
        self.assertEqual(seen, ["dg-secret-primary-1234", "dg-secret-backup-5678"])
        summaries = {row["id"]: row for row in await self.manager.list_summaries()}
        self.assertEqual(summaries[first]["last_status_code"], 429)
        self.assertIsNotNone(summaries[first]["cooldown_until"])
        self.assertIsNone(summaries[first]["quarantined_at"])
        self.assertIsNotNone(summaries[second]["last_success_at"])
        until = datetime.fromisoformat(summaries[first]["cooldown_until"])
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(remaining, 115)
        self.assertLessEqual(remaining, 121)

    async def test_401_quarantines_the_first_key_and_rotates_to_the_next(self):
        transcript, seen, first, second = await self._run_stt_with_pool(
            STTAuthenticationError("unauthorized", status=401)
        )
        self.assertEqual(transcript.text, "سلام")
        self.assertEqual(seen, ["dg-secret-primary-1234", "dg-secret-backup-5678"])
        summaries = {row["id"]: row for row in await self.manager.list_summaries()}
        self.assertIsNotNone(summaries[first]["quarantined_at"])
        self.assertEqual(summaries[first]["last_status_code"], 401)
        self.assertIsNotNone(summaries[second]["last_success_at"])

    async def test_bad_request_does_not_rotate_credentials(self):
        first, second = await self._add_stt_keys()
        seen: list[str | None] = []

        async def fake_attempt(_engine, _session, _audio, request_settings, **_kwargs):
            seen.append(request_settings.deepgram_api_key)
            raise STTRequestError("bad media request", status=415)

        with tempfile.NamedTemporaryFile() as audio:
            audio.write(b"a")
            audio.flush()
            with patch("gamas_bot.stt.aiohttp.ClientSession", lambda **_kwargs: _Session()), patch(
                "gamas_bot.stt._attempt_with_retries", side_effect=fake_attempt
            ):
                with self.assertRaises(STTError):
                    await transcribe(Path(audio.name), self.settings, credentials=self.manager)
        self.assertEqual(seen, ["dg-secret-primary-1234"])
        summaries = {row["id"]: row for row in await self.manager.list_summaries()}
        self.assertEqual(summaries[first]["last_status_code"], 415)
        self.assertEqual(summaries[second]["last_used_at"], None)
        self.assertIsNone(summaries[first]["cooldown_until"])
        self.assertIsNone(summaries[first]["quarantined_at"])

    async def test_note_provider_429_cools_down_and_rotates_credentials(self):
        first = await self.manager.add_credential(
            service="notes",
            provider="gemini",
            label="primary note key",
            secret="gemini-primary-1111",
            admin_id=700,
            priority=1,
        )
        second = await self.manager.add_credential(
            service="notes",
            provider="gemini",
            label="backup note key",
            secret="gemini-backup-2222",
            admin_id=700,
            priority=2,
        )
        settings = make_settings(
            note_api_provider="gemini",
            gemini_api_key=None,
            note_api_key=None,
            provider_credentials_encryption_key=self.master_key,
        )
        seen: list[str | None] = []

        async def fake_once(_chunk, request_settings, _session, *_args, **_kwargs):
            seen.append(request_settings.effective_note_api_key)
            if request_settings.effective_note_api_key == "gemini-primary-1111":
                raise ProviderHTTPError(
                    "rate limited", status=429, retry_after_seconds=90
                )
            return "structured output"

        with use_provider_credentials(self.manager), patch(
            "gamas_bot.structuring._structure_chunk_once", side_effect=fake_once
        ):
            result = await _structure_chunk("source text", settings, _Session())
        self.assertEqual(result, "structured output")
        self.assertEqual(seen, ["gemini-primary-1111", "gemini-backup-2222"])
        summaries = {row["id"]: row for row in await self.manager.list_summaries()}
        self.assertEqual(summaries[first]["last_status_code"], 429)
        self.assertIsNotNone(summaries[first]["cooldown_until"])
        self.assertIsNotNone(summaries[second]["last_success_at"])

    async def test_all_keys_exhausted_returns_a_clean_secret_free_error(self):
        first, second = await self._add_stt_keys()
        # Both keys hit 429: they cool down and are excluded from rotation.
        for credential_id in (first, second):
            record = await self.manager.credential_for_test(credential_id)
            await self.manager.record_result(
                record, result="cooldown", status_code=429, retry_after_seconds=600
            )
        self.assertEqual(await self.manager.candidates("stt", "deepgram"), [])
        with tempfile.NamedTemporaryFile() as audio:
            audio.write(b"a")
            audio.flush()
            with self.assertRaises(STTError) as raised:
                await transcribe(Path(audio.name), self.settings, credentials=self.manager)
        message = str(raised.exception)
        self.assertIn("تنظیم نشده", message)
        self.assertNotIn("dg-secret", message)
        self.assertNotIn("1234", message)
        self.assertNotIn("5678", message)

    async def test_a_successful_request_restores_healthy_state(self):
        first, _second = await self._add_stt_keys()
        record = await self.manager.credential_for_test(first)
        await self.manager.record_result(
            record, result="cooldown", status_code=429, retry_after_seconds=600
        )
        summaries = {row["id"]: row for row in await self.manager.list_summaries()}
        self.assertIsNotNone(summaries[first]["cooldown_until"])
        await self.manager.record_result(record, result="success", status_code=200)
        summaries = {row["id"]: row for row in await self.manager.list_summaries()}
        self.assertIsNone(summaries[first]["cooldown_until"])
        self.assertIsNone(summaries[first]["quarantined_at"])
        self.assertIsNotNone(summaries[first]["last_success_at"])
        self.assertIsNotNone((await self.manager.candidates("stt", "deepgram"))[0].id)

    async def test_provider_secrets_never_reach_the_logs(self):
        import logging

        first, _second = await self._add_stt_keys()
        record = await self.manager.credential_for_test(first)
        with self.assertLogs("gamas_bot", level="DEBUG") as captured:
            await self.manager.record_result(
                record, result="cooldown", status_code=429, retry_after_seconds=60
            )
            await self.manager.record_result(
                record, result="quarantined", status_code=401
            )
            logger = logging.getLogger("gamas_bot.provider_credentials")
            logger.warning("credential status changed id=%s", first)
        rendered = "\n".join(entry.getMessage() for entry in captured.records)
        for secret in ("dg-secret-primary-1234", "dg-secret-backup-5678"):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret[-12:], rendered)

    async def test_provider_key_requires_environment_master_key_before_storage(self):
        no_key = ProviderCredentialManager(
            self.db,
            make_settings(provider_credentials_encryption_key=None),
        )
        with self.assertRaises(CredentialStoreError):
            await no_key.add_credential(
                service="notes",
                provider="gemini",
                label="no vault",
                secret="would-be-secret",
                admin_id=700,
            )
        self.assertEqual(await self.manager.list_summaries(), [])


if __name__ == "__main__":
    unittest.main()
