"""Gamas Speech Platform: policy, routing, quality gate and transcribe integration.

These tests never call a real provider. Native and legacy attempts are replaced
at the ``_attempt_with_retries`` boundary, and the database is a real SQLite
file, so budget reservations, usage rows and events are exercised end to end.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import aiohttp

from gamas_bot import stt
from gamas_bot.config import Settings
from gamas_bot.database import Database
from gamas_bot.provider_credentials import (
    PROVIDER_CHOICES,
    ProviderCredential,
    ProviderCredentialManager,
)
from gamas_bot.stt import STT_PROVIDERS, STTError, STTRequestError, Transcript, transcribe
from gamas_bot.stt_platform.adapters import NativeSTTAdapter, ProviderSTTError
from gamas_bot.stt_platform.models import Transcript as PlatformTranscript
from gamas_bot.stt_platform.policy import SttPolicy, evaluate_gate
from gamas_bot.stt_platform.quality import hard_quality_failures
from gamas_bot.stt_platform.registry import STT_PROVIDER_REGISTRY, STTProviderClass
from gamas_bot.stt_platform.router import (
    CandidateFacts,
    SttRequirements,
    evaluate_candidate,
    plan_route,
    resolve_route,
)
from support import make_settings

PERSIAN = "سلام این یک آزمایش برای درس امروز است و متن فارسی کاملاً روان است"


def _info(slug: str):
    return STT_PROVIDER_REGISTRY[slug]


def _req(**overrides) -> SttRequirements:
    base = {"persian": True, "file_bytes": 1000, "duration_seconds": 60.0}
    base.update(overrides)
    return SttRequirements(**base)


class PolicyTests(unittest.TestCase):
    def test_default_policy_blocks_unallowlisted_trial_provider(self):
        decision = evaluate_gate(_info("assemblyai"), SttPolicy())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "trial_not_allowed")

    def test_speechmatics_and_deepgram_pass_via_default_allowlist(self):
        for slug in ("speechmatics", "deepgram"):
            with self.subTest(provider=slug):
                decision = evaluate_gate(_info(slug), SttPolicy())
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reason, "trial_allowlisted")

    def test_global_trial_flag_unlocks_other_trial_providers(self):
        policy = SttPolicy(allow_trial_providers=True)
        self.assertTrue(evaluate_gate(_info("assemblyai"), policy).allowed)

    def test_free_class_is_always_allowed(self):
        self.assertTrue(evaluate_gate(_info("groq"), SttPolicy()).allowed)

    def test_paid_class_requires_paid_fallback_opt_in(self):
        self.assertEqual(evaluate_gate(_info("soniox"), SttPolicy()).reason, "paid_not_allowed")
        self.assertTrue(evaluate_gate(_info("soniox"), SttPolicy(allow_paid_fallback=True)).allowed)

    def test_region_restricted_and_unsupported_are_never_allowed(self):
        for free_type in (STTProviderClass.REGION_RESTRICTED.value, STTProviderClass.UNSUPPORTED.value):
            with self.subTest(free_type=free_type):
                info = replace(_info("groq"), free_type=free_type)
                self.assertFalse(evaluate_gate(info, SttPolicy(allow_paid_fallback=True)).allowed)

    def test_admin_paid_attestation_blocks_free_native_provider_in_free_only_mode(self):
        decision = evaluate_gate(_info("groq"), SttPolicy(), billing_state="paid")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "billing_attested_paid")

    def test_openai_compatible_stays_usable_as_operator_configured_legacy(self):
        decision = evaluate_gate(_info("openai_compatible"), SttPolicy())
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.tier, "legacy")

    def test_free_only_off_permits_trial_and_paid(self):
        policy = SttPolicy(free_only=False)
        self.assertTrue(evaluate_gate(_info("assemblyai"), policy).allowed)
        self.assertTrue(evaluate_gate(_info("soniox"), policy).allowed)

    def test_policy_reads_settings(self):
        settings = make_settings(
            stt_free_only=True,
            stt_allow_trial_providers=False,
            stt_trial_allowlist=("groq",),
            stt_allow_paid_fallback=True,
        )
        policy = SttPolicy.from_settings(settings)
        self.assertEqual(policy.trial_allowlist, frozenset({"groq"}))
        self.assertTrue(policy.allow_paid_fallback)


class RouterTests(unittest.TestCase):
    def test_default_route_is_the_legacy_chain_for_speechmatics_primary(self):
        settings = make_settings(stt_primary="speechmatics", stt_default_route="")
        self.assertEqual(
            resolve_route(settings), ("speechmatics", "deepgram", "openai_compatible")
        )

    def test_native_primary_keeps_legacy_fallbacks_without_a_key_error(self):
        settings = make_settings(stt_primary="gemini_transcribe", stt_default_route="")
        self.assertEqual(
            resolve_route(settings),
            ("gemini_transcribe", "speechmatics", "deepgram", "openai_compatible"),
        )

    def test_default_route_override_wins(self):
        settings = make_settings(stt_default_route="groq, deepgram,groq")
        self.assertEqual(resolve_route(settings), ("groq", "deepgram"))

    def test_persian_job_never_routes_to_a_non_persian_engine(self):
        facts = {name: CandidateFacts(has_credential=True) for name in ("gladia", "elevenlabs_scribe", "aws_transcribe")}
        for name in facts:
            with self.subTest(provider=name):
                decision = evaluate_candidate(
                    name, 0, _req(persian=True), SttPolicy(free_only=False), facts[name]
                )
                self.assertFalse(decision.eligible)
                self.assertIn("persian_unsupported", decision.reasons)

    def test_missing_key_is_denied_with_a_stable_reason(self):
        decision = evaluate_candidate("groq", 0, _req(), SttPolicy(), CandidateFacts(has_credential=False))
        self.assertIn("not_configured", decision.reasons)

    def test_admin_disabled_provider_is_denied(self):
        facts = CandidateFacts(has_credential=True, admin_enabled=False)
        decision = evaluate_candidate("groq", 0, _req(), SttPolicy(), facts)
        self.assertIn("admin_disabled", decision.reasons)

    def test_file_at_or_over_direct_upload_limit_is_denied(self):
        facts = CandidateFacts(has_credential=True, max_upload=1000)
        decision = evaluate_candidate("groq", 0, _req(file_bytes=1000), SttPolicy(), facts)
        self.assertIn("file_too_large", decision.reasons)

    def test_gemini_thirty_minute_limit_applies_with_diarization(self):
        facts = CandidateFacts(has_credential=True)
        plain = evaluate_candidate("gemini_transcribe", 0, _req(duration_seconds=2000), SttPolicy(), facts)
        diarized = evaluate_candidate(
            "gemini_transcribe", 0, _req(duration_seconds=2000, diarization=True), SttPolicy(), facts
        )
        self.assertTrue(plain.eligible)
        self.assertIn("duration_too_long", diarized.reasons)

    def test_unknown_duration_is_a_warning_not_a_denial(self):
        facts = CandidateFacts(has_credential=True)
        decision = evaluate_candidate("gemini_transcribe", 0, _req(duration_seconds=None), SttPolicy(), facts)
        self.assertTrue(decision.eligible)
        self.assertIn("duration_unknown", decision.warnings)

    def test_feature_flags_are_checked_for_native_providers_only(self):
        facts = CandidateFacts(has_credential=True)
        native = evaluate_candidate("groq", 0, _req(diarization=True), SttPolicy(), facts)
        legacy = evaluate_candidate("deepgram", 0, _req(diarization=True), SttPolicy(), facts)
        self.assertIn("feature_unsupported:diarization", native.reasons)
        self.assertTrue(legacy.eligible)

    def test_gemini_drops_vocabulary_when_diarization_is_requested(self):
        facts = CandidateFacts(has_credential=True)
        decision = evaluate_candidate(
            "gemini_transcribe", 0, _req(diarization=True, vocabulary_terms=5), SttPolicy(), facts
        )
        self.assertIn("vocabulary_dropped_gemini_constraint", decision.warnings)

    def test_paid_candidates_run_after_free_candidates(self):
        settings = make_settings(stt_default_route="soniox,groq", stt_allow_paid_fallback=True)
        facts = {
            "soniox": CandidateFacts(has_credential=True),
            "groq": CandidateFacts(has_credential=True),
        }
        with patch.dict(
            STT_PROVIDER_REGISTRY, {"soniox": replace(_info("soniox"), enabled=True)}
        ):
            plan = plan_route(settings, _req(persian=False), facts)
        self.assertEqual(plan.execution, ("groq", "soniox"))

    def test_advisory_score_never_reorders_providers(self):
        settings = make_settings(stt_default_route="groq,speechmatics")
        facts = {
            "groq": CandidateFacts(has_credential=True),
            "speechmatics": CandidateFacts(has_credential=True),
        }
        plan = plan_route(settings, _req(), facts)
        self.assertEqual(plan.execution, ("groq", "speechmatics"))

    def test_fallback_disabled_runs_only_the_first_eligible_provider(self):
        settings = make_settings(stt_default_route="groq,speechmatics", stt_fallback_enabled=False)
        facts = {"groq": CandidateFacts(has_credential=True), "speechmatics": CandidateFacts(has_credential=True)}
        self.assertEqual(plan_route(settings, _req(), facts).execution, ("groq",))

    def test_failover_cap_limits_attempts(self):
        settings = make_settings(
            stt_default_route="groq,speechmatics,deepgram", stt_max_provider_failovers=0
        )
        facts = {name: CandidateFacts(has_credential=True) for name in ("groq", "speechmatics", "deepgram")}
        self.assertEqual(plan_route(settings, _req(), facts).execution, ("groq",))

    def test_legacy_default_route_order_is_unchanged_end_to_end(self):
        settings = make_settings(stt_primary="deepgram", stt_default_route="")
        facts = {name: CandidateFacts(has_credential=True) for name in resolve_route(settings)}
        plan = plan_route(settings, _req(persian=True), facts)
        self.assertEqual(plan.execution, ("deepgram", "speechmatics", "openai_compatible"))


class QualityGateTests(unittest.TestCase):
    def test_short_latin_text_is_not_rejected_for_a_persian_job(self):
        from gamas_bot.stt_platform.quality import TranscriptQualityGate

        verdict = TranscriptQualityGate(make_settings(stt_language="fa")).evaluate(
            PlatformTranscript("groq", "hello world", None),
            expected_language="fa",
        )
        self.assertNotIn("language_mismatch", verdict.reasons)

    def test_long_latin_text_for_a_persian_job_is_a_hard_failure(self):
        from gamas_bot.stt_platform.quality import TranscriptQualityGate

        text = " ".join(["lecture about distributed systems"] * 30)
        verdict = TranscriptQualityGate(make_settings(stt_language="fa")).evaluate(
            PlatformTranscript("groq", text, None), expected_language="fa"
        )
        self.assertIn("language_mismatch", hard_quality_failures(verdict.reasons, text_length=len(text)))

    def test_code_switched_persian_lecture_passes(self):
        from gamas_bot.stt_platform.quality import TranscriptQualityGate

        text = (PERSIAN + " ") * 6 + "machine learning model training"
        verdict = TranscriptQualityGate(make_settings(stt_language="fa")).evaluate(
            PlatformTranscript("groq", text, None), expected_language="fa"
        )
        self.assertEqual(hard_quality_failures(verdict.reasons, text_length=len(text)), ())

    def test_error_marker_words_reject_only_short_texts(self):
        short = "error: invalid api key"
        self.assertEqual(
            hard_quality_failures(("embedded_provider_error:api key",), text_length=len(short)),
            ("embedded_provider_error:api key",),
        )
        long_lecture = "در این درس api key را توضیح می‌دهیم " + PERSIAN * 10
        self.assertEqual(
            hard_quality_failures(("embedded_provider_error:api key",), text_length=len(long_lecture)),
            (),
        )

    def test_low_confidence_and_repetition_are_never_hard_failures(self):
        self.assertEqual(
            hard_quality_failures(("low_confidence:0.100<0.65", "excessive_repetition:run=20"), text_length=500),
            (),
        )

    def test_legacy_transcript_keeps_only_comparable_confidence(self):
        comparable = PlatformTranscript(
            engine="groq", text="x", confidence=0.9, provider="groq",
            metadata={"confidence_comparable": True},
        )
        not_comparable = PlatformTranscript(engine="groq", text="x", confidence=0.9, provider="groq")
        self.assertEqual(stt._legacy_transcript(comparable).confidence, 0.9)
        self.assertIsNone(stt._legacy_transcript(not_comparable).confidence)


class NativeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_upload_sends_key_in_header_and_never_in_url(self):
        secret = "AIza-test-secret-value"
        settings = make_settings(
            stt_language="fa",
            stt_provider_api_keys=(("gemini_transcribe", secret),),
        )
        calls: list[tuple[str, dict]] = []

        class _Session:
            def post(self, url, **kwargs):
                calls.append((url, kwargs.get("headers") or {}))
                raise aiohttp.ClientError("stop after capture")

        with tempfile.NamedTemporaryFile(suffix=".mp3") as audio:
            audio.write(b"ID3" + b"\x00" * 64)
            audio.flush()
            with self.assertRaises(ProviderSTTError):
                await NativeSTTAdapter("gemini_transcribe").transcribe(
                    _Session(), Path(audio.name), settings
                )
        self.assertTrue(calls)
        url, headers = calls[0]
        self.assertNotIn("key=", url)
        self.assertNotIn(secret, url)
        self.assertEqual(headers.get("x-goog-api-key"), secret)

    async def test_google_cloud_sends_key_in_header_and_never_in_url(self):
        secret = "AIza-google-test-secret"
        settings = make_settings(
            stt_language="fa",
            stt_provider_api_keys=(("google_cloud_stt", secret),),
        )
        calls: list[tuple[str, dict]] = []

        class _Session:
            def request(self, method, url, **kwargs):
                calls.append((url, kwargs.get("headers") or {}))
                raise aiohttp.ClientError("stop after capture")

        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            audio.write(b"RIFF" + b"\x00" * 64)
            audio.flush()
            with self.assertRaises(ProviderSTTError):
                await NativeSTTAdapter("google_cloud_stt").transcribe(
                    _Session(), Path(audio.name), settings
                )
        self.assertTrue(calls)
        url, headers = calls[0]
        self.assertNotIn("key=", url)
        self.assertNotIn(secret, url)
        self.assertEqual(headers.get("x-goog-api-key"), secret)

    def test_native_credentials_are_applied_to_request_settings(self):
        credential = ProviderCredential(
            id=1,
            service="stt",
            provider="groq",
            label="g1",
            secret="gsk-test-secret",
            model="whisper-large-v3",
            base_url="https://api.groq.com/openai/v1/",
        )
        applied = ProviderCredentialManager.apply_to_settings(make_settings(), credential)
        self.assertEqual(applied.stt_api_key("groq"), "gsk-test-secret")
        self.assertEqual(applied.stt_model("groq"), "whisper-large-v3")
        self.assertEqual(applied.stt_base_url("groq"), "https://api.groq.com/openai/v1")
        # Legacy providers keep their own fields.
        self.assertEqual(applied.deepgram_api_key, make_settings().deepgram_api_key)

    def test_credential_choices_cover_every_registry_provider(self):
        self.assertEqual(PROVIDER_CHOICES["stt"], frozenset(STT_PROVIDER_REGISTRY))
        for slug in ("speechmatics", "deepgram", "openai_compatible", "groq", "gemini_transcribe"):
            self.assertIn(slug, STT_PROVIDERS)

    def test_language_mapping_is_not_sent_verbatim_for_auto(self):
        self.assertIsNone(stt._language_error("groq", make_settings(stt_language="auto")))
        self.assertEqual(
            stt._language_error("ibm_watson_stt", make_settings(stt_language="auto")),
            "language_unsupported",
        )
        self.assertEqual(
            stt._language_error("speechmatics", make_settings(stt_language="ar_en")),
            None,
        )
        self.assertEqual(
            stt._language_error("groq", make_settings(stt_language="ar_en")),
            "language_unsupported",
        )


class _FakeCredentials:
    """Minimal credential manager: real DB, fixed candidates, no key store."""

    def __init__(self, db, pools):
        self.db = db
        self._pools = pools

    async def candidates(self, service, provider, **_kwargs):
        return list(self._pools.get(provider, []))

    @staticmethod
    def apply_to_settings(settings, credential):
        return ProviderCredentialManager.apply_to_settings(settings, credential)

    async def record_result(self, credential, **_kwargs):
        return None


def _wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


class TranscribeRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "gamas.sqlite3")
        await self.db.open()
        self.audio = _wav(self.root / "clip.wav", 1.0)
        self.groq = ProviderCredential(
            id=None, service="stt", provider="groq", label="g", secret="gsk-secret-value-1234"
        )

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    def _settings(self, **overrides) -> Settings:
        base = {
            "stt_primary": "groq",
            "stt_default_route": "groq",
            "stt_fallback_enabled": True,
            "stt_language": "fa",
            "speechmatics_api_key": None,
            "deepgram_api_key": None,
            "stt_provider_api_keys": (),
        }
        base.update(overrides)
        return make_settings(**base)

    async def _run(self, fake, *, settings=None, pools=None, submission_id=None):
        creds = _FakeCredentials(self.db, pools if pools is not None else {"groq": [self.groq]})
        with patch("gamas_bot.stt._attempt_with_retries", side_effect=fake) as attempt:
            result = await transcribe(
                self.audio, settings or self._settings(), credentials=creds, submission_id=submission_id
            )
        return result, attempt

    async def test_primary_native_provider_runs_without_key_error(self):
        settings = self._settings(
            stt_primary="gemini_transcribe",
            stt_default_route="",
            stt_provider_api_keys=(("gemini_transcribe", "AIza-test"),),
        )

        async def fake(engine, _session, _audio, _settings, **_kwargs):
            return Transcript(engine, PERSIAN, None)

        with patch("gamas_bot.stt._attempt_with_retries", side_effect=fake):
            result = await transcribe(self.audio, settings)
        self.assertEqual(result.engine, "gemini_transcribe")

    async def test_no_keys_still_reports_the_configuration_error(self):
        async def fake(engine, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("no provider should be attempted")

        with patch("gamas_bot.stt._attempt_with_retries", side_effect=fake):
            with self.assertRaises(STTError) as ctx:
                await transcribe(self.audio, self._settings(stt_default_route="", stt_primary="groq"))
        self.assertIn("هیچ کلید", str(ctx.exception))

    async def test_persian_only_routes_to_gladia_is_refused_with_reason(self):
        async def fake(engine, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("gladia must not receive Persian audio")

        pools = {"gladia": [self.groq]}
        settings = self._settings(stt_default_route="gladia")
        with self.assertRaises(STTError) as ctx:
            await self._run(fake, settings=settings, pools=pools)
        self.assertIn("persian_unsupported", str(ctx.exception))

    async def test_admin_disabled_provider_is_not_attempted(self):
        await self.db.stt_provider_settings_upsert("groq", enabled=False, admin_id=77)

        async def fake(engine, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("disabled provider attempted")

        with self.assertRaises(STTError) as ctx:
            await self._run(fake)
        self.assertIn("admin_disabled", str(ctx.exception))

    async def test_paid_attestation_blocks_free_provider(self):
        await self.db.stt_provider_settings_upsert("groq", billing_state="paid", admin_id=77)

        async def fake(engine, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("billing-attested paid account attempted")

        with self.assertRaises(STTError) as ctx:
            await self._run(fake)
        self.assertIn("billing_attested_paid", str(ctx.exception))

    async def test_exhausted_budget_denies_without_calling_the_provider(self):
        await self.db.stt_quota_set_budget(
            "groq", "acct-a", "audio_seconds_day", limit=100, remaining=0, reset_at=None, admin_id=77
        )

        async def fake(engine, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("exhausted budget still called the provider")

        with self.assertRaises(STTError) as ctx:
            await self._run(fake)
        self.assertIn("quota budget unavailable", str(ctx.exception))

    async def test_successful_call_commits_budget_and_records_usage(self):
        await self.db.stt_quota_set_budget(
            "groq", "acct-a", "audio_seconds_day", limit=100, remaining=100, reset_at=None, admin_id=77
        )

        async def fake(engine, *_args, **_kwargs):
            return Transcript(engine, PERSIAN, None)

        result, _attempt = await self._run(fake)
        self.assertEqual(result.engine, "groq")
        budget = await self.db.stt_quota_budget_get("groq", "acct-a", "audio_seconds_day")
        self.assertAlmostEqual(float(budget["remaining"]), 99.0, places=3)
        usage = await self.db.stt_usage_recent(provider="groq")
        self.assertTrue(any(row["result"] == "success" for row in usage))

    async def test_failed_call_releases_the_reservation(self):
        await self.db.stt_quota_set_budget(
            "groq", "acct-a", "audio_seconds_day", limit=100, remaining=100, reset_at=None, admin_id=77
        )

        async def fake(engine, *_args, **_kwargs):
            raise STTRequestError("bad request", status=400)

        with self.assertRaises(STTError):
            await self._run(fake)
        budget = await self.db.stt_quota_budget_get("groq", "acct-a", "audio_seconds_day")
        self.assertAlmostEqual(float(budget["remaining"]), 100.0, places=3)
        usage = await self.db.stt_usage_recent(provider="groq", failures_only=True)
        self.assertTrue(usage)

    async def test_quality_rejection_falls_back_to_the_next_provider(self):
        async def fake(engine, *_args, **_kwargs):
            if engine == "groq":
                # Short error-shaped payload leaked into the transcript text.
                return Transcript("groq", "error: invalid api key for this request", None)
            return Transcript("speechmatics", PERSIAN, None)

        settings = self._settings(stt_default_route="groq,speechmatics", speechmatics_api_key="sm-key")
        sm_key = ProviderCredential(
            id=None, service="stt", provider="speechmatics", label="sm", secret="sm-key"
        )
        pools = {"groq": [self.groq], "speechmatics": [sm_key]}
        result, attempt = await self._run(fake, settings=settings, pools=pools)
        self.assertEqual(result.engine, "speechmatics")
        self.assertEqual(attempt.call_count, 2)
        events = await self.db.stt_events_list(limit=50)
        self.assertTrue(any(row["event"] == "stt_quality_rejected" for row in events))

    async def test_provider_secret_never_reaches_events_or_usage(self):
        async def fake(engine, *_args, **_kwargs):
            raise STTRequestError("bad request", status=400)

        with self.assertRaises(STTError):
            await self._run(fake)
        dump = json.dumps(
            {
                "events": await self.db.stt_events_list(limit=100),
                "usage": await self.db.stt_usage_recent(limit=100),
            },
            default=str,
        )
        self.assertNotIn("gsk-secret-value-1234", dump)

    async def test_legacy_provider_remains_available_when_primary_is_native_and_absent(self):
        settings = self._settings(stt_default_route="", stt_primary="groq", speechmatics_api_key="sm-key")

        async def fake(engine, *_args, **_kwargs):
            return Transcript(engine, PERSIAN, None)

        result, _attempt = await self._run(fake, settings=settings, pools={"speechmatics": [None]})
        self.assertEqual(result.engine, "speechmatics")


class TrialAllowlistConfigTests(unittest.TestCase):
    def test_allowlist_defaults_to_speechmatics_and_deepgram(self):
        self.assertEqual(make_settings().stt_trial_allowlist, ("speechmatics", "deepgram"))

    def test_allowlist_is_parsed_from_environment(self):
        env = {"STT_TRIAL_ALLOWLIST": "Groq, speechmatics"}
        with patch.dict(os.environ, env, clear=False):
            settings = _load_settings_for_test()
        self.assertEqual(settings.stt_trial_allowlist, ("groq", "speechmatics"))

    def test_unknown_allowlist_provider_is_rejected(self):
        with patch.dict(os.environ, {"STT_TRIAL_ALLOWLIST": "not-a-provider"}, clear=False):
            with self.assertRaises(ValueError):
                _load_settings_for_test()


def _load_settings_for_test() -> Settings:
    return Settings.from_env(env_file=Path("/nonexistent/gamas-test.env"))


if __name__ == "__main__":
    unittest.main()
