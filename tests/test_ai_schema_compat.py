"""Schema-compat gate: freezes the request shapes the platform emits.

The builder comparison against the historical `_provider_request` payload proves
backward compatibility for the legacy providers; the per-adapter strategy
checks prove the capability-aware enhancements of spec §8.2 land where intended
(and stay within a documented delta — e.g. Gemini gains `responseSchema`).
"""

from __future__ import annotations

import pathlib
import unittest

from gamas_bot.ai.adapters import (
    STRATEGY_JSON_OBJECT,
    STRATEGY_PROMPT,
    adapter_for,
)
from gamas_bot.ai.schema import gemini_compat, note_json_schema, strict_note_json_schema
from gamas_bot.structuring import _provider_request

from support import make_settings

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _settings(**kw):
    """Settings with an OpenAI-compatible note provider (the legacy baseline)."""

    base = {
        "note_api_provider": "openai_compatible",
        "note_api_base_url": "https://router.bynara.id/v1",
        "note_api_model": "agnes-3-flash",
        "note_api_key": "sk-legacy-key",
        "note_api_max_output_tokens": 4096,
    }
    base.update(kw)
    return make_settings(**base)


def _ctx(provider: str, model: str, **kw):
    from test_provider_adapters import make_ctx

    return make_ctx(provider, model, **kw)


class LegacyPayloadFreezeTests(unittest.TestCase):
    def setUp(self):
        max_tokens = 4096
        self.settings = _settings()
        self.max_tokens = max_tokens

    def test_openai_compatible_shape_is_stable(self):
        url, headers, payload, _extra = _provider_request(
            "متن درس", self.settings, "", system_prompt="SYS"
        )
        self.assertEqual(self.settings.note_api_provider, "openai_compatible")
        adapter = adapter_for("openai_compatible", self.settings)
        request = adapter.build(
            _ctx(
                "openai_compatible",
                self.settings.effective_note_model,
                base_url=self.settings.note_api_base_url,
                max_output_tokens=self.max_tokens,
                system_prompt="SYS",
            ),
            self.settings.effective_note_api_key,
        )
        self.assertEqual(request.url, url)
        self.assertEqual(request.headers, headers)
        self.assertEqual(request.json_body["model"], payload["model"])
        self.assertEqual(request.json_body["messages"], payload["messages"])
        self.assertEqual(request.json_body["temperature"], payload["temperature"])
        self.assertEqual(request.json_body["max_tokens"], payload["max_tokens"])

    def test_nararouter_shape_preserved(self):
        """The production deployment (NOTE_API_* pointed at NaraRouter) is stable."""
        settings = _settings(note_api_key="sk-nara-key")
        legacy_url, legacy_headers, legacy_payload, _ = _provider_request(
            "متن", settings, "", system_prompt="SYS"
        )
        adapter = adapter_for("nara", settings)
        request = adapter.build(
            _ctx(
                "openai_compatible", "agnes-3-flash",
                base_url="https://router.bynara.id/v1",
                max_output_tokens=self.max_tokens,
                user_text="متن", system_prompt="SYS",
            ),
            "sk-nara-key",
        )
        self.assertEqual(request.url, legacy_url)
        self.assertEqual(request.headers, legacy_headers)
        self.assertEqual(request.json_body["model"], legacy_payload["model"])
        self.assertEqual(request.json_body["messages"], legacy_payload["messages"])
        self.assertEqual(request.json_body["temperature"], legacy_payload["temperature"])
        self.assertEqual(request.json_body["max_tokens"], legacy_payload["max_tokens"])

    def test_note_api_json_mode_kept_for_nara(self):
        settings = _settings(
            note_api_key="sk-nara-key",
            note_api_json_mode=True,
        )
        adapter = adapter_for("nara", settings)
        request = adapter.build(
            _ctx("openai_compatible", "agnes-3-flash",
                 base_url="https://router.bynara.id/v1",
                 json_strategy=STRATEGY_JSON_OBJECT),
            "sk-nara-key",
        )
        self.assertEqual(
            request.json_body.get("response_format"), {"type": "json_object"}
        )

    def test_gemini_shape_stable_plus_schema(self):
        settings = _settings(
            note_api_provider="gemini",
            note_api_model="gemini-2.5-flash",
            note_api_base_url="",
            note_api_key="AIza-key",
        )
        legacy_url, legacy_headers, legacy_payload, _ = _provider_request(
            "متن", settings, "", system_prompt="SYS"
        )
        adapter = adapter_for("gemini", settings)
        strategy = adapter.resolve_gemini_strategy() if False else None
        request_ctx = _ctx("gemini", "gemini-2.5-flash", base_url="",
                 max_output_tokens=self.max_tokens, user_text="متن",
                 system_prompt="SYS")
        request = adapter.build(request_ctx, "AIza-key")
        self.assertEqual(request.url, legacy_url)
        self.assertEqual(request.headers, legacy_headers)
        for key, value in legacy_payload.items():
            if key == "generationConfig":
                continue
            self.assertEqual(request.json_body[key], value, key)
        from gamas_bot.ai.adapters import STRATEGY_GEMINI_MIME, resolve_json_strategy
        from gamas_bot.ai.models import ModelCapabilities, ModelInfo
        from dataclasses import replace

        # With no advertised schema capability the router must keep the exact
        # historical Gemini payload ("responseMimeType" only).
        strategy = resolve_json_strategy(
            adapter,
            ModelInfo(provider="gemini", model_id="gemini-2.5-flash",
                      capabilities=ModelCapabilities()),
        )
        self.assertEqual(strategy, STRATEGY_GEMINI_MIME)
        legacy_cfg = legacy_payload["generationConfig"]
        bare = adapter.build(
            replace(request_ctx, json_strategy=STRATEGY_GEMINI_MIME), "AIza-key"
        )
        bare_cfg = bare.json_body["generationConfig"]
        self.assertEqual(set(bare_cfg), set(legacy_cfg))
        # Documented delta (spec §8.2): with modern structured-output strategy
        # the platform *adds* native schema output on top of the stable base.
        from gamas_bot.ai.adapters import STRATEGY_GEMINI_SCHEMA

        schema_req = adapter.build(
            replace(request_ctx, json_strategy=STRATEGY_GEMINI_SCHEMA), "AIza-key"
        )
        schema_cfg = schema_req.json_body["generationConfig"]
        for key, value in legacy_cfg.items():
            self.assertEqual(schema_cfg[key], value, key)
        self.assertEqual(schema_cfg["responseSchema"], gemini_compat(note_json_schema()))

    def test_gemini_schema_is_cleaned_for_api(self):
        schema = gemini_compat(note_json_schema())
        self.assertNotIn("$schema", schema)
        self.assertNotIn("required", schema.get("properties", {}).get("sections", {}))
        sections = schema["properties"]["sections"]["items"]
        self.assertNotIn("required", sections)
        self.assertIn("term", sections["properties"]["definitions"]["items"]["properties"])

    def test_strict_schema_requires_every_property(self):
        strict = strict_note_json_schema()
        sections = strict["properties"]["sections"]
        self.assertEqual(
            set(strict["required"]), set(strict["properties"].keys())
        )
        items = sections["items"]
        self.assertEqual(set(items["required"]), set(items["properties"].keys()))
        self.assertFalse(items.get("additionalProperties", True) and True)


