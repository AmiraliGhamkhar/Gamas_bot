-- Special (unlimited) users. A special user is never billed: jobs get no
-- reservation and no debit, but their submissions (with duration_seconds) are
-- still recorded, so the audit trail shows the usage. Admins set the flag with
-- /unlimited; every change is written to the audit table.
ALTER TABLE users ADD COLUMN is_unlimited INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN unlimited_reason TEXT;
ALTER TABLE users ADD COLUMN unlimited_granted_by INTEGER;
ALTER TABLE users ADD COLUMN unlimited_granted_at TEXT;
CREATE INDEX IF NOT EXISTS idx_users_unlimited ON users(is_unlimited, telegram_id);
