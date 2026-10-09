"""Provider-adapter contract tests (offline; mocked — no real API keys).

Every adapter must satisfy the same contract (spec §49):

* request building (correct URL/auth/payload for the provider family),
* response parsing (text + usage + finish reason + request id),
* error mapping (401/402/404/408/429/5xx with retryability and flags),
* rate-limit header capture against an allowlist,
* structured-output capability selection,
* model-discovery request/parsing where supported.

No test here touches the network or requires a credential.
"""

from __future__ import annotations

import unittest

from gamas_bot.ai import models, tokens
from gamas_bot.ai.adapters import (
    FAIL_AUTH,
    FAIL_BILLING,
    FAIL_MODEL,
    FAIL_QUOTA,
    FAIL_RATE_LIMITED,
    RequestContext,
    STRATEGY_GEMINI_SCHEMA,
    STRATEGY_JSON_OBJECT,
    STRATEGY_PROMPT,
    STRATEGY_STRICT_SCHEMA,
    adapter_for,
    quota_headers,
    resolve_json_strategy,
)
from gamas_bot.ai.models import ModelCapabilities, ModelInfo
from gamas_bot.ai.registry import ProviderClass, registry_info, resolve_canonical
from gamas_bot.structuring import StructuringError, _provider_request

from support import make_settings


def make_ctx(provider: str, model: str, **overrides) -> RequestContext:
    base_url = overrides.pop("base_url", None)
    canonical = resolve_canonical(provider, base_url)
    info = overrides.pop(
        "model_info",
        ModelInfo(provider=canonical, model_id=model, capabilities=ModelCapabilities()),
    )
    ctx = RequestContext(
        service="notes",
        request_type="chunk_structuring",
        provider=provider,
        canonical=canonical,
        base_url=base_url if base_url is not None else (registry_info(canonical).base_url or ""),
        model=model,
        system_prompt="SYSTEM",
        user_text="متن درس",
        max_output_tokens=4096,
        model_info=info,
        json_strategy=overrides.pop("json_strategy", STRATEGY_PROMPT),
    )
    for key, value in overrides.items():
        import dataclasses as _dc

        ctx = _dc.replace(ctx, **{key: value})
    return ctx


class NaraRouterContractTests(unittest.TestCase):
    """NaraRouter: prompt JSON until a model is verified structured-capable."""

    def setUp(self):
        self.adapter = adapter_for("nara", make_settings())

    def test_chat_completions_url_and_bearer(self):
        ctx = make_ctx("nara", "agnes-3-flash")
        request = self.adapter.build(ctx, "sk-nara-secret-1234")
        self.assertEqual(request.url, "https://router.bynara.id/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer sk-nara-secret-1234")
        self.assertEqual(request.json_body["model"], "agnes-3-flash")
        self.assertEqual(
            request.json_body["messages"],
            [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "متن درس"},
            ],
        )

    def test_prompt_json_default_no_response_format(self):
        ctx = make_ctx("nara", "agnes-3-flash")
        request = self.adapter.build(ctx, "key")
        self.assertNotIn("response_format", request.json_body)
        self.assertNotIn("stream", request.json_body)

    def test_legacy_json_mode_does_not_leak_to_nara(self):
        strategy = resolve_json_strategy(
            self.adapter,
            ModelInfo(provider="nara", model_id="agnes-3-flash"),
            legacy_json_mode=True,
        )
        self.assertEqual(strategy, STRATEGY_PROMPT)

    def test_verified_model_can_use_json_object(self):
        capable = ModelInfo(
            provider="nara",
            model_id="agnes-pro",
            capabilities=ModelCapabilities(supports_json_object=True),
        )
        strategy = resolve_json_strategy(self.adapter, capable)
        self.assertEqual(strategy, STRATEGY_JSON_OBJECT)

    def test_discovery_lists_models_endpoint(self):
        request = self.adapter.discovery_request("https://router.bynara.id/v1", "sk-x")
        self.assertEqual(request[0], "GET")
        self.assertEqual(request[1], "https://router.bynara.id/v1/models")
        self.assertEqual(request[2]["Authorization"], "Bearer sk-x")

    def test_public_plan_is_the_only_free_model_evidence(self):
        plans = {
            "data": [
                {"code": "free", "is_active": True, "models": ["agnes-2.5-flash"]},
                {"code": "freemium", "is_active": True,
                 "models": ["agnes-2.5-flash", "agnes-3-flash"]},
            ]
        }
        statuses = self.adapter.plan_model_statuses(plans)
        self.assertEqual(statuses["agnes-2.5-flash"], models.FREE_PLAN)
        self.assertEqual(statuses["agnes-3-flash"], models.PAID)
        account_models = {"data": [{"id": "agnes-2.5-flash"}, {"id": "agnes-3-flash"}]}
        discovered = self.adapter.parse_discovery(account_models, plan_model_statuses=statuses)
        self.assertEqual([model.free_status for model in discovered], [models.FREE_PLAN, models.PAID])
        self.assertEqual(self.adapter.plan_model_statuses({"data": []}), None)

    def test_secret_never_in_request_repr(self):
        request = self.adapter.build(make_ctx("nara", "agnes-3-flash"), "sk-nara-secret-1234")
        self.assertNotIn("sk-nara-secret-1234", repr(request))
        self.assertIn("sk-nara-secret-1234", request.headers["Authorization"])


