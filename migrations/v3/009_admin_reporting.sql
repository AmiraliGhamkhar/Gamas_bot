-- Period-accurate admin reporting.
--
-- Before this migration the admin overview could only answer lifetime totals:
-- the schema recorded when a submission was created and finished, but not when
-- processing actually started, why it failed, or which STT engine produced the
-- transcript. That made queue wait, processing time, error categories and
-- provider usage impossible to report from authoritative records (and the
-- transcript itself is deleted by the retention sweep, so engine history would
-- otherwise be lost too).
--
-- The three new columns are nullable: rows written before the upgrade keep
-- NULL, which the reports treat as "not recorded" rather than zero.
--
-- ADD COLUMN is replayed safely by db.Database._without_applied_columns, so a
-- crash midway through this file is safe to retry.
ALTER TABLE submissions ADD COLUMN started_at TEXT;
ALTER TABLE submissions ADD COLUMN error_kind TEXT;
ALTER TABLE submissions ADD COLUMN stt_engine TEXT;

-- Period filters ("last 7 days", "today") and the volume-by-day grouping.
CREATE INDEX IF NOT EXISTS idx_submissions_created ON submissions(created_at);
CREATE INDEX IF NOT EXISTS idx_submissions_finished ON submissions(finished_at);
CREATE INDEX IF NOT EXISTS idx_submissions_kind_created ON submissions(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_users_joined ON users(joined_at);

-- Period payment totals and status breakdown.
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at, status);

-- Expiring credit lots ("what expires in the next 7 days") scan credit rows
-- with an expiry, never the whole append-only ledger.
CREATE INDEX IF NOT EXISTS idx_ledger_kind_expiry ON ledger(kind, expires_at);
