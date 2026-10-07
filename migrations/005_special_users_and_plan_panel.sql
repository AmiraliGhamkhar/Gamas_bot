-- Special (unlimited) users, the three extra canonical prepaid plans
-- (5 / 10 / 20 hours) and the columns the administrator plan panel needs.
--
-- Every statement is idempotent: the runner applies each file once, but a
-- database restored from an older backup must survive a re-run without losing
-- accounting rows.

-- 1. Special users ----------------------------------------------------------
-- A special user is not billed at all: the reservation engine skips the
-- entitlement ledger for them and records the processed media instead.
ALTER TABLE users ADD COLUMN is_unlimited INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN unlimited_reason TEXT;
ALTER TABLE users ADD COLUMN unlimited_granted_by INTEGER;
ALTER TABLE users ADD COLUMN unlimited_granted_at TEXT;
CREATE INDEX IF NOT EXISTS idx_users_unlimited ON users(is_unlimited, telegram_id);

-- 2. Administrator-managed plans --------------------------------------------
-- is_custom marks a row an administrator created or edited from the panel.
-- The canonical catalogue sync never overwrites such a row, and it never
-- touches the enabled flag of an existing row either, so a plan an operator
-- disabled (or priced differently) survives a restart.
ALTER TABLE plans ADD COLUMN is_custom INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN created_by_admin_id INTEGER;
ALTER TABLE plans ADD COLUMN updated_by_admin_id INTEGER;

-- 3. Canonical short plans --------------------------------------------------
INSERT OR IGNORE INTO plans
    (code, name, included_seconds, price_toman, validity_days, is_free, sort_order, enabled, created_at, updated_at)
VALUES
    ('paid_5h_30d', '۵ ساعت / ۳۰ روز', 18000, 50000, 30, 0, 1, 1,
     strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
    ('paid_10h_30d', '۱۰ ساعت / ۳۰ روز', 36000, 75000, 30, 0, 2, 1,
     strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
    ('paid_20h_30d', '۲۰ ساعت / ۳۰ روز', 72000, 130000, 30, 0, 3, 1,
     strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));

-- The long plans seeded by migration 003 keep their prices and move after the
-- short ones. Rows an administrator has already re-ordered stay untouched.
UPDATE plans SET sort_order = 4
 WHERE code = 'paid_25h_30d' AND is_custom = 0 AND sort_order = 1;
UPDATE plans SET sort_order = 5
 WHERE code = 'paid_50h_30d' AND is_custom = 0 AND sort_order = 2;
