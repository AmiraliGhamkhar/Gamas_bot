"""Coverage for the provider-platform follow-on work.

Focused on behaviour that only appears once a deployment is actually
configured, i.e. the parts the first platform pass deliberately left closed:

* account-entitlement attestation for providers whose free eligibility is a
  property of the account rather than of published documentation (Groq);
* administrator overrides for the extra pipeline passes (outline/repair/
  final compilation), which each spend free-tier quota;
* non-token metering (Cloudflare Neurons) — estimation, daily guard, and the
  ``requires_paid_billing`` model gate;
* migration 008 schema additions and the richer structured-log filters;
* the admin panels added for health and settings.

Everything runs offline with mocked providers; no real key is required.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from gamas_bot.ai.profiles import profile_for
from gamas_bot.ai.models import ModelInfo, ModelRegistry
from gamas_bot.ai.registry import PROVIDER_REGISTRY, registry_info
from gamas_bot.ai.routing import ProviderRouter, RouteLeg
from gamas_bot.ai.tokens import estimate_metered_units
from gamas_bot.ai.usage import AIUsageTracker
from gamas_bot.database import Database, utc_now
from gamas_bot.provider_credentials import ProviderCredentialManager

from support import make_settings


#: Source marker used by live discovery; only live rows are auto-deactivated
#: when they disappear from a catalog (static seeds are never pruned).
_LIVE = "live:/v1/models"


def _migrations_sql(name: str) -> str:
    return (
        Path(__file__).resolve().parent.parent / "migrations" / name
    ).read_text(encoding="utf-8")


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

    def _panels(self):
        from gamas_bot.admin_ai import AIPanels

        outer = self

        class _Bot:
            def __init__(self):
                self.rendered: list[str] = []

            @property
            def db(self):
                return outer.db

            @property
            def settings(self):
                return outer.settings

            @property
            def provider_router(self):
                return outer.router

            @property
            def credential_manager(self):
                return outer.manager

            health_checker = None

            async def _edit_callback(self, event, text, buttons=None):
                self.rendered.append(text)

        bot = _Bot()
        panels = AIPanels(bot)
        panels.bot = bot
        return panels


    async def _attest(self, slug: str, admin_id: int = 42, *, when: str | None = None):
        await self.db.ai_provider_settings_upsert(
            slug,
            admin_id=admin_id,
            account_entitlement_attested_at=when or utc_now(),
            account_entitlement_attested_by_admin_id=admin_id,
        )
        self.router.invalidate_cache()


class AccountEntitlementTests(PlatformCase):
    """Spec §11: an account-verified provider is opt-in, never silently free."""

    async def test_groq_requires_account_verification(self):
        self.assertTrue(registry_info("groq").requires_account_verification)

    async def test_groq_is_blocked_without_attestation(self):
        plan = await self.router.plan()
        reasons = dict(plan.skipped)
        self.assertEqual(reasons.get("groq"), "account_entitlement_unverified")
        self.assertNotIn("groq", [leg.canonical for leg in plan.legs])

    async def test_groq_is_eligible_after_attestation(self):
        await self._attest("groq")
        plan = await self.router.plan()
        self.assertIn("groq", [leg.canonical for leg in plan.legs])
        groq_leg = next(leg for leg in plan.legs if leg.canonical == "groq")
        self.assertTrue(groq_leg.account_verified)

    async def test_partial_attestation_is_not_evidence(self):
        # A timestamp without the attesting admin id is not an attestation.
        await self.db.ai_provider_settings_upsert(
            "groq", admin_id=42, account_entitlement_attested_at=utc_now()
        )
        self.router.invalidate_cache()
        plan = await self.router.plan()
        self.assertEqual(dict(plan.skipped).get("groq"), "account_entitlement_unverified")

        await self.db.ai_provider_settings_upsert(
            "groq",
            admin_id=42,
            account_entitlement_attested_at=None,
            account_entitlement_attested_by_admin_id=42,
        )
        self.router.invalidate_cache()
        plan = await self.router.plan()
        self.assertEqual(dict(plan.skipped).get("groq"), "account_entitlement_unverified")

    async def test_attestation_does_not_widen_other_providers(self):
        """The flag only applies to providers that declare the requirement."""
        declared = {
            slug for slug, info in PROVIDER_REGISTRY.items() if info.requires_account_verification
        }
        self.assertEqual(declared, {"groq"})
        # A provider without the requirement keeps its own classification gate:
        # NVIDIA hosted endpoints are trial-only and stay blocked regardless of
        # any provider setting an administrator can flip from the panel.
        await self.db.ai_provider_settings_upsert("nvidia", admin_id=7, enabled=1)
        self.router.invalidate_cache()
        plan = await self.router.plan()
        self.assertEqual(dict(plan.skipped).get("nvidia"), "trial_only_blocked")

    async def test_plan_reports_why_a_leg_was_skipped(self):
        plan = await self.router.plan()
        self.assertTrue(plan.skipped)
        for provider, reason in plan.skipped:
            self.assertIsInstance(provider, str)
            self.assertIsInstance(reason, str)


class ExtraPassOverrideTests(PlatformCase):
    """Spec §26: outline/repair/compile are per-provider and admin-overridable."""

    async def test_profile_default_when_no_override(self):
        plan = await self.router.plan()
        profile = plan.profile
        self.assertEqual(plan.outline_enabled, profile.outline_enabled)
        self.assertEqual(plan.repair_enabled, profile.repair_enabled)
        self.assertEqual(plan.compile_enabled, profile.final_compile_enabled)
        self.assertEqual(
            plan.pass_state()["outline"], (profile.outline_enabled, "profile")
        )

    async def test_admin_override_turns_a_pass_off_and_on(self):
        # The leading provider is the env-seeded NaraRouter, which enables all
        # three passes by default; switch outline off explicitly.
        plan = await self.router.plan()
        primary = plan.legs[0].canonical
        await self.db.ai_provider_settings_upsert(
            primary, admin_id=42, outline_enabled=0
        )
        self.router.invalidate_cache()
        plan = await self.router.plan()
        self.assertFalse(plan.outline_enabled)
        self.assertEqual(plan.pass_state()["outline"], (False, "admin"))
        # The other passes keep their profile defaults.
        self.assertEqual(plan.repair_enabled, profile_for(primary).repair_enabled)

    async def test_override_applies_to_the_leading_provider_only(self):
        plan = await self.router.plan()
        primary = plan.legs[0].canonical
        await self.db.ai_provider_settings_upsert(primary, admin_id=42, repair_enabled=0)
        self.router.invalidate_cache()
        plan = await self.router.plan()
        self.assertFalse(plan.repair_enabled)
        self.assertEqual(plan.legs[0].canonical, primary)

    async def test_null_override_means_inherit(self):
        plan = await self.router.plan()
        primary = plan.legs[0].canonical
        await self.db.ai_provider_settings_upsert(
            primary, admin_id=42, final_compile_enabled=None
        )
        self.router.invalidate_cache()
        plan = await self.router.plan()
        self.assertEqual(plan.compile_enabled, profile_for(primary).final_compile_enabled)
        self.assertEqual(plan.pass_state()["final_compile"][1], "profile")

    async def test_free_providers_still_default_their_passes_off(self):
        """Groq/OpenRouter free profiles keep the extra passes disabled."""
        self.assertFalse(profile_for("groq").outline_enabled)
        self.assertFalse(profile_for("groq").repair_enabled)
        self.assertFalse(profile_for("groq").final_compile_enabled)
        self.assertFalse(profile_for("openrouter").outline_enabled)


class MeteredUnitTests(unittest.TestCase):
    """Spec §17: Neurons are estimated locally and never under-counted."""

    def test_token_metered_provider_has_no_units(self):
        self.assertIsNone(estimate_metered_units(metering_unit="", units_per_1k_tokens=25.0, input_tokens=1000))

    def test_estimate_scales_with_tokens(self):
        units = estimate_metered_units(
            metering_unit="neurons",
            units_per_1k_tokens=25.0,
            input_tokens=1000,
            output_tokens=1000,
        )
        self.assertEqual(units, 50)

    def test_estimate_rounds_up(self):
        # Never round down: a partial unit still consumes budget.
        self.assertEqual(
            estimate_metered_units(
                metering_unit="neurons", units_per_1k_tokens=25.0, input_tokens=1
            ),
            1,
        )

    def test_zero_tokens_costs_nothing(self):
        self.assertEqual(
            estimate_metered_units(
                metering_unit="neurons", units_per_1k_tokens=25.0, input_tokens=0
            ),
            0,
        )

    def test_cloudflare_profile_is_metered(self):
        profile = profile_for("cloudflare")
        self.assertEqual(profile.metering_unit, "neurons")
        self.assertGreater(profile.metering_units_per_1k_tokens, 0)
        self.assertEqual(profile.included_units_per_day, 10_000)
        self.assertEqual(registry_info("cloudflare").included_units_per_day, 10_000)

    def test_only_cloudflare_is_metered_today(self):
        metered = {
            slug: info.metering_unit
            for slug, info in PROVIDER_REGISTRY.items()
            if info.metering_unit
        }
        self.assertEqual(metered, {"cloudflare": "neurons"})


class NeuronBudgetTests(PlatformCase):
    async def test_daily_neuron_guard_blocks_when_exhausted(self):
        profile = profile_for("cloudflare")
        self.assertFalse(await self.router._metered_units_exhausted("cloudflare", profile))

        # Record enough estimated neurons to consume the whole allocation.
        await self.tracker.record(
            {
                "service": "notes",
                "provider": "cloudflare",
                "canonical": "cloudflare",
                "model": "@cf/meta/llama-3.1-8b-instruct",
                "request_type": "chunk_structuring",
                "result": "success",
                "latency_ms": 10,
                "actual_input_tokens": 400_000,
                "actual_output_tokens": 0,
                "neurons_estimated": 10_000,
            }
        )
        self.assertTrue(await self.router._metered_units_exhausted("cloudflare", profile))

    async def test_admin_budget_override_is_honoured(self):
        await self.db.ai_provider_settings_upsert("cloudflare", admin_id=42, neuron_budget_daily=5)
        self.router.invalidate_cache()
        await self.tracker.record(
            {
                "service": "notes",
                "provider": "cloudflare",
                "canonical": "cloudflare",
                "model": "m",
                "request_type": "chunk_structuring",
                "result": "success",
                "latency_ms": 5,
                "neurons_estimated": 6,
            }
        )
        profile = profile_for("cloudflare")
        self.assertTrue(await self.router._metered_units_exhausted("cloudflare", profile))

    async def test_token_metered_provider_ignores_the_unit_guard(self):
        self.assertFalse(
            await self.router._metered_units_exhausted("nara", profile_for("nara"))
        )

    async def test_neuron_rollup_accumulates(self):
        for _ in range(3):
            await self.tracker.record(
                {
                    "service": "notes",
                    "provider": "cloudflare",
                    "canonical": "cloudflare",
                    "model": "m",
                    "request_type": "chunk_structuring",
                    "result": "success",
                    "latency_ms": 1,
                    "neurons_estimated": 7,
                }
            )
        self.assertEqual(await self.db.ai_usage_today_units("cloudflare"), 21)

    async def test_requires_paid_billing_model_is_rejected_in_free_only(self):
        from gamas_bot.ai.models import ModelCapabilities, ModelInfo

        info = ModelInfo(
            provider="cloudflare",
            model_id="@cf/frontier/paid-only",
            capabilities=ModelCapabilities(requires_paid_billing=True),
        )
        leg = RouteLeg(provider="cloudflare", canonical="cloudflare", free_only=True)
        self.assertEqual(
            self.router._model_blocked(leg, info), "model requires paid billing (FREE_ONLY)"
        )
        # The same model on a deliberately paid leg is allowed through here;
        # whether that leg runs at all is governed by AI_ALLOW_PAID_FALLBACK.
        paid_leg = RouteLeg(provider="cloudflare", canonical="cloudflare", free_only=False)
        self.assertIsNone(self.router._model_blocked(paid_leg, info))

    async def test_requires_paid_billing_flag_round_trips(self):
        await self.models.apply_discovery(
            "cloudflare", [ModelInfo(provider="cloudflare", model_id="@cf/test/model")]
        )
        await self.db.ai_model_set_requires_paid_billing(
            "cloudflare", "@cf/test/model", required=True
        )
        rows = await self.db.ai_models_list("cloudflare")
        row = next(r for r in rows if r["model"] == "@cf/test/model")
        capabilities = row["capabilities_json"]
        if isinstance(capabilities, str):
            capabilities = json.loads(capabilities)
        self.assertTrue(capabilities["requires_paid_billing"])
        await self.db.ai_model_set_requires_paid_billing(
            "cloudflare", "@cf/test/model", required=False
        )
        rows = await self.db.ai_models_list("cloudflare")
        row = next(r for r in rows if r["model"] == "@cf/test/model")
        capabilities = row["capabilities_json"]
        if isinstance(capabilities, str):
            capabilities = json.loads(capabilities)
        self.assertFalse(capabilities["requires_paid_billing"])


class Migration008Tests(unittest.TestCase):
    def test_adds_expected_columns(self):
        script = _migrations_sql("008_provider_entitlement_and_pass_policy.sql")
        for column in (
            "account_entitlement_attested_at",
            "account_entitlement_attested_by_admin_id",
            "outline_enabled",
            "repair_enabled",
            "final_compile_enabled",
            "neuron_budget_daily",
            "region",
            "deployment_scope",
            "quota_expires_at",
        ):
            self.assertIn(column, script, column)
        self.assertIn("neurons_estimated", script)

    def test_is_forward_only(self):
        script = _migrations_sql("008_provider_entitlement_and_pass_policy.sql")
        upper = script.upper()
        for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
            self.assertNotIn(forbidden, upper, forbidden)
        # ALTER TABLE is allowed only as ADD COLUMN.
        from gamas_bot.database import split_sql_statements

        for statement in split_sql_statements(script):
            normalized = statement.upper()
            if "ALTER TABLE" in normalized:
                self.assertIn("ADD COLUMN", normalized, statement)

class Migration008ApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_applies_to_a_real_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.sqlite3")
            await db.open()
            try:
                await db.ai_provider_settings_upsert(
                    "cloudflare", admin_id=1, neuron_budget_daily=1234
                )
                row = await db.ai_provider_settings_get("cloudflare")
                self.assertEqual(row["neuron_budget_daily"], 1234)
                self.assertEqual(await db.ai_usage_today_units("cloudflare"), 0)
            finally:
                await db.close()


class LogFilterTests(PlatformCase):
    """Spec §35: logs filter by status class, error type, job and date."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        now = utc_now()
        for index, (event, status, error_class) in enumerate(
            [
                ("note_request_failed", 502, "server"),
                ("note_request_failed", 429, "rate_limited"),
                ("note_request_succeeded", 200, None),
                ("provider_rate_limited", 429, "rate_limited"),
            ]
        ):
            await self.db.ai_event_insert(
                {
                    "event": event,
                    "service": "notes",
                    "provider": "groq",
                    "canonical": "groq",
                    "model": "openai/gpt-oss-20b",
                    "request_type": "chunk_structuring",
                    "http_status": status,
                    "error_class": error_class,
                    "job_id": "GMS-1" if index < 2 else "GMS-2",
                    "created_at": now,
                }
            )

    async def test_status_class_filter(self):
        server = await self.db.ai_events_list(http_status=5)
        self.assertEqual(len(server), 1)
        self.assertEqual(server[0]["http_status"], 502)
        client = await self.db.ai_events_list(http_status=4)
        self.assertEqual(len(client), 2)

    async def test_exact_status_filter(self):
        rows = await self.db.ai_events_list(http_status=502)
        self.assertEqual(len(rows), 1)

    async def test_error_class_filter(self):
        rows = await self.db.ai_events_list(error_class="rate_limited")
        self.assertEqual(len(rows), 2)

    async def test_job_filter(self):
        rows = await self.db.ai_events_list(job_id="GMS-2")
        self.assertEqual(len(rows), 2)

    async def test_model_and_request_type_filters(self):
        self.assertEqual(len(await self.db.ai_events_list(model="openai/gpt-oss-20b")), 4)
        self.assertEqual(
            len(await self.db.ai_events_list(request_type="chunk_structuring")), 4
        )
        self.assertEqual(len(await self.db.ai_events_list(model="other")), 0)

    async def test_date_filter_excludes_nothing_today(self):
        from datetime import timedelta

        since = (
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            - timedelta(days=1)
        ).isoformat(timespec="seconds")
        rows = await self.db.ai_events_list(since=since)
        self.assertEqual(len(rows), 4)
        future = (
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            + timedelta(days=1)
        ).isoformat(timespec="seconds")
        self.assertEqual(len(await self.db.ai_events_list(since=future)), 0)