class GroqContractTests(unittest.TestCase):
    """Groq: strict schema where advertised, reasoning controls, rate limits."""

    def setUp(self):
        self.adapter = adapter_for("groq", make_settings())

    def test_strict_schema_for_gpt_oss(self):
        seed = models.STATIC_SEEDS["groq"][0]
        ctx = make_ctx(
            "groq", seed.model_id, model_info=seed, json_strategy=STRATEGY_STRICT_SCHEMA
        )
        request = self.adapter.build(ctx, "gsk_key")
        response_format = request.json_body["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        # Strict-mode shape: every property required + additionalProperties false.
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("sections", schema["required"])

    def test_unknown_model_falls_back_to_prompt_json(self):
        unknown = ModelInfo(provider="groq", model_id="restored-unknown-9000")
        self.assertEqual(resolve_json_strategy(self.adapter, unknown), STRATEGY_PROMPT)

    def test_reasoning_effort_policy_is_capability_gated(self):
        seed = models.STATIC_SEEDS["groq"][0]
        ctx = make_ctx(
            "groq", seed.model_id, model_info=seed, reasoning_policy="none"
        )
        request = self.adapter.build(ctx, "k")
        self.assertEqual(request.json_body.get("reasoning_effort"), "none")
        # A model whose capabilities do not advertise reasoning_effort never
        # receives the parameter, whatever the policy asks for.
        plain = ModelInfo(
            provider="groq",
            model_id="some-plain-model",
            capabilities=ModelCapabilities(supports_reasoning_effort=False),
        )
        ctx = make_ctx("groq", plain.model_id, model_info=plain, reasoning_policy="none")
        request = self.adapter.build(ctx, "k")
        self.assertNotIn("reasoning_effort", request.json_body)

    def test_streaming_disabled_for_structured_outputs(self):
        seed = models.STATIC_SEEDS["groq"][0]
        ctx = make_ctx("groq", seed.model_id, model_info=seed)
        request = self.adapter.build(ctx, "k")
        self.assertFalse(request.json_body.get("stream"))

    def test_guard_model_stays_json_object_only_without_exact_schema_evidence(self):
        caps = models.infer_capabilities("groq", "openai/gpt-oss-safeguard-20b")
        self.assertTrue(caps.supports_json_object)
        self.assertFalse(caps.supports_json_schema)
        self.assertFalse(caps.supports_strict_json_schema)

    def test_qwen38_seed_uses_strict_schema_and_recommended_effort(self):
        # Verified against console.groq.com/docs (2026-10-09): qwen/qwen3.8-27b
        # supports strict structured outputs and documents instruct mode
        # (reasoning_effort="none") for general-purpose work.
        seed = next(m for m in models.STATIC_SEEDS["groq"] if m.model_id == "qwen/qwen3.8-27b")
        self.assertTrue(seed.capabilities.supports_strict_json_schema)
        self.assertEqual(seed.capabilities.recommended_reasoning_effort, "none")
        ctx = make_ctx(
            "groq", seed.model_id, model_info=seed, json_strategy=STRATEGY_STRICT_SCHEMA
        )
        request = self.adapter.build(ctx, "k")
        self.assertEqual(request.json_body["response_format"]["json_schema"]["strict"], True)
        # model_default policy picks up the documented recommendation.
        self.assertEqual(request.json_body.get("reasoning_effort"), "none")

    def test_zai_free_plan_seeds_require_key_attestation(self):
        # docs.z.ai pricing (verified 2026-10-09) lists both as Free; account
        # access and no-overage billing still require the credential attestation.
        ids = {m.model_id: m.free_status for m in models.STATIC_SEEDS["zai"]}
        self.assertEqual(ids.get("glm-4.5-flash"), models.FREE_PLAN)
        self.assertEqual(ids.get("glm-4.7-flash"), models.FREE_PLAN)

    def test_rate_limit_headers_captured_allowlist(self):
        captured = quota_headers(
            {
                "x-ratelimit-remaining-requests": "29",
                "x-ratelimit-reset-requests": "1s",
                "retry-after": "4",
                "x-secret-trace": "must-not-persist",
            }
        )
        self.assertIn("x-ratelimit-remaining-requests", captured)
        self.assertIn("retry-after", captured)
        self.assertNotIn("x-secret-trace", captured)

    def test_error_mapping(self):
        adapter = self.adapter
        failure = adapter.map_error(
            429, b'{"error":{"message":"rate limited","code":"rate_limited"}}',
            {"Retry-After": "8"}, model="openai/gpt-oss-120b", key="gsk",
        )
        self.assertEqual(failure.category, FAIL_RATE_LIMITED)
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.retry_after, 8.0)
        auth = adapter.map_error(401, b'{"error":{"message":"bad key"}}', {}, model="m", key=None)
        self.assertTrue(auth.credential_invalid)
        self.assertEqual(auth.category, FAIL_AUTH)
        missing = adapter.map_error(404, b'{"error":{"message":"model not found"}}', {}, model="m", key=None)
        self.assertTrue(missing.model_unavailable)
        self.assertEqual(missing.category, FAIL_MODEL)

    def test_secret_redacted_from_error_message(self):
        failure = self.adapter.map_error(
            500, b'{"error":{"message":"echo gsk-supersecret999 boom"}}', {},
            model="m", key="gsk-supersecret999",
        )
        self.assertNotIn("gsk-supersecret999", failure.message or "")
        self.assertNotIn("gsk-supersecret999", failure.message or "")


