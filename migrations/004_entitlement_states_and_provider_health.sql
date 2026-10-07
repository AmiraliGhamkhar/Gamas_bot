-- 004: explicit entitlement lifecycle, expiration ledger events, provider health.
--
-- SQLite cannot alter CHECK constraints, so entitlements and usage_ledger are
-- rebuilt. The order keeps every foreign key valid while foreign_keys=ON:
--   1. rename the old tables (SQLite rewrites child references to the new names),
--   2. create the new tables under the canonical names and copy every row,
--   3. drop the old ledger (no children) and then the old entitlements (whose
--      only child, the old ledger, is gone),
--   4. recreate indexes and the lifecycle triggers.
-- The migration runner wraps this file in one transaction, so it is all-or-nothing.

ALTER TABLE usage_ledger RENAME TO usage_ledger_v3;
ALTER TABLE entitlements RENAME TO entitlements_v3;

CREATE TABLE entitlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    payment_id INTEGER UNIQUE REFERENCES payment_requests(id),
    granted_seconds INTEGER NOT NULL CHECK (granted_seconds > 0),
    remaining_seconds INTEGER NOT NULL CHECK (remaining_seconds >= 0 AND remaining_seconds <= granted_seconds),
    starts_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'exhausted', 'expired', 'revoked')),
    source TEXT NOT NULL CHECK (source IN ('free_lifetime', 'payment', 'admin_credit')),
    granted_by_admin_id INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((source = 'payment' AND payment_id IS NOT NULL) OR source <> 'payment'),
    -- The one-time free plan never expires.
    CHECK (source <> 'free_lifetime' OR expires_at IS NULL)
);

INSERT INTO entitlements
    (id, user_id, plan_id, payment_id, granted_seconds, remaining_seconds, starts_at, expires_at,
     status, source, granted_by_admin_id, reason, created_at, updated_at)
SELECT id, user_id, plan_id, payment_id, granted_seconds, remaining_seconds, starts_at, expires_at,
       CASE WHEN status = 'active' AND remaining_seconds = 0 THEN 'exhausted' ELSE status END,
       source, granted_by_admin_id, reason, created_at, updated_at
FROM entitlements_v3;

CREATE TABLE usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER REFERENCES usage_reservations(id),
    entitlement_id INTEGER REFERENCES entitlements(id),
    submission_id INTEGER REFERENCES audio_submissions(id) ON DELETE SET NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'reserve', 'consume', 'release', 'denied', 'grant', 'admin_credit',
        'adjustment', 'expiration', 'debit'
    )),
    requested_seconds INTEGER NOT NULL DEFAULT 0 CHECK (requested_seconds >= 0),
    reserved_seconds INTEGER NOT NULL DEFAULT 0 CHECK (reserved_seconds >= 0),
    consumed_seconds INTEGER NOT NULL DEFAULT 0 CHECK (consumed_seconds >= 0),
    released_seconds INTEGER NOT NULL DEFAULT 0 CHECK (released_seconds >= 0),
    available_seconds INTEGER,
    admin_telegram_id INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL
);

-- Historical expirations were logged as adjustments; give them their own type.
INSERT INTO usage_ledger
    (id, reservation_id, entitlement_id, submission_id, user_id, event_type, requested_seconds,
     reserved_seconds, consumed_seconds, released_seconds, available_seconds, admin_telegram_id,
     reason, created_at)
SELECT id, reservation_id, entitlement_id, submission_id, user_id,
       CASE WHEN event_type = 'adjustment' AND reason = 'Paid entitlement expired'
            THEN 'expiration' ELSE event_type END,
       requested_seconds, reserved_seconds, consumed_seconds, released_seconds, available_seconds,
       admin_telegram_id, reason, created_at
FROM usage_ledger_v3;

DROP TABLE usage_ledger_v3;
DROP TABLE entitlements_v3;

CREATE UNIQUE INDEX IF NOT EXISTS idx_entitlement_free_once_per_user
    ON entitlements(user_id) WHERE source = 'free_lifetime';
CREATE INDEX IF NOT EXISTS idx_entitlements_balance_order
    ON entitlements(user_id, status, expires_at, id);
CREATE INDEX IF NOT EXISTS idx_entitlements_expiry
    ON entitlements(status, expires_at) WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_usage_ledger_user_created
    ON usage_ledger(user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_usage_ledger_submission
    ON usage_ledger(submission_id, id);
CREATE INDEX IF NOT EXISTS idx_usage_ledger_reservation
    ON usage_ledger(reservation_id, event_type, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_ledger_one_grant_per_entitlement
    ON usage_ledger(entitlement_id) WHERE event_type='grant';

-- Lifecycle: an active entitlement drained to zero is 'exhausted'; seconds
-- returned by a released/finalized reservation make it 'active' again.
-- Expired and revoked entitlements are never revived by these triggers.
CREATE TRIGGER IF NOT EXISTS trg_entitlement_exhausted
AFTER UPDATE OF remaining_seconds ON entitlements
WHEN NEW.remaining_seconds = 0 AND NEW.status = 'active'
BEGIN
    UPDATE entitlements SET status = 'exhausted' WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_entitlement_reactivated
AFTER UPDATE OF remaining_seconds ON entitlements
WHEN NEW.remaining_seconds > 0 AND NEW.status = 'exhausted'
BEGIN
    UPDATE entitlements SET status = 'active' WHERE id = NEW.id;
END;

-- Payment review metadata and lookups used by the admin status lists.
CREATE INDEX IF NOT EXISTS idx_payment_requests_status_reviewed
    ON payment_requests(status, reviewed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_action_created
    ON admin_audit_log(action, created_at DESC);

-- Cached result of the last manual health check (never a secret).
ALTER TABLE provider_credentials ADD COLUMN health_state TEXT;
ALTER TABLE provider_credentials ADD COLUMN health_checked_at TEXT;
ALTER TABLE provider_credentials ADD COLUMN health_latency_ms INTEGER;
ALTER TABLE provider_credentials ADD COLUMN health_detail TEXT;
