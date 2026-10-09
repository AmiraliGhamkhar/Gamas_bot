-- Persist the reviewed provider registry for admin visibility and auditability.
-- Forward-only and additive: this creates a new table without rewriting any
-- provider credentials, routes, model catalog rows, or usage history.

CREATE TABLE IF NOT EXISTS ai_provider_registry (
    provider TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    classification TEXT NOT NULL,
    protocol TEXT NOT NULL,
    base_url TEXT,
    auth_style TEXT NOT NULL,
    docs_url TEXT NOT NULL,
    pricing_url TEXT NOT NULL,
    data_use_policy TEXT NOT NULL,
    commercial_use_allowed INTEGER NOT NULL CHECK (commercial_use_allowed IN (0,1)),
    free_tier_policy TEXT NOT NULL,
    supported_services_json TEXT NOT NULL DEFAULT '[]',
    generation_allowed_in_free_only INTEGER NOT NULL CHECK (generation_allowed_in_free_only IN (0,1)),
    quota_can_become_paid INTEGER NOT NULL CHECK (quota_can_become_paid IN (0,1)),
    exposes_quota_headers INTEGER NOT NULL CHECK (exposes_quota_headers IN (0,1)),
    model_discovery INTEGER NOT NULL CHECK (model_discovery IN (0,1)),
    requires_explicit_enable INTEGER NOT NULL CHECK (requires_explicit_enable IN (0,1)),
    requires_account_verification INTEGER NOT NULL CHECK (requires_account_verification IN (0,1)),
    metering_unit TEXT NOT NULL DEFAULT '',
    included_units_per_day INTEGER NOT NULL DEFAULT 0,
    region_restriction TEXT NOT NULL DEFAULT '',
    experimental_only INTEGER NOT NULL CHECK (experimental_only IN (0,1)),
    aliases_json TEXT NOT NULL DEFAULT '[]',
    last_reviewed TEXT NOT NULL,
    last_catalog_sync_at TEXT,
    last_quota_sync_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_provider_registry_classification
    ON ai_provider_registry(classification, provider);