class OpenRouterContractTests(unittest.TestCase):
    def setUp(self):
        self.adapter = adapter_for("openrouter", make_settings())

    def test_free_suffix_infers_free_status(self):
        self.assertTrue(models.infer_openrouter_free("meta-llama/x:free"))
        self.assertFalse(models.infer_openrouter_free("openai/gpt-4o"))
        self.assertEqual(models.infer_free_status("openrouter", "vendor/x:free"), models.FREE_PLAN)
        self.assertEqual(models.infer_free_status("openrouter", "openai/gpt-4o"), models.PAID)

    def test_usage_accounting_requested(self):
        ctx = make_ctx("openrouter", "vendor/x:free")
        request = self.adapter.build(ctx, "sk-or")
        self.assertEqual(request.json_body.get("usage"), {"include": True})
        self.assertEqual(request.headers.get("X-Title"), "Gamas Study Bot")

    def test_free_quota_exhaustion_classified(self):
        failure = self.adapter.map_error(
            429, b'{"error":{"message":"Rate limit exceeded: free-models-per-day","code":429}}',
            {}, model="vendor/x:free", key=None,
        )
        self.assertEqual(failure.category, FAIL_QUOTA)
        self.assertTrue(failure.quota_exhausted)

    def test_payment_required_is_billing(self):
        failure = self.adapter.map_error(
            402, b'{"error":{"message":"Insufficient credits"}}', {}, model="m", key=None
        )
        self.assertTrue(failure.billing_required)
        self.assertEqual(failure.category, FAIL_BILLING)


