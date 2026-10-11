-- Production hardening: canonical free-plan code, receipt provenance columns,
-- and the indexes the provider-health / credential-rotation panels query.
--
-- Every statement is idempotent: the runner applies each file once, but a
-- database restored from an older backup (or created before schema_migrations
-- existed) must survive a re-run without losing accounting rows.

-- 1. Canonical plan code -----------------------------------------------------
-- Migration 003 seeded the lifetime free plan as 'free_lifetime_1h'. The
-- canonical code is 'free_1h'; rename in place so existing entitlements keep
-- their foreign key and nobody is granted a second free hour.
UPDATE plans
   SET code = 'free_1h',
       updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
 WHERE code = 'free_lifetime_1h'
   AND NOT EXISTS (SELECT 1 FROM plans WHERE code = 'free_1h');

-- Seed the canonical row when it is missing entirely (fresh database, or a
-- database where the rename above could not run).
INSERT OR IGNORE INTO plans
    (code, name, included_seconds, price_toman, validity_days, is_free, sort_order, created_at, updated_at)
VALUES
    ('free_1h', 'رایگان مادام‌العمر', 3600, 0, NULL, 1, 0,
     strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));

-- 2. Receipt provenance ------------------------------------------------------
-- receipt_file_id keeps the Telegram file identifier of the reviewed receipt so
-- an administrator can still inspect it after local retention deletes the file;
-- admin_note records the reviewer's free-form note next to the status.
ALTER TABLE payment_requests ADD COLUMN receipt_file_id TEXT;
ALTER TABLE payment_requests ADD COLUMN admin_note TEXT;

-- 3. Indexes -----------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_payment_requests_reviewer
    ON payment_requests(reviewer_telegram_id, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_entitlements_status_expiry
    ON entitlements(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_usage_reservations_status
    ON usage_reservations(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_provider_credentials_cooldown
    ON provider_credentials(cooldown_until);
CREATE INDEX IF NOT EXISTS idx_provider_credentials_enabled
    ON provider_credentials(enabled, service, provider);
CREATE INDEX IF NOT EXISTS idx_provider_credentials_service
    ON provider_credentials(service, provider);
CREATE INDEX IF NOT EXISTS idx_admin_audit_target
    ON admin_audit_log(target_type, target_id, created_at DESC);
