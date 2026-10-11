-- Provider-platform follow-on: account-entitlement attestation, per-provider
-- extra-pass overrides, Cloudflare neuron accounting and region/quota metadata.
-- Forward-only and non-destructive: only ADD COLUMNs and new indexes. No
-- existing row is rewritten except the documented fail-closed reset below.

-- ---------------------------------------------------------------------------
-- ai_provider_settings: deployment-scoped provider policy
-- ---------------------------------------------------------------------------

-- Account-entitlement attestation (spec §11 / §22).
-- Some providers publish rate limits but no documented no-charge production
-- entitlement. Gamas cannot verify an account's billing state from
-- documentation, so FREE_ONLY routing for these providers stays blocked until
-- an administrator explicitly attests that this deployment's account is a
-- no-charge / no-overage account. NULL = not attested = blocked.
ALTER TABLE ai_provider_settings ADD COLUMN account_entitlement_attested_at TEXT;
ALTER TABLE ai_provider_settings ADD COLUMN account_entitlement_attested_by_admin_id INTEGER;

-- Per-provider extra-pass overrides (spec §26). NULL = inherit the provider
-- profile default; 0/1 = an explicit administrator decision. These gate the
-- outline, QA-repair and final-compilation calls, which each consume free-tier
-- quota on top of the chunk calls.
ALTER TABLE ai_provider_settings ADD COLUMN outline_enabled INTEGER;
ALTER TABLE ai_provider_settings ADD COLUMN repair_enabled INTEGER;
ALTER TABLE ai_provider_settings ADD COLUMN final_compile_enabled INTEGER;

-- Cloudflare Workers AI neuron budget (spec §17). NULL = use the registry
-- default (10,000 Neurons/day, last verified 2026-10-09). Stored per
-- deployment because the included allocation is plan/account scoped.
ALTER TABLE ai_provider_settings ADD COLUMN neuron_budget_daily INTEGER;

-- Region-restricted provider metadata (spec §19): where the deployment runs,
-- how broadly it is enabled, and when a promotional/region quota expires.
ALTER TABLE ai_provider_settings ADD COLUMN region TEXT;
ALTER TABLE ai_provider_settings ADD COLUMN deployment_scope TEXT;
ALTER TABLE ai_provider_settings ADD COLUMN quota_expires_at TEXT;

-- NVIDIA "Free Endpoint" availability (spec §16). Hosted developer endpoints
-- come and go per model, so free availability is a live capability rather than
-- a permanent guarantee: 0 = not free / unknown, 1 = observed free.
ALTER TABLE ai_models ADD COLUMN free_endpoint INTEGER NOT NULL DEFAULT 0
    CHECK (free_endpoint IN (0,1));

-- ---------------------------------------------------------------------------
-- Neuron accounting (spec §17)
-- ---------------------------------------------------------------------------
-- Cloudflare meters inference in Neurons, not tokens, and does not return
-- neuron counts in API responses. Gamas stores an explicit *estimate* derived
-- from reviewed per-model neuron rates so the daily free allocation can be
-- protected locally. Estimate rows are always labelled as estimates in the UI.
ALTER TABLE ai_usage_records ADD COLUMN neurons_estimated INTEGER;
ALTER TABLE ai_usage_daily ADD COLUMN neurons_estimated INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_ai_provider_settings_enabled
    ON ai_provider_settings(enabled);

-- Any deployment that enabled a region-restricted or account-verified provider
-- before attestation existed must re-confirm it explicitly. This is the same
-- fail-closed posture migration 007 applied to credentials: nothing that was
-- never reviewed is silently trusted.
UPDATE ai_provider_settings
SET account_entitlement_attested_at = NULL,
    account_entitlement_attested_by_admin_id = NULL;