class AdminPanelWiringTests(PlatformCase):
    """The new panels and toggles are reachable and audited."""

    async def test_entitlement_toggle_is_audited(self):
        ui = self._panels()

        class _Event:
            async def answer(self, *args, **kwargs):
                return None

            async def get_sender(self):
                class _Sender:
                    id = 42

                return _Sender()

        event = _Event()
        await ui._toggle_account_entitlement(event, "groq")
        row = await self.db.ai_provider_settings_get("groq")
        self.assertTrue(row["account_entitlement_attested_at"])
        self.assertEqual(row["account_entitlement_attested_by_admin_id"], 42)
        # Toggling again revokes it.
        await ui._toggle_account_entitlement(event, "groq")
        row = await self.db.ai_provider_settings_get("groq")
        self.assertIsNone(row["account_entitlement_attested_at"])
        self.assertIsNone(row["account_entitlement_attested_by_admin_id"])

    async def test_entitlement_toggle_rejects_other_providers(self):
        ui = self._panels()

        class _Event:
            def __init__(self):
                self.answered = []

            async def answer(self, text, alert=False):
                self.answered.append(text)

        event = _Event()
        await ui._toggle_account_entitlement(event, "nara")
        self.assertTrue(event.answered)
        row = await self.db.ai_provider_settings_get("nara")
        self.assertFalse(row and row.get("account_entitlement_attested_at"))

    async def test_pass_toggle_cycles_inherit_off_on(self):
        ui = self._panels()

        class _Event:
            async def answer(self, *args, **kwargs):
                return None

            async def get_sender(self):
                class _Sender:
                    id = 42

                return _Sender()

        event = _Event()
        profile = profile_for("groq")
        # Groq defaults all three passes off, so the first explicit step is "on".
        await ui._toggle_pass(event, "outline_enabled", "groq")
        row = await self.db.ai_provider_settings_get("groq")
        self.assertEqual(bool(row["outline_enabled"]), not profile.outline_enabled)
        # Second step sets the profile default explicitly; third returns to
        # inherit, so an admin can always undo an override completely.
        await ui._toggle_pass(event, "outline_enabled", "groq")
        row = await self.db.ai_provider_settings_get("groq")
        self.assertEqual(bool(row["outline_enabled"]), profile.outline_enabled)
        await ui._toggle_pass(event, "outline_enabled", "groq")
        row = await self.db.ai_provider_settings_get("groq")
        self.assertIsNone(row["outline_enabled"])

    async def test_pass_override_changes_the_planned_policy(self):
        plan = await self.router.plan()
        primary = plan.legs[0].canonical
        await self.db.ai_provider_settings_upsert(
            primary, admin_id=42, outline_enabled=0, repair_enabled=0
        )
        self.router.invalidate_cache()
        plan = await self.router.plan()
        self.assertFalse(plan.outline_enabled)
        self.assertFalse(plan.repair_enabled)

    async def test_health_panel_runs_without_a_checker(self):
        ui = self._panels()

        class _Edit:
            def __init__(self):
                self.text = ""

        class _Event:
            async def get_sender(self):
                class _Sender:
                    id = 42

                return _Sender()

        await ui.show_health(_Event())
        self.assertIn("سلامت", ui.bot.rendered[-1])

    async def test_settings_panel_reports_free_only_state(self):
        ui = self._panels()

        class _Event:
            async def get_sender(self):
                class _Sender:
                    id = 42

                return _Sender()

        await ui.show_settings(_Event())
        self.assertIn("FREE_ONLY", ui.bot.rendered[-1])
        self.assertIn("Fallback پولی", ui.bot.rendered[-1])

    async def test_logs_panel_status_filters_are_wired(self):
        from gamas_bot.admin_ai import AIPanels

        self.assertTrue(callable(AIPanels._log_filter))
        ui = self._panels()

        class _Event:
            async def get_sender(self):
                class _Sender:
                    id = 42

                return _Sender()

        await ui.show_logs(_Event(), status_class=5)
        self.assertIn("5xx", ui.bot.rendered[-1])
        await ui.show_logs(_Event(), days=1)
        self.assertIn("1 روز", ui.bot.rendered[-1])


