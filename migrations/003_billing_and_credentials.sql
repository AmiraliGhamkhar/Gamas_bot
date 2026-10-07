-- Canonical plans; Database.sync_plan_catalog applies validated deployment overrides.
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    included_seconds INTEGER NOT NULL CHECK (included_seconds > 0),
    price_toman INTEGER NOT NULL DEFAULT 0 CHECK (price_toman >= 0),
    validity_days INTEGER CHECK (validity_days IS NULL OR validity_days > 0),
    is_free INTEGER NOT NULL DEFAULT 0 CHECK (is_free IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO plans
    (code, name, included_seconds, price_toman, validity_days, is_free, sort_order, created_at, updated_at)
VALUES
    ('free_lifetime_1h', 'رایگان مادام‌العمر', 3600, 0, NULL, 1, 0,
     strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
    ('paid_25h_30d', '۲۵ ساعت / ۳۰ روز', 90000, 150000, 30, 0, 1,
     strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
    ('paid_50h_30d', '۵۰ ساعت / ۳۰ روز', 180000, 250000, 30, 0, 2,
     strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));

CREATE TABLE IF NOT EXISTS payment_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    amount_toman INTEGER NOT NULL CHECK (amount_toman > 0),
    status TEXT NOT NULL CHECK (status IN ('awaiting_receipt', 'pending', 'approved', 'rejected', 'cancelled')),
    receipt_path TEXT,
    receipt_message_id INTEGER,
    receipt_submitted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewer_telegram_id INTEGER,
    rejection_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_one_open_request_per_user
    ON payment_requests(user_id) WHERE status IN ('awaiting_receipt', 'pending');
CREATE INDEX IF NOT EXISTS idx_payment_requests_status_created
    ON payment_requests(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_payment_requests_user_created
    ON payment_requests(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS entitlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    payment_id INTEGER UNIQUE REFERENCES payment_requests(id),
    granted_seconds INTEGER NOT NULL CHECK (granted_seconds > 0),
    remaining_seconds INTEGER NOT NULL CHECK (remaining_seconds >= 0 AND remaining_seconds <= granted_seconds),
    starts_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'revoked')),
    source TEXT NOT NULL CHECK (source IN ('free_lifetime', 'payment', 'admin_credit')),
    granted_by_admin_id INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((source = 'payment' AND payment_id IS NOT NULL) OR source <> 'payment')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entitlement_free_once_per_user
    ON entitlements(user_id) WHERE source = 'free_lifetime';
CREATE INDEX IF NOT EXISTS idx_entitlements_balance_order
    ON entitlements(user_id, status, expires_at, id);

-- Existing users receive their single lifetime free entitlement in this migration.
INSERT OR IGNORE INTO entitlements
    (user_id, plan_id, payment_id, granted_seconds, remaining_seconds, starts_at, expires_at,
     status, source, granted_by_admin_id, reason, created_at, updated_at)
SELECT u.id, p.id, NULL, p.included_seconds, p.included_seconds,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), NULL, 'active', 'free_lifetime', NULL,
       'One-time lifetime free plan migration',
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM users AS u JOIN plans AS p ON p.code = 'free_lifetime_1h'
WHERE NOT EXISTS (
    SELECT 1 FROM entitlements AS e WHERE e.user_id = u.id AND e.source = 'free_lifetime'
);

CREATE TABLE IF NOT EXISTS usage_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL UNIQUE REFERENCES audio_submissions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    required_seconds INTEGER NOT NULL CHECK (required_seconds >= 0),
    reserved_seconds INTEGER NOT NULL DEFAULT 0 CHECK (reserved_seconds >= 0),
    consumed_seconds INTEGER NOT NULL DEFAULT 0 CHECK (consumed_seconds >= 0),
    released_seconds INTEGER NOT NULL DEFAULT 0 CHECK (released_seconds >= 0),
    status TEXT NOT NULL CHECK (status IN ('reserved', 'consumed', 'partially_consumed', 'released', 'insufficient')),
    reason TEXT,
    created_at TEXT NOT NULL,
    finalized_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_reservations_user_created
    ON usage_reservations(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER REFERENCES usage_reservations(id),
    entitlement_id INTEGER REFERENCES entitlements(id),
    submission_id INTEGER REFERENCES audio_submissions(id) ON DELETE SET NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('reserve', 'consume', 'release', 'denied', 'grant', 'admin_credit', 'adjustment')),
    requested_seconds INTEGER NOT NULL DEFAULT 0 CHECK (requested_seconds >= 0),
    reserved_seconds INTEGER NOT NULL DEFAULT 0 CHECK (reserved_seconds >= 0),
    consumed_seconds INTEGER NOT NULL DEFAULT 0 CHECK (consumed_seconds >= 0),
    released_seconds INTEGER NOT NULL DEFAULT 0 CHECK (released_seconds >= 0),
    available_seconds INTEGER,
    admin_telegram_id INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_ledger_user_created
    ON usage_ledger(user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_usage_ledger_submission
    ON usage_ledger(submission_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_ledger_one_grant_per_entitlement
    ON usage_ledger(entitlement_id) WHERE event_type='grant';

INSERT OR IGNORE INTO usage_ledger
    (entitlement_id, user_id, event_type, reserved_seconds, reason, created_at)
SELECT e.id, e.user_id, 'grant', e.granted_seconds, 'Lifetime free plan grant', e.created_at
FROM entitlements AS e
WHERE e.source='free_lifetime';

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_telegram_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created
    ON admin_audit_log(created_at DESC, id DESC);

-- Only Fernet ciphertext is persisted. The master key is supplied via the process environment.
CREATE TABLE IF NOT EXISTS provider_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL CHECK (service IN ('stt', 'notes')),
    provider TEXT NOT NULL,
    label TEXT NOT NULL,
    secret_ciphertext TEXT NOT NULL,
    secret_last4 TEXT NOT NULL,
    base_url TEXT,
    model TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    quarantined_at TEXT,
    cooldown_until TEXT,
    last_status_code INTEGER,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_used_at TEXT,
    last_error TEXT,
    created_by_admin_id INTEGER NOT NULL,
    updated_by_admin_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(service, provider, label)
);
CREATE INDEX IF NOT EXISTS idx_provider_credentials_lookup
    ON provider_credentials(service, provider, enabled, priority, id);
