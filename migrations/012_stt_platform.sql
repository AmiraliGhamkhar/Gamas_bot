-- Gamas Speech Platform. Forward-only, additive migration: existing Telegram
-- submissions, transcripts, accounting records and Fernet ciphertext are not
-- rewritten. Provider keys continue to use provider_credentials' encrypted
-- secret_ciphertext column; only safe, non-secret STT metadata is added.

ALTER TABLE provider_credentials ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS stt_provider_registry (
    provider TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    protocol TEXT NOT NULL,
    classification TEXT NOT NULL,
    persian_batch INTEGER NOT NULL CHECK (persian_batch IN (0,1)),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    last_verified_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stt_provider_settings (
    provider TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    billing_state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (billing_state IN ('unknown','free','paid')),
    billing_attested_at TEXT,
    billing_attested_by_admin_id INTEGER,
    updated_by_admin_id INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stt_routes (
    provider TEXT PRIMARY KEY,
    position INTEGER NOT NULL UNIQUE CHECK (position >= 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    model_override TEXT,
    updated_by_admin_id INTEGER,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stt_routes_enabled_position
    ON stt_routes(enabled, position);

CREATE TABLE IF NOT EXISTS stt_model_registry (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'available'
        CHECK (status IN ('available','deprecated','retired','unavailable')),
    language_support_json TEXT NOT NULL DEFAULT '[]',
    persian_supported INTEGER NOT NULL DEFAULT 0 CHECK (persian_supported IN (0,1)),
    feature_support_json TEXT NOT NULL DEFAULT '[]',
    free_status TEXT NOT NULL DEFAULT 'unknown',
    max_duration_seconds INTEGER,
    max_file_size INTEGER,
    quality_score REAL,
    deprecated INTEGER NOT NULL DEFAULT 0 CHECK (deprecated IN (0,1)),
    deprecation_date TEXT,
    available INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0,1)),
    source TEXT NOT NULL DEFAULT 'static_seed',
    last_verified TEXT,
    last_benchmarked_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, model)
);
CREATE INDEX IF NOT EXISTS idx_stt_models_provider_available
    ON stt_model_registry(provider, available, model);

-- Quota observations are scoped to a specific encrypted credential, or to an
-- explicitly named environment/provider account. Unknown is represented by no
-- row; Gamas does not manufacture a quota based on marketing copy.
CREATE TABLE IF NOT EXISTS stt_quota_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    account_scope TEXT NOT NULL DEFAULT 'provider',
    credential_id INTEGER REFERENCES provider_credentials(id) ON DELETE SET NULL,
    model TEXT NOT NULL DEFAULT '',
    quota_type TEXT NOT NULL,
    quota_limit REAL,
    used REAL,
    remaining REAL,
    reset_at TEXT,
    source TEXT NOT NULL DEFAULT 'unknown',
    observed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, account_scope, model, quota_type)
);
CREATE INDEX IF NOT EXISTS idx_stt_quota_latest
    ON stt_quota_snapshots(provider, account_scope, quota_type, observed_at DESC);

-- A budget is an explicit, account-scoped authorization ceiling. Admin-set
-- values represent the remaining no-overage allocation seen in the provider
-- console. Each routed whole-file job atomically reserves from it before the
-- HTTP request, so concurrent workers cannot race through a free limit.
CREATE TABLE IF NOT EXISTS stt_quota_budgets (
    provider TEXT NOT NULL,
    account_scope TEXT NOT NULL,
    credential_id INTEGER REFERENCES provider_credentials(id) ON DELETE SET NULL,
    quota_type TEXT NOT NULL,
    quota_limit REAL NOT NULL CHECK (quota_limit >= 0),
    remaining REAL NOT NULL CHECK (remaining >= 0 AND remaining <= quota_limit),
    reset_at TEXT,
    source TEXT NOT NULL DEFAULT 'admin_attestation',
    updated_by_admin_id INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, account_scope, quota_type)
);

CREATE TABLE IF NOT EXISTS stt_quota_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    account_scope TEXT NOT NULL,
    credential_id INTEGER REFERENCES provider_credentials(id) ON DELETE SET NULL,
    submission_id INTEGER REFERENCES audio_submissions(id) ON DELETE SET NULL,
    quota_type TEXT NOT NULL,
    reserved_units REAL NOT NULL CHECK (reserved_units >= 0),
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved','committed','released')),
    created_at TEXT NOT NULL,
    finalized_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_stt_quota_reservations_provider
    ON stt_quota_reservations(provider, account_scope, quota_type, status);

-- Operational metadata only. Transcript text, raw audio, prompts, provider
-- response bodies, API keys and authorization headers must never be written to
-- either table.
CREATE TABLE IF NOT EXISTS stt_usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER REFERENCES audio_submissions(id) ON DELETE SET NULL,
    job_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    credential_id INTEGER REFERENCES provider_credentials(id) ON DELETE SET NULL,
    route_position INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 1,
    result TEXT NOT NULL CHECK (result IN ('success','failure','retry','fallback','skipped')),
    audio_bytes INTEGER,
    audio_duration_seconds REAL,
    latency_ms INTEGER,
    http_status INTEGER,
    error_category TEXT NOT NULL DEFAULT '',
    request_id TEXT,
    quota_type TEXT,
    quota_units REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stt_usage_provider_created
    ON stt_usage_records(provider, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_stt_usage_submission
    ON stt_usage_records(submission_id, id);

CREATE TABLE IF NOT EXISTS stt_provider_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    credential_id INTEGER REFERENCES provider_credentials(id) ON DELETE SET NULL,
    job_id TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    latency_ms INTEGER,
    error_category TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info','warning','error')),
    fields_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stt_events_created
    ON stt_provider_events(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_stt_events_provider_event
    ON stt_provider_events(provider, event, created_at DESC);
