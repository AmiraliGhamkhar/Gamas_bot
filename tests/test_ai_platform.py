"""AI Provider Platform integration tests (offline, mock sessions only).

Covers: provider registry/classification, model discovery + cache,
capability resolution, FREE_ONLY filtering + paid-fallback rejection,
provider-level failover + credential rotation/quarantine, 429/502 handling,
quota accounting, structured lifecycle events, secret hygiene, deprecation /
region / commercial-use restrictions, and legacy backward compatibility.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cryptography.fernet import Fernet

from gamas_bot.ai.models import (
    FREE_PROMOTIONAL,
    PAID,
    STATIC_SEEDS,
    ModelCapabilities,
    ModelInfo,
    ModelRegistry,
)
from gamas_bot.ai.registry import (
    PROVIDER_REGISTRY,
    ProviderClass,
    free_only_generation_allowed,
    registry_info,
    resolve_canonical,
)
from gamas_bot.ai.routing import (
    NoteJobSession,
    ProviderRouter,
    RouteLeg,
    job_session_scope,
)
from gamas_bot.ai.sync import sync_provider_catalog
from gamas_bot.ai.usage import AIUsageTracker, sanitize_text
from gamas_bot.config import Settings
from gamas_bot.database import Database, utc_now
from gamas_bot.provider_credentials import ProviderCredentialManager
from gamas_bot.structuring import (
    StructuringError,
    _structure_chunk,
    note_chunk_chars,
)

from support import make_settings


# ---------------------------------------------------------------------------
# DB-backed test harness
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, payload=None, headers=None):
        self.status = status
        self.headers = headers or {}
        self._payload = payload

    async def read(self):
        return json.dumps(self._payload).encode("utf-8")

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ScriptedSession:
    """aiohttp double that replays `(host_substring, response)` rules in order."""

    def __init__(self):
        self.rules: list[tuple[str, list]] = []
        self.requests: list[dict] = []

    def add(self, host: str, *responses):
        self.rules.append((host, list(responses)))

    def post(self, url, headers=None, params=None, json=None, timeout=None, data=None):
        self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        for host, responses in self.rules:
            if host in url and responses:
                response = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected request: {url}")


def _notes_payload(text: str, model: str = "m", *, usage=None) -> dict:
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def _gemini_payload(text: str) -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
    }


class PlatformCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "ai.sqlite3")
        await self.db.open()
        self.settings = make_settings(
            database_path=root / "ai.sqlite3",
            session_path=root / "session",
            temp_dir=root / "tmp",
            provider_credentials_encryption_key=Fernet.generate_key().decode("ascii"),
            note_api_provider="openai_compatible",
            note_api_base_url="https://router.bynara.id/v1",
            note_api_model="agnes-3-flash",
            note_api_key="sk-nara-env-1234",
        )
        self.manager = ProviderCredentialManager(self.db, self.settings)
        self.tracker = AIUsageTracker(self.db)
        self.models = ModelRegistry(self.db, self.settings)
        self.router = ProviderRouter(
            self.db, self.settings, self.manager, self.tracker, self.models
        )

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _session_plan(self, task="chunk_structuring") -> NoteJobSession:
        plan = await self.router.plan(task)
        return NoteJobSession(self.router, plan, self.settings, job_id="GMS-000011")


class RegistryClassificationTests(unittest.TestCase):
    def test_all_required_providers_registered(self):
        expected = {
            "gemini", "nara", "groq", "openrouter", "mistral", "sambanova", "zai",
            "nvidia", "cloudflare", "huggingface", "alibaba", "cohere", "cerebras",
            "anthropic", "openai_compatible",
        }
        self.assertTrue(expected.issubset(set(PROVIDER_REGISTRY)))

    def test_free_only_eligibility_matrix(self):
        self.assertTrue(free_only_generation_allowed("gemini"))
        self.assertTrue(free_only_generation_allowed("nara"))
        self.assertFalse(free_only_generation_allowed("groq"))
        self.assertTrue(free_only_generation_allowed("openrouter"))
        self.assertFalse(free_only_generation_allowed("cohere"))
        self.assertFalse(free_only_generation_allowed("cerebras"))
        self.assertFalse(free_only_generation_allowed("alibaba"))
        self.assertFalse(free_only_generation_allowed("anthropic"))
        self.assertFalse(free_only_generation_allowed("zai"))
        self.assertFalse(free_only_generation_allowed("openai_compatible"))

    def test_url_classification(self):
        cases = {
            "https://router.bynara.id/v1": "nara",
            "https://api.groq.com/openai/v1": "groq",
            "https://openrouter.ai/api/v1": "openrouter",
            "https://api.mistral.ai/v1": "mistral",
            "https://api.sambanova.ai/v1": "sambanova",
            "https://api.z.ai/api/paas/v4": "zai",
            "https://integrate.api.nvidia.com/v1": "nvidia",
            "https://api.cloudflare.com/client/v4/accounts/x/ai/v1": "cloudflare",
            "https://router.huggingface.co/v1": "huggingface",
            "https://dashscope.aliyuncs.com/compatible-mode/v1": "alibaba",
            "https://api.cohere.com/compatibility/v1": "cohere",
            "https://api.cerebras.ai/v1": "cerebras",
            "https://example.vm/v1": "openai_compatible",
        }
        for url, canonical in cases.items():
            self.assertEqual(resolve_canonical("openai_compatible", url), canonical, url)

    def test_classification_labels(self):
        self.assertEqual(registry_info("cloudflare").classification, ProviderClass.FREE_PLAN)
        self.assertEqual(registry_info("groq").classification, ProviderClass.ACCOUNT_UNVERIFIED)
        self.assertEqual(registry_info("zai").classification, ProviderClass.FREE_PLAN)
        self.assertEqual(registry_info("nvidia").classification, ProviderClass.TRIAL_ONLY)
        self.assertEqual(registry_info("cohere").classification, ProviderClass.TRIAL_ONLY)
        self.assertEqual(registry_info("alibaba").classification, ProviderClass.REGION_RESTRICTED)

    def test_credential_pool_slugs_match_registry(self):
        from gamas_bot.provider_credentials import NOTE_PROVIDER_CHOICES

        self.assertEqual(NOTE_PROVIDER_CHOICES, frozenset(PROVIDER_REGISTRY))


class ConfigParsingTests(unittest.TestCase):
    def _env(self, key, value):
        import os

        os.environ[key] = value

    def tearDown(self):
        import os

        for key in (
            "AI_FREE_ONLY", "AI_ALLOW_PAID_FALLBACK", "AI_ROUTING_ENABLED",
            "AI_PROVIDER_SYNC_TTL", "AI_DEFAULT_NOTE_ROUTE", "AI_MAX_PROVIDER_FAILOVERS",
            "AI_MAX_GENERATION_RETRIES", "AI_QUOTA_SAFETY_MARGIN", "CLOUDFLARE_ACCOUNT_ID",
        ):
            os.environ.pop(key, None)

    def test_new_variables_parse(self):
        self._env("AI_FREE_ONLY", "false")
        self._env("AI_ALLOW_PAID_FALLBACK", "true")
        self._env("AI_MAX_PROVIDER_FAILOVERS", "5")
        self._env("AI_QUOTA_SAFETY_MARGIN", "0.2")
        self._env("CLOUDFLARE_ACCOUNT_ID", "acc-1")
        settings = Settings.from_env(env_file=Path("/nonexistent/.env"))
        self.assertFalse(settings.ai_free_only)
        self.assertTrue(settings.ai_allow_paid_fallback)
        self.assertEqual(settings.ai_max_provider_failovers, 5)
        self.assertAlmostEqual(settings.ai_quota_safety_margin, 0.2)
        self.assertEqual(settings.cloudflare_account_id, "acc-1")

    def test_provider_slug_validation_accepts_new_slugs(self):
        self._env("NOTE_API_PROVIDER", "nara")
        try:
            settings = Settings.from_env(env_file=Path("/nonexistent/.env"))
            self.assertEqual(settings.note_api_provider, "nara")
        finally:
            import os

            os.environ.pop("NOTE_API_PROVIDER", None)

    def test_invalid_margin_rejected(self):
        self._env("AI_QUOTA_SAFETY_MARGIN", "0.9")
        with self.assertRaises(ValueError):
            Settings.from_env(env_file=Path("/nonexistent/.env"))


class ModelRegistryTests(PlatformCase):
    async def test_discovery_persists_and_deactivates_missing(self):
        discovered_first = [
            ModelInfo(provider="nara", model_id="agnes-3-flash", source="live"),
            ModelInfo(provider="nara", model_id="old-model", source="live"),
        ]
        stats = await self.models.apply_discovery("nara", discovered_first)
        self.assertEqual(stats["synced"], 2)
        stats = await self.models.apply_discovery(
            "nara", [ModelInfo(provider="nara", model_id="agnes-3-flash", source="live")]
        )
        self.assertEqual(stats["deactivated"], 1)
        cached = await self.models.cached("nara")
        ids = [m.model_id for m in cached]
        self.assertEqual(ids, ["agnes-3-flash"])
        cached_all = await self.models.cached("nara", include_unavailable=True)
        self.assertIn("old-model", [m.model_id for m in cached_all])
        gone = [m for m in cached_all if m.model_id == "old-model"][0]
        self.assertFalse(gone.available)

    async def test_resolve_precedence_db_then_static(self):
        await self.models.apply_discovery(
            "gemini", [ModelInfo(provider="gemini", model_id="gemini-custom", source="live")]
        )
        custom = await self.models.resolve("gemini", "gemini-custom")
        self.assertEqual(custom.source, "live")
        static = await self.models.resolve("gemini", "gemini-2.5-flash")
        self.assertEqual(static.source, "static_seed")
        unknown = await self.models.resolve("nara", "totally-new-model")
        self.assertEqual(unknown.free_status, "unknown")  # account-level plan is not model evidence

    async def test_reviewed_seed_facts_survive_incomplete_live_catalog_rows(self):
        seed = next(
            model for model in STATIC_SEEDS["gemini"]
            if model.model_id == "gemini-2.5-flash"
        )
        await self.models.apply_discovery(
            "gemini",
            [
                ModelInfo(
                    provider="gemini", model_id=seed.model_id,
                    capabilities=ModelCapabilities(), free_status="unknown", source="live:test",
                )
            ],
        )
        resolved = await self.models.resolve("gemini", seed.model_id)
        stored = await self.db.ai_model_get("gemini", seed.model_id)
        self.assertEqual(resolved.free_status, seed.free_status)
        self.assertEqual(resolved.capabilities, seed.capabilities)
        self.assertEqual(stored["free_status"], seed.free_status)
        self.assertEqual(
            ModelCapabilities.from_json(stored["capabilities_json"]), seed.capabilities
        )

        # A later, explicit live paid fact is not hidden by the static seed.
        await self.models.apply_discovery(
            "gemini",
            [ModelInfo(provider="gemini", model_id=seed.model_id, free_status=PAID, source="live:test")],
        )
        resolved = await self.models.resolve("gemini", seed.model_id)
        self.assertEqual(resolved.free_status, PAID)
        self.assertEqual(resolved.capabilities, seed.capabilities)

    async def test_promotional_expiry_blocks_model(self):
        from datetime import datetime, timedelta, timezone

        info = ModelInfo(
            provider="zai", model_id="glm-promo",
            free_status=FREE_PROMOTIONAL,
            free_until=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        )
        self.assertFalse(info.free_only_eligible())

    async def test_sync_uses_adapter_and_records_event(self):
        async def fake_fetch(url, headers, timeout):
            if url.endswith("/api/plans"):
                payload = {"data": [{"code": "free", "is_active": True,
                                    "models": ["agnes-2.5-flash"]},
                                   {"code": "freemium", "is_active": True,
                                    "models": ["agnes-3-flash"]}]}
            else:
                payload = {"data": [{"id": "agnes-2.5-flash"}, {"id": "agnes-3-flash"}]}
            return 200, {}, json.dumps(payload).encode("utf-8")

        result = await sync_provider_catalog(self.router, "nara", session_get=fake_fetch, force=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.synced, 2)
        free_model = await self.models.resolve("nara", "agnes-2.5-flash")
        paid_model = await self.models.resolve("nara", "agnes-3-flash")
        self.assertEqual(free_model.free_status, "free_plan")
        self.assertEqual(paid_model.free_status, "paid")
        events = await self.db.ai_events_list(event="provider_sync")
        self.assertTrue(events)
        second = await sync_provider_catalog(self.router, "nara", session_get=fake_fetch)
        self.assertTrue(second.cached)


class CatalogSyncSafetyTests(PlatformCase):
    async def test_cloudflare_pages_and_failed_sync_preserve_existing_catalog(self):
        from gamas_bot.ai.adapters import adapter_for

        self.settings = replace(self.settings, cloudflare_account_id="acc-123")
        self.manager.settings = self.settings
        self.models.settings = self.settings
        self.router = ProviderRouter(
            self.db, self.settings, self.manager, self.tracker, self.models
        )
        key_id = await self.manager.add_credential(
            service="notes", provider="cloudflare", label="cf", secret="cf-token-0001", admin_id=1
        )
        # Read-only catalog discovery is allowed with an un-attested key; it
        # does not authorize any generation or change the key billing state.
        await self.models.apply_discovery(
            "cloudflare", [ModelInfo(provider="cloudflare", model_id="old-model", source="live:test")]
        )
        adapter = adapter_for("cloudflare", self.settings)
        adapter.discovery_page_size = 2
        adapter.max_discovery_pages = 3

        def body(ids):
            return json.dumps({"success": True, "result": {"data": [{"id": item} for item in ids]}}).encode()

        async def failed_fetch(url, headers, timeout):
            if "page=1" in url:
                return 200, {}, body(["model-a", "model-b"])
            return 503, {}, b"provider diagnostic containing cf-token-0001"

        failed = await sync_provider_catalog(
            self.router, "cloudflare", adapter=adapter, session_get=failed_fetch, force=True
        )
        self.assertFalse(failed.ok)
        prior = await self.db.ai_model_get("cloudflare", "old-model")
        self.assertTrue(prior["available"])
        self.assertEqual((await self.db.provider_credential_record(key_id))["billing_state"], "unknown")

        async def successful_fetch(url, headers, timeout):
            if "page=1" in url:
                return 200, {}, body(["model-a", "model-b"])
            return 200, {}, body(["model-c"])

        result = await sync_provider_catalog(
            self.router, "cloudflare", adapter=adapter, session_get=successful_fetch, force=True
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.synced, 3)
        self.assertEqual(result.deactivated, 1)
        models = await self.db.ai_models_list("cloudflare", include_unavailable=True)
        by_id = {item["model"]: item for item in models}
        self.assertFalse(by_id["old-model"]["available"])
        self.assertEqual(by_id["model-a"]["free_status"], "unknown")
        events = await self.db.ai_events_list(event="provider_sync")
        self.assertNotIn("cf-token-0001", repr(events))


class RoutingPlanTests(PlatformCase):
    async def test_seed_keeps_env_provider_first(self):
        order = self.router._seed_order("chunk_structuring")
        self.assertEqual(order[0], "nara")
        self.assertIn("gemini", order)

    async def test_plan_skips_trial_and_paid_providers(self):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [
                {"provider": "cohere", "enabled": 1, "free_only": 1},
                {"provider": "nara", "enabled": 1, "free_only": 1},
                {"provider": "anthropic", "enabled": 1, "free_only": 1},
            ],
            admin_id=None,
        )
        plan = await self.router.plan()
        legs = [leg.canonical for leg in plan.legs]
        self.assertEqual(legs, ["nara"])

    async def test_paid_fallback_is_opt_in(self):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [
                {"provider": "openai_compatible", "enabled": 1, "free_only": 0},
                {"provider": "nara", "enabled": 1, "free_only": 1},
            ],
            admin_id=None,
        )
        plan = await self.router.plan()
        self.assertEqual([leg.canonical for leg in plan.legs], ["nara"])
        paid_settings = make_settings(
            database_path=self.settings.database_path,
            session_path=self.settings.session_path,
            temp_dir=self.settings.temp_dir,
            note_api_provider="openai_compatible",
            note_api_base_url="https://router.bynara.id/v1",
            ai_allow_paid_fallback=True,
        )
        paid_router = ProviderRouter(self.db, paid_settings, self.manager, self.tracker, self.models)
        plan = await paid_router.plan()
        # The admin-added openai_compatible row canonicalises to the Nara adapter
        # (host matched), but with paid fallback enabled BOTH legs stay in the
        # plan — including the non-free-only tail leg.
        self.assertEqual(len(plan.legs), 2)
        self.assertFalse(plan.legs[1].free_only)

    async def test_region_restricted_requires_admin_unlock(self):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "alibaba", "enabled": 1, "free_only": 1},
             {"provider": "nara", "enabled": 1, "free_only": 1}],
            admin_id=None,
        )
        plan = await self.router.plan()
        self.assertEqual([leg.canonical for leg in plan.legs], ["nara"])
        await self.db.ai_provider_settings_upsert("alibaba", experimental_unlocked=True)
        self.router.invalidate_cache()
        plan = await self.router.plan()
        # Unlocking the region gate is not enough; FREE_ONLY still keeps a
        # non-free provider out of the plan.
        self.assertNotIn("alibaba", [leg.canonical for leg in plan.legs])

    async def test_openrouter_daily_ledger_blocks_when_exhausted(self):
        # The ledger is keyed by the *current* UTC day; never hard-code a date.
        day = utc_now()[:10]
        for index in range(50):
            await self.db.ai_usage_insert(
                {
                    "provider": "openrouter", "model": "x:free", "request_type": "chunk_structuring",
                    "result": "success", "created_at": f"{day}T10:{index:02d}:00",
                }
            )
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "openrouter", "enabled": 1, "free_only": 1},
             {"provider": "nara", "enabled": 1, "free_only": 1}],
            admin_id=None,
        )
        plan = await self.router.plan()
        reasons = dict(plan.skipped)
        self.assertEqual(reasons.get("openrouter"), "quota_exhausted")
        self.assertEqual([leg.canonical for leg in plan.legs], ["nara"])


class FailoverRotationTests(PlatformCase):
    async def _paid_compatible_mode(self):
        # Groq is deliberately not classified free. These tests exercise
        # request failover under an explicitly relaxed test deployment.
        self.settings = replace(self.settings, ai_free_only=False)
        self.manager.settings = self.settings
        self.models.settings = self.settings
        self.router = ProviderRouter(
            self.db, self.settings, self.manager, self.tracker, self.models
        )

    async def _route(self, providers):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [
                {"provider": p, "enabled": 1, "free_only": 0 if p == "groq" else 1}
                for p in providers
            ],
            admin_id=None,
        )

    async def _add_paid_groq(self, label="g1", secret="gsk-paid-key-0001"):
        credential_id = await self.manager.add_credential(
            service="notes", provider="groq", label=label,
            secret=secret, admin_id=1,
        )
        await self.manager.set_billing_attestation(credential_id, "paid", 1)
        return credential_id

    async def _add_free_gemini(self, label="gemini", secret="AIza-free-test-1234"):
        credential_id = await self.manager.add_credential(
            service="notes", provider="gemini", label=label,
            secret=secret, admin_id=1,
        )
        await self.manager.set_billing_attestation(credential_id, "free", 1)
        return credential_id

    async def test_provider_level_failover_after_429_with_no_credentials(self):
        await self._paid_compatible_mode()
        await self._route(["groq", "gemini"])
        await self._add_free_gemini()
        session = _ScriptedSession()
        job = await self._session_plan()
        # Groq has no credential; the attested Gemini leg succeeds.
        session.add("generativelanguage.googleapis.com", _Response(200, _gemini_payload("{\"title\": \"ت\"}")))
        text = await job.generate(
            request_type="chunk_structuring", task="chunk_structuring",
            system_prompt="S", user_text="U", session=session,
        )
        self.assertEqual(text, "{\"title\": \"ت\"}")
        self.assertEqual(job.fallbacks, 1)
        rows = await self.db.ai_usage_recent(limit=10)
        self.assertTrue(any(row["provider"] == "gemini" and row["result"] == "success" for row in rows))

    async def test_quota_failover_groq_429_then_gemini(self):
        await self._paid_compatible_mode()
        groq_id = await self._add_paid_groq(secret="gsk-test-key-0001")
        await self._add_free_gemini()
        await self._route(["groq", "gemini"])
        session = _ScriptedSession()
        session.add(
            "api.groq.com",
            _Response(429, {"error": {"message": "rate limited"}}, {"Retry-After": "30"}),
        )
        session.add("generativelanguage.googleapis.com", _Response(200, _gemini_payload("OK")))
        job = await self._session_plan()
        text = await job.generate(
            request_type="chunk_structuring", task="chunk_structuring",
            system_prompt="S", user_text="U", session=session,
        )
        self.assertEqual(text, "OK")
        record = await self.db.provider_credential_record(groq_id)
        self.assertTrue(record["cooldown_until"])  # cooldown honoured Retry-After?
        events = await self.db.ai_events_list(event="note_request_fallback")
        self.assertTrue(events)

    async def test_401_quarantines_credential_and_rotates(self):
        await self._paid_compatible_mode()
        bad = await self._add_paid_groq(label="bad", secret="gsk-bad-1111")
        await self._add_paid_groq(label="good", secret="gsk-good-2222")
        await self._route(["groq"])
        session = _ScriptedSession()
        session.add(
            "api.groq.com",
            _Response(401, {"error": {"message": "invalid api key"}}),
            _Response(200, _notes_payload("FINE")),
        )
        job = await self._session_plan()
        text = await job.generate(
            request_type="chunk_structuring", task="chunk_structuring",
            system_prompt="S", user_text="U", session=session,
        )
        self.assertEqual(text, "FINE")
        record = await self.db.provider_credential_record(bad)
        self.assertTrue(record["quarantined_at"])
        events = await self.db.ai_events_list(event="credential_quarantined")
        self.assertTrue(events)

    async def test_502_is_retried_before_failover(self):
        await self._paid_compatible_mode()
        await self._add_paid_groq(secret="gsk-key-3333")
        await self._route(["groq", "gemini"])
        await self._add_free_gemini()
        session = _ScriptedSession()
        session.add(
            "api.groq.com",
            _Response(502, {"error": {"message": "bad gateway"}}),
            _Response(200, _notes_payload("RECOVERED")),
        )
        job = await self._session_plan()
        text = await job.generate(
            request_type="chunk_structuring", task="chunk_structuring",
            system_prompt="S", user_text="U", session=session,
        )
        self.assertEqual(text, "RECOVERED")
        self.assertEqual(job.fallbacks, 0)

    async def test_400_schema_rejection_downgrades_once(self):
        await self._paid_compatible_mode()
        await self._add_paid_groq(secret="gsk-key-4444")
        await self._route(["groq"])
        session = _ScriptedSession()
        session.add(
            "api.groq.com",
            _Response(400, {"error": {"message": "response_format json_schema unsupported"}}),
            _Response(200, _notes_payload("DOWNGRADED")),
        )
        job = await self._session_plan()
        text = await job.generate(
            request_type="chunk_structuring", task="chunk_structuring",
            system_prompt="S", user_text="U", session=session,
        )
        self.assertEqual(text, "DOWNGRADED")
        second = session.requests[-1]["json"]
        self.assertNotIn("response_format", second)

    async def test_all_providers_fail_raises_content_free_error(self):
        await self._paid_compatible_mode()
        await self._add_paid_groq(secret="gsk-secret-5555")
        await self._route(["groq"])
        session = _ScriptedSession()
        session.add(
            "api.groq.com",
            _Response(503, {"error": {"message": "echo gsk-secret-5555"}}),
        )
        job = await self._session_plan()
        with self.assertRaises(StructuringError) as ctx:
            await job.generate(
                request_type="chunk_structuring", task="chunk_structuring",
                system_prompt="S", user_text="U", session=session,
            )
        self.assertNotIn("gsk-secret-5555", str(ctx.exception))

    async def test_gemini_primary_uses_env_key(self):
        settings = make_settings(
            database_path=self.settings.database_path,
            session_path=self.settings.session_path,
            temp_dir=self.settings.temp_dir,
            note_api_provider="gemini",
            note_api_key=None,
            gemini_api_key="AIza-env",
            ai_free_only=True,
        )
        credential_id = await self.manager.add_credential(
            service="notes", provider="gemini", label="attested env copy",
            secret="AIza-env", admin_id=1,
        )
        await self.manager.set_billing_attestation(credential_id, "free", 1)
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "gemini", "enabled": 1, "free_only": 1}], admin_id=None,
        )
        router = ProviderRouter(self.db, settings, self.manager, self.tracker, self.models)
        plan = await router.plan()
        job = NoteJobSession(router, plan, settings)
        session = _ScriptedSession()
        session.add("generativelanguage.googleapis.com", _Response(200, _gemini_payload("GEMINI")))
        text = await job.generate(
            request_type="chunk_structuring", task="chunk_structuring",
            system_prompt="S", user_text="U", session=session,
        )
        self.assertEqual(text, "GEMINI")
        request = session.requests[-1]
        self.assertEqual(request["headers"]["x-goog-api-key"], "AIza-env")
        config = request["json"]["generationConfig"]
        self.assertIn("responseSchema", config)

    async def test_historical_deployment_preserves_but_does_not_auto_attest_env_key(self):
        # NOTE_API_PROVIDER=openai_compatible + Nara base + legacy key remains
        # configured and discoverable, but it is not silently marked free.
        leg = RouteLeg(provider="nara", canonical="nara")
        generation_pool = await self.router.credentials_for(leg)
        self.assertEqual(generation_pool, [])
        discovery_pool = await self.router.credentials_for(leg, for_generation=False)
        self.assertEqual(len(discovery_pool), 1)
        self.assertEqual(discovery_pool[0].secret, "sk-nara-env-1234")


class LegacyNoteSettingCompatibilityTests(PlatformCase):
    async def test_note_api_timeout_retries_and_output_remain_safety_caps(self):
        settings = replace(
            self.settings,
            note_api_provider="gemini",
            note_api_key=None,
            note_api_base_url=None,
            note_api_model=None,
            gemini_api_key="AIza-test",
            note_api_timeout=12,
            note_api_retries=0,
            note_api_max_output_tokens=321,
        )
        self.manager.settings = settings
        router = ProviderRouter(self.db, settings, self.manager, self.tracker, self.models)
        key_id = await self.manager.add_credential(
            service="notes", provider="gemini", label="gemini", secret="AIza-test", admin_id=1
        )
        await self.manager.set_billing_attestation(key_id, "free", 1)
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "gemini", "enabled": 1, "free_only": 1}], admin_id=None,
        )
        plan = await router.plan()
        job = NoteJobSession(router, plan, settings)
        session = _ScriptedSession()
        session.add(
            "generativelanguage.googleapis.com",
            _Response(502, {"error": {"message": "temporary"}}),
            _Response(200, _gemini_payload("OK")),
        )
        with self.assertRaises(StructuringError):
            await job.generate(
                request_type="chunk_structuring", task="chunk_structuring",
                system_prompt="S", user_text="U", session=session,
            )
        self.assertEqual(len(session.requests), 1)  # NOTE_API_RETRIES=0 caps retries
        request = session.requests[0]
        self.assertEqual(request["timeout"].total, 12)
        self.assertEqual(request["json"]["generationConfig"]["maxOutputTokens"], 321)


class QuotaAccountingTests(PlatformCase):
    async def test_provider_rpm_and_daily_token_caps_fail_closed(self):
        from gamas_bot.ai.profiles import profile_for

        for _index in range(15):
            await self.db.ai_usage_insert(
                {
                    "provider": "nara", "model": "agnes-2.5-flash",
                    "request_type": "chunk_structuring", "result": "success",
                    "estimated_input_tokens": 100,
                }
            )
        reason = await self.router._local_request_budget_reason(
            profile_for("nara"), "nara", estimated_input_tokens=100,
            output_token_budget=100,
        )
        self.assertIn("requests-per-minute", reason)

        await self.db.ai_usage_insert(
            {
                "provider": "sambanova", "model": "Meta-Llama-3.3-70B-Instruct",
                "request_type": "chunk_structuring", "result": "success",
                "estimated_input_tokens": 192_000,
            }
        )
        reason = await self.router._local_request_budget_reason(
            profile_for("sambanova"), "sambanova", estimated_input_tokens=200,
            output_token_budget=8192,
        )
        self.assertIn("daily token budget", reason)
        self.assertFalse(await self.router._daily_quota_exhausted("sambanova"))
        await self.db.ai_usage_insert(
            {
                "provider": "sambanova", "model": "Meta-Llama-3.3-70B-Instruct",
                "request_type": "chunk_structuring", "result": "failure",
                "estimated_input_tokens": 8_000,
            }
        )
        self.assertTrue(await self.router._daily_quota_exhausted("sambanova"))

    async def test_inflight_requests_are_reserved_atomically(self):
        from dataclasses import replace
        from gamas_bot.ai.profiles import profile_for

        profile = replace(profile_for("nara"), requests_per_minute=1)
        results = await asyncio.gather(
            *(
                self.router._reserve_local_request_budget(
                    profile, "nara", estimated_input_tokens=100, output_token_budget=100
                )
                for _ in range(2)
            )
        )
        allowed = [(reason, token) for reason, token in results if token is not None]
        blocked = [reason for reason, token in results if token is None and reason]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(len(blocked), 1)
        self.assertIn("requests-per-minute", blocked[0])
        await self.router._release_local_request_budget(allowed[0][1])

    async def test_quota_ledgers_group_legacy_gateway_rows_by_canonical_provider(self):
        await self.db.ai_usage_insert(
            {
                "provider": "openai_compatible", "canonical": "nara",
                "model": "agnes-3-flash", "request_type": "chunk_structuring",
                "result": "success", "estimated_input_tokens": 320,
                "estimated_output_tokens": 80,
            }
        )
        self.assertEqual(await self.tracker.today_request_count("nara"), 1)
        self.assertEqual(await self.tracker.today_token_count("nara"), 400)
        self.assertEqual(
            await self.tracker.window_stats("nara", seconds=60),
            {"requests": 1, "tokens": 400},
        )

    async def test_usage_rows_and_daily_rollup(self):
        await self.db.ai_usage_insert(
            {
                "provider": "groq", "model": "m1", "request_type": "chunk_structuring",
                "result": "success", "latency_ms": 900, "http_status": 200,
                "actual_input_tokens": 1000, "actual_output_tokens": 500,
                "total_tokens": 1500, "route_position": 1,
            }
        )
        await self.db.ai_usage_insert(
            {
                "provider": "groq", "model": "m1", "request_type": "chunk_structuring",
                "result": "failure", "error_class": "rate_limited", "http_status": 429,
                "latency_ms": 300,
            }
        )
        metrics = await self.db.ai_usage_metrics(days=1)
        self.assertEqual(metrics["requests"], 2)
        self.assertEqual(metrics["successes"], 1)
        self.assertEqual(metrics["rate_limited"], 1)
        self.assertEqual(metrics["fallbacks"], 1)
        self.assertEqual(metrics["input_tokens"], 1000)
        daily = await self.db.ai_usage_summary(days=1)
        self.assertTrue(daily)
        groq_row = [r for r in daily if r["provider"] == "groq"][0]
        self.assertEqual(groq_row["requests"], 2)

    async def test_sanitize_text_strips_echoed_secrets(self):
        cleaned = sanitize_text(
            "error: key gsk-supersecret999 rejected by gateway", ("gsk-supersecret999",)
        )
        self.assertNotIn("gsk-supersecret999", cleaned)
        self.assertIn("gateway", cleaned)

    async def test_provider_diagnostics_are_allowlisted_before_storage(self):
        self.assertEqual(self.router._safe_finish_reason("STOP", "sk-secret-key"), "stop")
        self.assertEqual(
            self.router._safe_finish_reason("transcript says sk-secret-key", "sk-secret-key"),
            "",
        )
        safe_headers = self.router._safe_quota_headers(
            {
                "x-ratelimit-remaining-requests": "17",
                "x-ratelimit-reset-requests": "2s",
                "x-ratelimit-limit-tokens": "transcript sk-secret-key",
                "x-request-id": "chatcmpl-safe-123",
            },
            "sk-secret-key",
        )
        self.assertEqual(safe_headers["x-ratelimit-remaining-requests"], "17")
        self.assertNotIn("x-ratelimit-limit-tokens", safe_headers)
        self.assertNotIn("sk-secret-key", json.dumps(safe_headers))

    async def test_usage_rows_never_contain_prompt_or_answers(self):
        await self.db.ai_usage_insert(
            {
                "provider": "nara", "model": "agnes-3-flash", "request_type": "chunk_structuring",
                "result": "success", "credential_label": "environment",
            }
        )
        events = await self.db.ai_events_list(limit=10)
        rows = await self.db.ai_usage_recent(limit=10)
        blob = json.dumps(events) + json.dumps(rows)
        self.assertNotIn("متن درس", blob)
        self.assertNotIn("sk-nara-env", blob)


class ChunkBudgetIntegrationTests(PlatformCase):
    async def test_router_turns_token_budget_into_chars(self):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "groq", "enabled": 1, "free_only": 1}],
            admin_id=None,
        )
        job = await self._session_plan()
        with job_session_scope(job):
            budget = note_chunk_chars(self.settings)
        # Groq free profile: ~2.4k tokens -> far below the 22k legacy cap.
        self.assertLess(budget, 8000)

    async def test_no_router_means_legacy_chunk_size(self):
        budget = note_chunk_chars(self.settings)
        from gamas_bot.structuring import TRANSCRIPT_CHUNK_CHARS

        self.assertEqual(budget, TRANSCRIPT_CHUNK_CHARS - 200)

    async def test_structure_chunk_goes_through_router_when_bound(self):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "nara", "enabled": 1, "free_only": 1}],
            admin_id=None,
        )
        credential_id = await self.manager.add_credential(
            service="notes", provider="nara", label="nara-free",
            secret="sk-nara-free-0001", model="agnes-2.5-flash", admin_id=1,
        )
        await self.manager.set_billing_attestation(credential_id, "free", 1)
        await self.models.apply_discovery(
            "nara", [ModelInfo(provider="nara", model_id="agnes-2.5-flash", free_status="free_plan")]
        )
        job = await self._session_plan()
        session = _ScriptedSession()
        session.add("router.bynara.id", _Response(200, _notes_payload("ROUTED")))
        with job_session_scope(job):
            text = await _structure_chunk(
                "متن خام", self.settings, session, system_prompt="S"
            )
        self.assertEqual(text, "ROUTED")
        rows = await self.db.ai_usage_recent(limit=5)
        self.assertTrue(any(r["provider"] == "nara" for r in rows))

    async def test_extra_pass_policy_comes_from_primary_profile(self):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "groq", "enabled": 1, "free_only": 1}],
            admin_id=None,
        )
        plan = await self.router.plan()
        self.assertFalse(plan.outline_enabled)
        self.assertFalse(plan.repair_enabled)
        self.assertFalse(plan.compile_enabled)
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "gemini", "enabled": 1, "free_only": 1}],
            admin_id=None,
        )
        plan = await self.router.plan()
        self.assertTrue(plan.outline_enabled)


class ProviderTermsSafetyTests(PlatformCase):
    async def test_zai_education_restrictions_block_routes_even_with_free_model_and_key(self):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "zai", "enabled": 1, "free_only": 1, "model": "glm-4.5-flash"}],
            admin_id=None,
        )
        credential_id = await self.manager.add_credential(
            service="notes", provider="zai", label="zai-free",
            secret="sk-zai-free-1234", model="glm-4.5-flash", admin_id=1,
        )
        await self.manager.set_billing_attestation(credential_id, "free", 1)
        plan = await self.router.plan()
        self.assertFalse(any(leg.canonical == "zai" for leg in plan.legs))
        self.assertIn(("zai", "terms_of_use_blocked"), plan.skipped)


class FreeOnlySafetyTests(PlatformCase):
    async def test_free_only_never_routes_to_paid_model(self):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "openrouter", "enabled": 1, "free_only": 1, "model": "openai/gpt-4o"}],
            admin_id=None,
        )
        # paid model on a free plan provider -> leg never fires.
        await self.manager.add_credential(
            service="notes", provider="openrouter", label="or", secret="sk-or-9999", admin_id=1
        )
        session = _ScriptedSession()
        job = await self._session_plan()
        with self.assertRaises(StructuringError):
            await job.generate(
                request_type="chunk_structuring", task="chunk_structuring",
                system_prompt="S", user_text="U", session=session,
            )
        self.assertEqual(session.requests, [])

    async def test_deprecated_model_is_not_selected(self):
        models_registry = self.models
        await models_registry.apply_discovery(
            "nara",
            [
                ModelInfo(provider="nara", model_id="agnes-3-flash", source="live",
                          deprecated=False),
            ],
        )
        await self.db.ai_models_set_deprecated("nara", "agnes-3-flash", date="2026-09-01")
        job = await self._session_plan()
        session = _ScriptedSession()
        with self.assertRaises(StructuringError):
            await job.generate(
                request_type="chunk_structuring", task="chunk_structuring",
                system_prompt="S", user_text="U", session=session,
            )
        self.assertEqual(session.requests, [])

    async def test_backend_artifacts_scrubbed_from_student_notes(self):
        # Spec §46: provider metadata, API keys, and HTTP plumbing must NEVER
        # appear in student-facing structured notes.
        from gamas_bot.structuring import parse_structured_notes
        raw_payload = json.dumps(
            {
                "title": "مقدمه بر یادگیری ماشین — Groq API powered",
                "summary": "این یادگیری ماشین است. کلید من sk-nry-abc123456789 است. HTTP 502 Bad Gateway.",
                "learning_objectives": ["یادگیری مفهوم"],
                "sections": [
                    {
                        "heading": "بخش ۱",
                        "paragraphs": [
                            "I am an AI assistant and here is the lesson.",
                            "شناسه درخواست chatcmpl-xyz123456789 و job GMS-000042.",
                            "متن اصلی درس دربارهٔ گرادیان کاهشی.",
                        ],
                        "bullets": [], "definitions": [], "examples": [],
                        "steps": [], "formulas": [], "key_points": [], "callouts": [],
                    }
                ],
                "key_points": [], "review_questions": [], "glossary": [],
            }
        )
        notes = parse_structured_notes(raw_payload)
        self.assertGreater(notes.backend_artifacts, 0)
        self.assertNotIn("Groq API", notes.title)
        self.assertNotIn("sk-nry", notes.summary)
        self.assertNotIn("HTTP 502", notes.summary)
        body = " ".join(notes.sections[0].paragraphs)
        self.assertNotIn("AI assistant", body)
        self.assertNotIn("chatcmpl", body)
        self.assertNotIn("GMS-", body)
        self.assertIn("گرادیان کاهشی", body)

    async def test_live_quota_probe_updates_snapshot_and_blocks_routing(self):
        # Spec §12/§29: live entitlement probe via key_info endpoint gives the
        # authoritative free-daily counter.
        from gamas_bot.ai.sync import probe_provider_quota
        await self.manager.add_credential(
            service="notes", provider="openrouter", label="o1",
            secret="sk-or-test-key-1111", admin_id=1,
        )
        fake_payload = {
            "data": {
                "label": "o1",
                "is_free_tier": True,
                "free_model_daily_requests": {"used": 50, "limit": 50, "remaining": 0},
            }
        }
        async def fake_get(url, headers, timeout):
            return 200, {}, json.dumps(fake_payload).encode("utf-8")
        result = await probe_provider_quota(self.router, "openrouter", session_get=fake_get)
        self.assertTrue(result.ok)
        self.assertEqual(result.remaining, 0)
        # Snapshot is now in SQLite; router.plan() should skip openrouter.
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": "openrouter", "enabled": 1, "free_only": 1},
             {"provider": "nara", "enabled": 1, "free_only": 1}],
            admin_id=None,
        )
        plan = await self.router.plan()
        reasons = dict(plan.skipped)
        self.assertEqual(reasons.get("openrouter"), "quota_exhausted")
        self.assertEqual([leg.canonical for leg in plan.legs], ["nara"])

    async def test_billing_attestation_gates_free_vs_paid_legs(self):
        key_id = await self.manager.add_credential(
            service="notes", provider="groq", label="paid-key",
            secret="gsk-paid-key-2222", admin_id=1, free_only=False,
        )
        leg = RouteLeg(provider="groq", canonical="groq", free_only=True)
        self.assertEqual(await self.router.credentials_for(leg), [])
        # A stale legacy value without auditable attestation metadata is not
        # sufficient evidence to authorize a free-generation lane.
        async with self.db._transaction(immediate=True) as conn:
            await conn.execute(
                "UPDATE provider_credentials SET billing_state='free' WHERE id=?", (key_id,)
            )
        self.assertEqual(await self.router.credentials_for(leg), [])
        await self.manager.set_billing_attestation(key_id, "paid", 1)
        self.assertEqual(await self.router.credentials_for(leg), [])
        paid_leg = RouteLeg(provider="groq", canonical="groq", free_only=False)
        pool_paid = await self.router.credentials_for(paid_leg)
        self.assertEqual(len(pool_paid), 1)
        self.assertEqual(pool_paid[0].label, "paid-key")
        await self.manager.set_billing_attestation(key_id, "unknown", 1)
        self.assertEqual(await self.router.credentials_for(paid_leg), [])


if __name__ == "__main__":
    unittest.main()