class NoSecretOrContentLeakTests(PlatformCase):
    """The new fields must not smuggle content into the ledger."""

    async def test_neuron_estimate_is_metadata_only(self):
        await self.tracker.record(
            {
                "service": "notes",
                "provider": "cloudflare",
                "canonical": "cloudflare",
                "model": "m",
                "request_type": "chunk_structuring",
                "result": "success",
                "latency_ms": 1,
                "neurons_estimated": 42,
            }
        )
        rows = await self.db.ai_events_list()
        blob = json.dumps(rows, default=str)
        self.assertNotIn("transcript", blob.lower())


if __name__ == "__main__":
    unittest.main()


class NvidiaEndpointLifecycleTests(PlatformCase):
    """Spec §16: free endpoints are live capabilities, and dead models get a
    suggested replacement instead of an unexplained failover."""

    async def test_free_endpoint_round_trips(self):
        await self.models.apply_discovery(
            "nvidia",
            [
                ModelInfo(
                    provider="nvidia",
                    model_id="meta/llama-3.1-8b-instruct",
                    free_endpoint=True,
                )
            ],
        )
        rows = await self.db.ai_models_list("nvidia")
        self.assertTrue(rows[0]["free_endpoint"])
        info = await self.models.resolve("nvidia", "meta/llama-3.1-8b-instruct")
        self.assertTrue(info.free_endpoint)

    async def test_defaults_to_not_free_endpoint(self):
        info = ModelInfo(provider="nvidia", model_id="some/model")
        self.assertFalse(info.free_endpoint)

    async def test_missing_model_is_deactivated_not_deleted(self):
        await self.models.apply_discovery(
            "nvidia",
            [
                ModelInfo(provider="nvidia", model_id="a/one", source=_LIVE),
                ModelInfo(provider="nvidia", model_id="a/two", source=_LIVE),
            ],
        )
        # A later sync that no longer lists "a/two" marks it unavailable while
        # keeping the historical row (and any usage attached to it).
        result = await self.models.apply_discovery(
            "nvidia", [ModelInfo(provider="nvidia", model_id="a/one", source=_LIVE)]
        )
        self.assertEqual(result["deactivated"], 1)
        all_rows = await self.db.ai_models_list("nvidia", include_unavailable=True)
        self.assertEqual(len(all_rows), 2)
        self.assertEqual(
            len(await self.db.ai_models_list("nvidia", include_unavailable=False)), 1
        )

    async def test_free_endpoint_is_latched_until_withdrawn(self):
        """A sync that omits the flag must not silently clear a known fact."""
        await self.models.apply_discovery(
            "nvidia",
            [ModelInfo(provider="nvidia", model_id="a/one", free_endpoint=True, source=_LIVE)],
        )
        await self.models.apply_discovery(
            "nvidia", [ModelInfo(provider="nvidia", model_id="a/one", source=_LIVE)]
        )
        info = await self.models.resolve("nvidia", "a/one")
        self.assertTrue(info.free_endpoint)

    def test_replacement_prefers_a_live_free_endpoint(self):
        from gamas_bot.ai.models import suggest_replacement

        from gamas_bot.ai.models import FREE_PLAN, PAID

        catalog = [
            ModelInfo(provider="nvidia", model_id="old/deprecated", deprecated=True),
            ModelInfo(provider="nvidia", model_id="paid/model", free_status=PAID),
            ModelInfo(provider="nvidia", model_id="free/model", free_endpoint=True, free_status=FREE_PLAN),
            ModelInfo(provider="nvidia", model_id="gone/model", available=False),
        ]
        suggestion = suggest_replacement(catalog, "old/deprecated")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion.model_id, "free/model")

    def test_replacement_never_returns_the_same_model(self):
        from gamas_bot.ai.models import suggest_replacement

        catalog = [ModelInfo(provider="nvidia", model_id="only/one")]
        self.assertIsNone(suggest_replacement(catalog, "only/one"))

    def test_replacement_skips_deprecated_and_unavailable(self):
        from gamas_bot.ai.models import suggest_replacement

        catalog = [
            ModelInfo(provider="nvidia", model_id="dep/one", deprecated=True),
            ModelInfo(provider="nvidia", model_id="gone/one", available=False),
        ]
        self.assertIsNone(suggest_replacement(catalog, "old"))

    async def test_deprecated_model_is_rejected_by_the_router(self):
        from gamas_bot.ai.models import ModelCapabilities

        info = ModelInfo(
            provider="nvidia",
            model_id="dep/one",
            deprecated=True,
            capabilities=ModelCapabilities(supports_text=True),
        )
        leg = RouteLeg(provider="nvidia", canonical="nvidia", free_only=True)
        self.assertIn("deprecated", self.router._model_blocked(leg, info) or "")


