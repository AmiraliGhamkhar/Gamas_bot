"""AI Provider Platform integration tests (offline, mock sessions only).

Covers: provider registry/classification, model discovery + cache,
capability resolution, FREE_ONLY filtering + paid-fallback rejection,
provider-level failover + credential rotation/quarantine, 429/502 handling,
quota accounting, structured lifecycle events, secret hygiene, deprecation /
region / commercial-use restrictions, and legacy backward compatibility.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from gamas_bot.ai import adapters as ai_adapters
from gamas_bot.ai.models import (
    FREE_PROMOTIONAL,
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
    AllProvidersFailedError,
    NoteJobSession,
    ProviderRouter,
    RouteLeg,
    job_session_scope,
)
from gamas_bot.ai.sync import sync_provider_catalog
from gamas_bot.ai.usage import AIUsageTracker, sanitize_text
from gamas_bot.config import Settings
from gamas_bot.database import Database
from gamas_bot.provider_credentials import ProviderCredentialManager
from gamas_bot.structuring import (
    ProviderHTTPError,
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
        self.requests.append({"url": url, "headers": headers, "json": json})
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
        self.assertTrue(free_only_generation_allowed("groq"))
        self.assertTrue(free_only_generation_allowed("openrouter"))
        self.assertFalse(free_only_generation_allowed("cohere"))
        self.assertFalse(free_only_generation_allowed("cerebras"))
        self.assertFalse(free_only_generation_allowed("alibaba"))
        self.assertFalse(free_only_generation_allowed("anthropic"))
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
        self.assertEqual(registry_info("cloudflare").classification, ProviderClass.PERMANENT_FREE)
        self.assertEqual(registry_info("zai").classification, ProviderClass.PROMOTIONAL_FREE)
        self.assertEqual(registry_info("nvidia").classification, ProviderClass.PROMOTIONAL_FREE)
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
        self.assertEqual(unknown.free_status, "free_plan")  # account-level free plan

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
            return 200, {}, json.dumps(
                {"data": [{"id": "agnes-3-flash"}, {"id": "agnes-3-pro"}]}
            ).encode("utf-8")

        result = await sync_provider_catalog(self.router, "nara", session_get=fake_fetch, force=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.synced, 2)
        events = await self.db.ai_events_list(event="provider_sync")
        self.assertTrue(events)
        second = await sync_provider_catalog(self.router, "nara", session_get=fake_fetch)
        self.assertTrue(second.cached)


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
        day = "2026-10-08"
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
    async def _route(self, providers):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [{"provider": p, "enabled": 1, "free_only": 1} for p in providers],
            admin_id=None,
        )

    async def test_provider_level_failover_after_429_with_no_credentials(self):
        await self._route(["groq", "nara"])
        session = _ScriptedSession()
        job = await self._session_plan()
        # groq has no credential -> leg fails -> nara succeeds via env key.
        session.add("router.bynara.id", _Response(200, _notes_payload("{\"title\": \"ت\"}")))
        text = await job.generate(
            request_type="chunk_structuring", task="chunk_structuring",
            system_prompt="S", user_text="U", session=session,
        )
        self.assertEqual(text, "{\"title\": \"ت\"}")
        self.assertGreaterEqual(job.fallbacks, 1)
        rows = await self.db.ai_usage_recent(limit=10)
        self.assertTrue(any(r["provider"] == "nara" and r["result"] == "success" for r in rows))

    async def test_quota_failover_groq_429_then_nara(self):
        groq_id = await self.manager.add_credential(
            service="notes", provider="groq", label="g1",
            secret="gsk-test-key-0001", admin_id=1,
        )
        await self._route(["groq", "nara"])
        session = _ScriptedSession()
        session.add(
            "api.groq.com",
            _Response(429, {"error": {"message": "rate limited"}}, {"Retry-After": "30"}),
        )
        session.add("router.bynara.id", _Response(200, _notes_payload("OK")))
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
        bad = await self.manager.add_credential(
            service="notes", provider="groq", label="bad", secret="gsk-bad-1111", admin_id=1
        )
        good = await self.manager.add_credential(
            service="notes", provider="groq", label="good", secret="gsk-good-2222", admin_id=1
        )
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
        await self.manager.add_credential(
            service="notes", provider="groq", label="g1", secret="gsk-key-3333", admin_id=1
        )
        await self._route(["groq", "nara"])
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
        await self.manager.add_credential(
            service="notes", provider="groq", label="g1", secret="gsk-key-4444", admin_id=1
        )
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
        await self.manager.add_credential(
            service="notes", provider="groq", label="g1", secret="gsk-secret-5555", admin_id=1
        )
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
        await self._route(["gemini"])
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

    async def test_historical_deployment_replays_legacy_credential_pool(self):
        # NOTE_API_PROVIDER=openai_compatible + Nara base + key (no stored keys).
        job = await self._session_plan()
        leg = RouteLeg(provider="nara", canonical="nara")
        pool = await self.router.credentials_for(leg)
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0].secret, "sk-nara-env-1234")


class QuotaAccountingTests(PlatformCase):
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


if __name__ == "__main__":
    unittest.main()