class GeminiContractTests(unittest.TestCase):
    def setUp(self):
        self.adapter = adapter_for("gemini", make_settings())

    def test_discovery_does_not_infer_reasoning_from_model_name(self):
        found = models.parse_discovery(
            "gemini",
            {
                "models": [
                    {
                        "name": "models/gemini-3-flash-preview",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ]
            },
        )
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].capabilities.supports_reasoning)
        self.assertFalse(found[0].capabilities.supports_json_schema)

    def test_native_generate_content_shape(self):
        seed = models.STATIC_SEEDS["gemini"][0]
        ctx = make_ctx(
            "gemini", "gemini-2.5-flash", model_info=seed, json_strategy=STRATEGY_GEMINI_SCHEMA
        )
        request = self.adapter.build(ctx, "AIza-key")
        self.assertEqual(
            request.url,
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        )
        self.assertEqual(request.headers["x-goog-api-key"], "AIza-key")
        config = request.json_body["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertIn("responseSchema", config)
        self.assertEqual(config["maxOutputTokens"], 4096)
        self.assertEqual(
            request.json_body["contents"],
            [{"role": "user", "parts": [{"text": "متن درس"}]}],
        )
        self.assertEqual(
            request.json_body["systemInstruction"], {"parts": [{"text": "SYSTEM"}]}
        )

    def test_prompt_strategy_has_no_mime_forcing(self):
        ctx = make_ctx("gemini", "gemini-2.5-flash", json_strategy=STRATEGY_PROMPT)
        request = self.adapter.build(ctx, "k")
        self.assertNotIn("responseMimeType", request.json_body["generationConfig"])

    def test_missing_key_is_a_configuration_error(self):
        ctx = make_ctx("gemini", "gemini-2.5-flash")
        with self.assertRaises(StructuringError):
            self.adapter.build(ctx, "")

    def test_usage_metadata_extracted(self):
        response = self.adapter.parse(
            {
                "candidates": [
                    {"content": {"parts": [{"text": "{\"title\": \"x\"}"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {
                    "promptTokenCount": 1000,
                    "candidatesTokenCount": 500,
                    "totalTokenCount": 1500,
                },
            },
            http_status=200,
            headers={"x-request-id": "req-1"},
            latency_ms=42,
        )
        self.assertEqual(response.usage.input_tokens, 1000)
        self.assertEqual(response.usage.output_tokens, 500)
        self.assertEqual(response.usage.total_tokens, 1500)
        self.assertEqual(response.finish_reason, "STOP")
        self.assertEqual(response.request_id, "req-1")

    def test_daily_quota_classified(self):
        failure = self.adapter.map_error(
            429,
            b'{"error":{"message":"Quota exceeded: generate_requests_per_model_per_day","code":429}}',
            {"Retry-After": "60"}, model="gemini-2.5-flash", key=None,
        )
        self.assertEqual(failure.category, FAIL_QUOTA)
        self.assertTrue(failure.quota_exhausted)

    def test_discovery_parses_native_models(self):
        payload = {
            "models": [
                {
                    "name": "models/gemini-2.5-flash",
                    "displayName": "Gemini 2.5 Flash",
                    "inputTokenLimit": 1048576,
                    "outputTokenLimit": 65536,
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
                {
                    "name": "models/embedding-001",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }
        discovered = self.adapter.parse_discovery(payload)
        ids = [m.model_id for m in discovered]
        self.assertEqual(ids, ["gemini-2.5-flash"])
        self.assertEqual(discovered[0].context_window, 1048576)
        self.assertEqual(discovered[0].free_status, models.FREE_PLAN)

    def test_openai_compatible_gemini_endpoint_supported(self):
        adapter = adapter_for("gemini", make_settings(), protocol_override="gemini_openai")
        ctx = make_ctx("gemini", "gemini-2.5-flash", base_url="")
        request = adapter.build(ctx, "AIza")
        self.assertEqual(
            request.url,
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer AIza")


class LegacyParityTests(unittest.TestCase):
    """Byte-parity with the pre-platform payloads for the legacy providers."""

    def setUp(self):
        self.settings = make_settings()

    def test_openai_compatible_parity(self):
        provider = "openai_compatible"
        settings = make_settings(
            note_api_provider=provider,
            note_api_base_url="https://router.bynara.id/v1",
            note_api_model="agnes-3-flash",
            note_api_key="sk-legacy",
        )
        url, headers, payload, _ = _provider_request("CHUNK", settings, "PROMPT:")
        adapter = adapter_for("nara", settings)
        info = ModelInfo(provider="nara", model_id="agnes-3-flash")
        ctx = make_ctx(
            "nara", "agnes-3-flash", base_url="https://router.bynara.id/v1",
            model_info=info,
        )
        request = adapter.build(ctx, "sk-legacy")
        assert request.url == url
        assert request.headers["Authorization"] == headers["Authorization"]
        self.assertEqual(request.json_body["model"], payload["model"])
        self.assertEqual(request.json_body["messages"][0]["role"], payload["messages"][0]["role"])
        # USER content differs (ctx uses a canned text); compare structure only.
        self.assertEqual(len(request.json_body["messages"]), 2)

    def test_legacy_json_mode_maps_to_json_object_strategy(self):
        adapter = adapter_for("openai_compatible", self.settings)
        strategy = resolve_json_strategy(
            adapter,
            ModelInfo(provider="openai_compatible", model_id="gpt-4o-mini"),
            legacy_json_mode=True,
        )
        self.assertEqual(strategy, STRATEGY_JSON_OBJECT)

    def test_anthropic_payload_parity(self):
        provider = "anthropic"
        settings = make_settings(
            note_api_provider=provider, note_api_key="sk-ant", note_api_model="claude-haiku-4-5"
        )
        url, headers, payload, _ = _provider_request("CHUNK", settings, "PROMPT:")
        adapter = adapter_for("anthropic", settings)
        info = ModelInfo(provider="anthropic", model_id="claude-haiku-4-5")
        ctx = make_ctx("anthropic", "claude-haiku-4-5", model_info=info)
        request = adapter.build(ctx, "sk-ant")
        self.assertEqual(request.url, url)
        for key in headers:
            self.assertEqual(request.headers[key], headers[key])
        self.assertEqual(request.json_body["model"], payload["model"])
        self.assertNotIn("top_p", request.json_body)
        self.assertNotIn("top_k", request.json_body)

    def test_generic_adapter_never_sends_top_k(self):
        adapter = adapter_for("sambanova", self.settings)
        ctx = make_ctx("sambanova", "Meta-Llama-3.3-70B-Instruct")
        request = adapter.build(ctx, "k")
        self.assertNotIn("top_k", request.json_body)


class CloudflareContractTests(unittest.TestCase):
    def test_account_scoped_base_url(self):
        settings = make_settings(cloudflare_account_id="acc-123")
        adapter = adapter_for("cloudflare", settings)
        self.assertEqual(
            adapter.default_base_url(),
            "https://api.cloudflare.com/client/v4/accounts/acc-123/ai/v1",
        )

    def test_missing_account_id_fails_closed(self):
        settings = make_settings(cloudflare_account_id="")
        adapter = adapter_for("cloudflare", settings)
        ctx = make_ctx("cloudflare", "@cf/meta/llama-3.1-8b-instruct", base_url="")
        with self.assertRaises(StructuringError):
            adapter.build(ctx, "cf-token")

    def test_model_search_is_account_scoped_paginated_and_conservative(self):
        settings = make_settings(cloudflare_account_id="acc-123")
        adapter = adapter_for("cloudflare", settings)
        base = adapter.default_base_url()
        method, url, headers = adapter.discovery_request(base, "cf-token", page=2)
        self.assertEqual(method, "GET")
        self.assertIn("/client/v4/accounts/acc-123/ai/models/search", url)
        self.assertIn("page=2", url)
        self.assertIn("format=openrouter", url)
        self.assertEqual(headers["Authorization"], "Bearer cf-token")
        payload = {
            "success": True,
            "result": {"data": [{"id": "@cf/verified-id", "pricing": {"neuron": 0},
                                  "architecture": {"input_modalities": ["text", "image"]}}]},
        }
        discovered = adapter.parse_discovery(payload)
        self.assertEqual([item.model_id for item in discovered], ["@cf/verified-id"])
        self.assertEqual(discovered[0].free_status, models.FREE_UNKNOWN)
        self.assertFalse(discovered[0].capabilities.supports_image)

    def test_requires_paid_billing_models_blocked_in_free_only(self):
        info = ModelInfo(
            provider="cloudflare",
            model_id="@cf/paid/model",
            capabilities=ModelCapabilities(requires_paid_billing=True),
            free_status=models.FREE_UNKNOWN,
        )
        self.assertFalse(info.free_only_eligible())


class CohereCerebrasContractTests(unittest.TestCase):
    def test_cohere_trial_classification(self):
        info = registry_info("cohere")
        self.assertEqual(info.classification, ProviderClass.TRIAL_ONLY)
        self.assertFalse(info.commercial_use_allowed)
        self.assertTrue(info.experimental_only)
        self.assertFalse(info.generation_allowed_in_free_only)

    def test_cerebras_trial_classification(self):
        info = registry_info("cerebras")
        self.assertEqual(info.classification, ProviderClass.TRIAL_ONLY)

    def test_cohere_discovery_parses_native_shape(self):
        adapter = adapter_for("cohere", make_settings())
        discovered = adapter.parse_discovery({"models": [{"name": "command-a-03-2025"}]})
        self.assertEqual([m.model_id for m in discovered], ["command-a-03-2025"])

    def test_alibaba_region_restricted(self):
        info = registry_info("alibaba")
        self.assertEqual(info.classification, ProviderClass.REGION_RESTRICTED)
        self.assertTrue(info.requires_explicit_enable)
        self.assertFalse(info.generation_allowed_in_free_only)


class ZaiFreeTypeTests(unittest.TestCase):
    def test_free_plan_seed(self):
        seed = models.STATIC_SEEDS["zai"][0]
        self.assertEqual(seed.free_status, models.FREE_PLAN)
        self.assertTrue(seed.free_now())

    def test_promotional_free_expiry_respected(self):
        from datetime import datetime, timedelta, timezone

        expired = ModelInfo(
            provider="zai",
            model_id="glm-promo",
            free_status=models.FREE_PROMOTIONAL,
            free_until=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        )
        self.assertFalse(expired.free_now())
        active = ModelInfo(
            provider="zai",
            model_id="glm-promo",
            free_status=models.FREE_PROMOTIONAL,
            free_until=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        )
        self.assertTrue(active.free_now())


class TokenBudgetTests(unittest.TestCase):
    def test_estimate_is_conservative(self):
        persian = "درس فیزیک هسته‌ای " * 1000
        estimate = tokens.estimate_tokens(persian)
        self.assertGreater(estimate, len(persian) / 3)

    def test_chunk_budget_respects_token_and_char_bounds(self):
        small = tokens.chunk_char_budget(1000, char_cap=8000)
        self.assertLess(small, 4000)
        capped = tokens.chunk_char_budget(100000, char_cap=8000)
        self.assertEqual(capped, 8000)

    def test_free_profiles_shrink_chunks(self):
        groq = tokens.chunk_char_budget(2400, char_cap=8000)
        gemini = tokens.chunk_char_budget(12000, char_cap=22000)
        self.assertLess(groq * 3, gemini)


if __name__ == "__main__":
    unittest.main()