class RouterEndToEndTests(PlatformCase):
    """A realistic plan: the route is filtered, explained, and auditable."""

    async def test_plan_never_includes_trial_or_terms_blocked_providers(self):
        plan = await self.router.plan()
        blocked = {"nvidia", "cerebras", "cohere", "huggingface", "zai"}
        for leg in plan.legs:
            self.assertNotIn(leg.canonical, blocked)
        reasons = dict(plan.skipped)
        self.assertIn("nvidia", reasons)
        self.assertIn("zai", reasons)

    async def test_free_only_legs_come_before_paid_legs(self):
        """Paid fallback can never be promoted above a free provider."""
        plan = await self.router.plan()
        seen_paid = False
        for leg in plan.legs:
            if not leg.free_only:
                seen_paid = True
            elif seen_paid:
                self.fail("a free leg was ordered after a paid leg")

    async def test_env_deployment_keeps_its_provider_first(self):
        plan = await self.router.plan()
        self.assertEqual(plan.legs[0].canonical, "nara")

    async def test_pass_policy_is_reported_with_origin(self):
        plan = await self.router.plan()
        state = plan.pass_state()
        self.assertIn("outline", state)
        self.assertIn("repair", state)
        self.assertIn("final_compile", state)
        for _key, (enabled, origin) in state.items():
            self.assertIsInstance(enabled, bool)
            self.assertIn(origin, {"admin", "profile"})


