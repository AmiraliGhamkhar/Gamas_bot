-- AI Provider Platform: forward-only additive schema.
-- Reuses provider_credentials + admin_audit_log; nothing existing is altered
-- destructively (only ADD COLUMNs, which the migration runner tolerates).

-- Provider-level admin state (overrides registry defaults per deployment).
CREATE TABLE IF NOT EXISTS ai_provider_settings (
    provider TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    free_only_blocked INTEGER NOT NULL DEFAULT 0 CHECK (free_only_blocked IN (0,1)),
    experimental_unlocked INTEGER NOT NULL DEFAULT 0 CHECK (experimental_unlocked IN (0,1)),
    plan_label TEXT,
    notes TEXT,
    updated_by_admin_id INTEGER,
    updated_at TEXT NOT NULL
);

-- Discovered + seeded model catalog. Historical rows are never deleted;
-- disappeared models are marked available=0.
CREATE TABLE IF NOT EXISTS ai_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    display_name TEXT,
    context_window INTEGER NOT NULL DEFAULT 0,
    max_output_tokens INTEGER NOT NULL DEFAULT 0,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    free_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (free_status IN ('unknown','free_permanent','free_plan','free_promotional','paid')),
    free_until TEXT,
    commercial_use_allowed INTEGER NOT NULL DEFAULT 1 CHECK (commercial_use_allowed IN (0,1)),
    region_restriction TEXT,
    deprecated INTEGER NOT NULL DEFAULT 0 CHECK (deprecated IN (0,1)),
    deprecation_date TEXT,
    available INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0,1)),
    source TEXT NOT NULL DEFAULT 'static_seed',
    source_last_verified_at TEXT,
    quality_score REAL,
    last_benchmarked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, model)
);
CREATE INDEX IF NOT EXISTS idx_ai_models_provider ON ai_models(provider, available, free_status);

-- Task-aware routing: one ordered provider list per (service, task_type).
CREATE TABLE IF NOT EXISTS ai_provider_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL DEFAULT 'notes',
    task_type TEXT NOT NULL DEFAULT 'chunk_structuring',
    route_index INTEGER NOT NULL,
    provider TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    free_only INTEGER NOT NULL DEFAULT 1 CHECK (free_only IN (0,1)),
    model TEXT,
    updated_by_admin_id INTEGER,
    updated_at TEXT NOT NULL,
    UNIQUE(service, task_type, route_index)
);
CREATE INDEX IF NOT EXISTS idx_ai_routes_task ON ai_provider_routes(service, task_type, route_index);

-- Per-request usage ledger: metadata only, never prompt/response content.
CREATE TABLE IF NOT EXISTS ai_usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    service TEXT NOT NULL,
    provider TEXT NOT NULL,
    canonical TEXT,
    credential_id INTEGER,
    credential_label TEXT,
    model TEXT,
    request_type TEXT,
    route_position INTEGER,
    attempt INTEGER,
    job_id TEXT,
    submission_id INTEGER,
    latency_ms INTEGER,
    http_status INTEGER,
    estimated_input_tokens INTEGER,
    actual_input_tokens INTEGER,
    actual_output_tokens INTEGER,
    total_tokens INTEGER,
    finish_reason TEXT,
    retry_after_seconds REAL,
    quota_headers_json TEXT,
    json_strategy TEXT,
    result TEXT NOT NULL,
    error_class TEXT,
    free_class TEXT DEFAULT 'unknown' CHECK (free_class IN ('free','paid','unknown')),
    request_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_created ON ai_usage_records(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ai_usage_provider ON ai_usage_records(provider, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_usage_submission ON ai_usage_records(submission_id);

-- Daily rollup for quota panels and rate-limit accounting.
CREATE TABLE IF NOT EXISTS ai_usage_daily (
    day TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    requests INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    rate_limit_hits INTEGER NOT NULL DEFAULT 0,
    server_errors INTEGER NOT NULL DEFAULT 0,
    client_errors INTEGER NOT NULL DEFAULT 0,
    fallbacks INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms_total INTEGER NOT NULL DEFAULT 0,
    paid_block_events INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider, model)
);

-- Latest observed quota state per provider/credential (from headers/probes).
CREATE TABLE IF NOT EXISTS ai_quota_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    credential_id INTEGER,
    model TEXT NOT NULL DEFAULT '',
    window TEXT NOT NULL DEFAULT 'day',
    remaining TEXT,
    reset_at TEXT,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'headers',
    UNIQUE(provider, credential_id, model, window)
);

-- Rolling structured event log for the admin Logs panel (pruned by app layer).
CREATE TABLE IF NOT EXISTS ai_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    event TEXT NOT NULL,
    service TEXT NOT NULL DEFAULT 'notes',
    provider TEXT,
    canonical TEXT,
    model TEXT,
    request_type TEXT,
    route_position INTEGER,
    http_status INTEGER,
    latency_ms INTEGER,
    error_class TEXT,
    detail TEXT,
    job_id TEXT,
    check_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_events_created ON ai_events(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ai_events_provider ON ai_events(provider, event);

-- Credential metadata extensions (non-destructive ADD COLUMNs).
ALTER TABLE provider_credentials ADD COLUMN key_type TEXT DEFAULT 'api_key';
ALTER TABLE provider_credentials ADD COLUMN free_only INTEGER;
ALTER TABLE provider_credentials ADD COLUMN paid_allowed INTEGER;
ALTER TABLE provider_credentials ADD COLUMN billing_state TEXT;
ALTER TABLE provider_credentials ADD COLUMN expires_at TEXT;
ALTER TABLE provider_credentials ADD COLUMN notes TEXT;
ALTER TABLE provider_credentials ADD COLUMN failure_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE provider_credentials ADD COLUMN last_quota_json TEXT;