class StrategyMatrixTests(unittest.TestCase):
    def setUp(self):
        self.settings = _settings()

    def test_groq_profile_output_budget_matches_free_tier(self):
        """Groq's free tier silently returns 400 above its completion cap; the
        platform's profile is the single source of truth for the clamp, and the
        default routing model never exceeds it (the router materialises
        ``max_output_tokens`` from the profile — see profiles.py)."""
        from gamas_bot.ai.profiles import profile_for

        profile = profile_for("groq")
        self.assertEqual(profile.max_output_tokens, 4096)
        ctx = _ctx("groq", "openai/gpt-oss-120b",
                   max_output_tokens=profile.max_output_tokens)
        request = adapter_for("groq", self.settings).build(ctx, "gsk-key")
        self.assertEqual(request.json_body["max_tokens"], 4096)

    def test_prompt_strategy_omits_response_format(self):
        ctx = _ctx("mistral", "mistral-small-latest", json_strategy=STRATEGY_PROMPT)
        request = adapter_for("mistral", self.settings).build(ctx, "m-key")
        self.assertNotIn("response_format", request.json_body)

    def test_zai_strict_schema_flag(self):
        ctx = _ctx("zai", "glm-4.5-flash")
        adapter = adapter_for("zai", self.settings)
        request = adapter.build(ctx, "zai-key")
        if adapter.supports_strict_schema:
            response_format = request.json_body.get("response_format") or {}
            self.assertEqual(response_format.get("type"), "json_schema")
        else:
            # GLM rejects ``strict: true``; prompt-mode JSON keeps working.
            self.assertNotIn("response_format", request.json_body)

    def test_redacted_request_never_carries_text(self):
        ctx = _ctx("gemini", "gemini-2.5-flash", base_url="")
        request = adapter_for("gemini", self.settings).build(ctx, "AIza-SECRET-0000")
        redacted = request.redacted()
        blob = repr(redacted)
        self.assertNotIn("AIza-SECRET-0000", blob)
        self.assertNotIn("متن درس", blob)


class MigrationKeysTests(unittest.TestCase):
    def test_migration_006_creates_all_ai_tables(self):
        sql = (ROOT / "migrations" / "006_ai_provider_platform.sql").read_text(encoding="utf-8")
        for table in (
            "ai_models",
            "ai_provider_settings",
            "ai_provider_routes",
            "ai_usage_records",
            "ai_usage_daily",
            "ai_quota_snapshots",
            "ai_events",
        ):
            self.assertIn(table, sql)
        for column in ("key_type", "free_only", "failure_streak", "billing_state"):
            self.assertIn(column, sql)


    def test_migration_007_adds_forward_only_billing_attestations(self):
        sql = (ROOT / "migrations" / "007_provider_billing_attestations.sql").read_text(encoding="utf-8")
        self.assertIn("ALTER TABLE provider_credentials ADD COLUMN billing_attested_at TEXT", sql)
        self.assertIn("billing_attested_by_admin_id INTEGER", sql)
        self.assertIn("SET billing_state='unknown'", sql)


if __name__ == "__main__":
    unittest.main()