class NoSecretInNewPanelsTests(PlatformCase):
    async def test_provider_detail_never_renders_a_secret(self):
        await self.manager.add_credential(
            service="notes",
            provider="groq",
            label="groq-main",
            secret="sk-groq-SUPERSECRET-9999",
            model="qwen/qwen3.8-27b",
            admin_id=42,
        )
        ui = self._panels()

        class _Event:
            async def get_sender(self):
                class _Sender:
                    id = 42

                return _Sender()

        await ui.show_provider_detail(_Event(), "groq")
        rendered = ui.bot.rendered[-1]
        self.assertNotIn("SUPERSECRET", rendered)
        self.assertNotIn("sk-groq", rendered)


class JobSessionPassPolicyTests(PlatformCase):
    """Regression: ``structuring._pass_allowed`` reads the *session*, not the
    plan, so the session must expose the effective policy (spec §26)."""

    async def test_session_proxies_the_planned_pass_policy(self):
        from gamas_bot.ai.routing import NoteJobSession

        plan = await self.router.plan()
        session = NoteJobSession(self.router, plan, self.settings, job_id="GMS-1")
        self.assertEqual(session.outline_enabled, plan.outline_enabled)
        self.assertEqual(session.repair_enabled, plan.repair_enabled)
        self.assertEqual(session.compile_enabled, plan.compile_enabled)

    async def test_admin_override_reaches_the_session(self):
        from gamas_bot.ai.routing import NoteJobSession

        plan = await self.router.plan()
        primary = plan.legs[0].canonical
        await self.db.ai_provider_settings_upsert(primary, admin_id=42, repair_enabled=0)
        self.router.invalidate_cache()
        plan = await self.router.plan()
        session = NoteJobSession(self.router, plan, self.settings, job_id="GMS-2")
        self.assertFalse(session.repair_enabled)
        self.assertTrue(plan.repair_enabled is False)

    async def test_structuring_pass_gate_reads_the_session_without_error(self):
        """``_pass_allowed`` must never raise AttributeError on a bound session."""
        from gamas_bot.ai.routing import NoteJobSession, job_session_scope
        from gamas_bot.structuring import _pass_allowed

        plan = await self.router.plan()
        session = NoteJobSession(self.router, plan, self.settings, job_id="GMS-3")
        with job_session_scope(session):
            for kind in ("outline", "repair", "compile"):
                self.assertIsInstance(_pass_allowed(kind), bool)
