-- Mandatory Telegram channel subscription enforcement (v4).
--
-- These columns are observability only. Membership is *always* re-verified
-- against Telegram on every protected interaction; the values stored here are
-- the last observed state for the admin panel and audit trail, never the
-- enforcement source. Existing rows keep NULL until their next interaction,
-- so no user is deleted, recreated or marked restricted by this migration.
ALTER TABLE users ADD COLUMN subscription_status TEXT;
ALTER TABLE users ADD COLUMN subscription_verified_at TEXT;
